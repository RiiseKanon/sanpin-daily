#!/usr/bin/env python3
"""
三品王每日品牌动态日报 — 每日主任务脚本

用法:
    python scheduler/daily_job.py                    # 使用当天日期
    python scheduler/daily_job.py --date 2026-07-08  # 指定日期
    python scheduler/daily_job.py --dry-run          # 试运行（不写入文件）

流程:
    1. 加载品牌库和事件库
    2. 并行执行4个采集器（5天窗口）
    3. 处理流水线（去重→状态机→评级→聚合）
    4. 渲染HTML日报
    5. 归档 + 输出 latest.html
"""
import asyncio
import sys
import os
from pathlib import Path
from datetime import date, timedelta
from loguru import logger

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from collector.self_monitor import SelfMonitorCollector
from collector.competitor_news import CompetitorNewsCollector
from collector.industry_news import IndustryNewsCollector
from processor import ProcessingPipeline
from storage.event_store import EventStore
from storage.report_store import ReportStore
from generator.renderer import ReportRenderer
import config
import json
from datetime import date, timedelta



async def run_collection(target_date: str) -> list:
    """并行运行所有采集器"""
    collectors = [
        SelfMonitorCollector(),
        CompetitorNewsCollector(),
        IndustryNewsCollector(),
    ]

    all_items = []
    tasks = [c.collect(target_date) for c in collectors]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, list):
            all_items.extend(result)
            logger.info(f"[{collectors[i].source_name}] 采集完成: {len(result)} 条")
        elif isinstance(result, Exception):
            logger.error(f"[{collectors[i].source_name}] 采集异常: {result}")

    # 关闭所有采集器的HTTP客户端
    for c in collectors:
        try:
            await c.close()
        except Exception:
            pass

    return all_items


def run_daily_job(target_date: str = None, dry_run: bool = False):
    """执行每日采集+处理+生成全流程"""
    if target_date is None:
        target_date = date.today().isoformat()

    logger.info(f"{'='*60}")
    logger.info(f"三品王每日品牌动态日报 — {target_date}")
    logger.info(f"{'='*60}")

    # Step 1: 确保目录存在
    config.ensure_dirs()

    # Step 2: 加载事件库
    event_store = EventStore(config.EVENTS_PATH)
    logger.info(f"事件库: {event_store.total()} 条事件")

    # Step 3: 执行采集
    logger.info(f"开始采集（窗口: {config.TIME_WINDOW_DAYS} 天）...")
    raw_items = asyncio.run(run_collection(target_date))
    logger.info(f"采集完成: 共 {len(raw_items)} 条原始条目")

    # Step 3.5: 竞品搜索代理 — 如果竞品动态不足，尝试注入外部搜索结果
    injected_items = _inject_external_searches(target_date)
    if injected_items:
        logger.info(f"注入外部搜索结果: {len(injected_items)} 条")
        raw_items.extend(injected_items)

    # Step 4: 处理流水线
    pipeline = ProcessingPipeline(event_store)
    report = pipeline.process_raw_items(raw_items, target_date)

    # Step 4.5: Part 4 — 今日焦点品牌（从核心竞品中轮流深挖）
    report["part_4"] = _build_focus_brand(target_date)

    # Step 4.6: Part 5 — 品牌库变动简报（仅周一）
    report["part_5"] = _build_library_brief(target_date)

    # Step 5: 渲染HTML
    renderer = ReportRenderer()

    if not dry_run:
        # 输出 latest.html
        html_path = renderer.render_to_file(report)
        # 归档
        archive_path = renderer.archive(report)
        # 保存数据
        report_store = ReportStore()
        report_store.save_report(report)

        logger.info(f"✅ 日报生成完成!")
        logger.info(f"   最新版: {html_path}")
        logger.info(f"   归档:   {archive_path}")

        # Step 6: 部署公网访问 + 发送钉钉通知
        _deploy_and_notify(report, target_date)
    else:
        # 试运行：只输出统计
        html = renderer.render(report)
        logger.info(f"🔍 试运行完成（未写入文件）")
        logger.info(f"   HTML大小: {len(html)} 字符")

    # 输出摘要
    stats = report.get("stats", {})
    pipeline_stats = report.get("pipeline_stats", {})
    logger.info(f"   统计: 事件{stats.get('total_events',0)}条 "
                f"(新{pipeline_stats.get('new_events',0)} "
                f"更{pipeline_stats.get('updated_events',0)}) "
                f"排名{stats.get('total_rankings',0)}平台")

    # 事件库状态
    status_counts = event_store.count_by_status()
    logger.info(f"   事件库状态: {status_counts}")

    return report


def _inject_external_searches(target_date: str) -> list:
    """
    读取外部搜索代理产出的结果文件，注入到采集流水线。

    搜索代理工作流:
    1. daily_job 运行采集 → 竞品动态不足 → 输出搜索任务文件
    2. Agent 读取任务文件，使用 WebSearch 批量搜索
    3. Agent 将搜索结果写入 _search_results.json
    4. daily_job 重新运行时读取结果文件，注入到 raw_items

    结果文件格式:
    {
      "target_date": "2026-07-13",
      "results": [
        {
          "brand_name": "尝不忘", "brand_id": "BR002", "circle": "核心竞品",
          "title": "...", "url": "...", "summary": "...", "published_at": "...",
          "category": "渠道/扩张动作"
        },
        ...
      ]
    }
    """
    from collector.base import RawItem
    result_file = config.DATA_DIR / "_search_results.json"
    if not result_file.exists():
        return []

    try:
        with open(result_file, "r") as f:
            data = json.load(f)
    except Exception:
        return []

    # 验证日期匹配
    if data.get("target_date") != target_date:
        logger.info(f"搜索结果日期不匹配（{data.get('target_date')} vs {target_date}），跳过")
        return []

    items = []
    for r in data.get("results", []):
        item = RawItem(
            source="AI搜索代理",
            url=r.get("url", ""),
            title=r.get("title", ""),
            content=r.get("summary", ""),
            published_at=r.get("published_at", target_date),
            brand_name=r.get("brand_name", ""),
            brand_id=r.get("brand_id", ""),
            circle=r.get("circle", ""),
            category=r.get("category", "行业趋势/政策"),
            keywords=r.get("keywords", []),
            metadata={"query": r.get("brand_name", ""), "source_type": "ai_websearch"},
        )
        items.append(item)

    # 读取后删除，避免重复注入
    result_file.unlink()
    logger.info(f"从 _search_results.json 注入 {len(items)} 条外部搜索结果（已删除源文件）")

    return items


def _export_search_tasks(target_date: str) -> Path:
    """
    导出竞品搜索任务清单为 JSON 文件，供外部搜索代理使用。

    返回文件路径。
    """
    from collector.competitor_news import CompetitorNewsCollector
    c = CompetitorNewsCollector()
    tasks = c.build_search_tasks(target_date)

    output_file = config.DATA_DIR / "_search_tasks.json"
    output_file.write_text(json.dumps({
        "target_date": target_date,
        "total_tasks": len(tasks),
        "tasks": tasks,
        "instruction": (
            "对每个品牌的 query 执行 WebSearch，收集近 7 天的新闻动态。"
            "每条结果需包含: brand_name, brand_id, circle, title, url, summary, published_at, category。"
            "将结果写入 data/_search_results.json，格式见 _inject_external_searches 文档。"
        ),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(f"📋 搜索任务已导出: {output_file} ({len(tasks)} 个品牌)")
    return output_file


def _deploy_and_notify(report: dict, target_date: str):
    """部署日报 + 发送钉钉通知

    双环境适配：
    - 沙箱环境：cloudflared 隧道（实时链接）+ GitHub 存档
    - GitHub Actions：GitHub Pages + jsDelivr CDN（国内备选）
    """
    try:
        import subprocess, shutil, time, re, socket, tempfile, os as _os
        from pathlib import Path
        from notifier.dingtalk import send_report_link

        is_ci = _os.getenv("CI", "") or _os.getenv("GITHUB_ACTIONS", "")

        # 1) 拷贝日报到 serve 目录 + 生成历史索引
        serve_dir = Path(config.PROJECT_ROOT) / "serve"
        report_dir = serve_dir / "report" / target_date
        report_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.LATEST_HTML_PATH, report_dir / "index.html")
        _build_history_index(serve_dir)
        logger.info("📋 历史索引已更新")

        # 2) 推送到 GitHub 永久存档
        github_url = None
        try:
            repo_dir = Path(tempfile.gettempdir()) / "sanpin-daily-pages"
            repo_url = "https://github.com/RiiseKanon/sanpin-daily.git"
            token = _os.getenv("GH_TOKEN", "")

            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            auth_url = repo_url.replace("https://", f"https://RiiseKanon:{token}@")
            subprocess.run(["git", "clone", "--depth", "1", auth_url, str(repo_dir)],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "RiiseKanon"], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "RiiseKanon@users.noreply.github.com"], check=True)

            gr_dir = repo_dir / "report" / target_date
            gr_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(config.LATEST_HTML_PATH, gr_dir / "index.html")
            shutil.copy(config.LATEST_HTML_PATH, repo_dir / "index.html")
            # 同时复制历史索引
            shutil.copy(serve_dir / "index.html", repo_dir / "index.html")

            subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", f"日报 {target_date}"], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "push"], check=True)

            github_url = f"https://riisekanon.github.io/sanpin-daily/report/{target_date}/"
            logger.info(f"📦 GitHub 存档完成: {github_url}")
        except Exception as e:
            logger.warning(f"GitHub 推送失败（不影响主流程）: {e}")

        # 3) 部署公网链接
        report_url = None
        if is_ci:
            # GitHub Actions 环境：直接用 GitHub Pages + jsDelivr CDN
            report_url = f"https://cdn.jsdelivr.net/gh/RiiseKanon/sanpin-daily@main/report/{target_date}/index.html"
            logger.info(f"✅ CI 部署: {report_url}")
        else:
            # 沙箱环境：cloudflared 隧道
            port = 8888
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_running = s.connect_ex(("127.0.0.1", port)) == 0
            s.close()

            if not server_running:
                subprocess.Popen(
                    ["python3.11", "-m", "http.server", str(port), "--directory", str(serve_dir)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                time.sleep(1)

            tunnel_url = None
            cf_log = Path("/tmp/cf.log")
            if cf_log.exists():
                text = cf_log.read_text()
                m = re.search(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com", text)
                if m:
                    tunnel_url = m.group(0)

            if not tunnel_url:
                logger.info("🌐 启动 cloudflared 隧道...")
                cf_log.unlink(missing_ok=True)
                subprocess.Popen(
                    ["cloudflared", "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
                    stdout=open(str(cf_log), "w"), stderr=subprocess.STDOUT,
                )
                for _ in range(15):
                    time.sleep(2)
                    if cf_log.exists():
                        m = re.search(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com", cf_log.read_text())
                        if m:
                            tunnel_url = m.group(0)
                            break

            if tunnel_url:
                report_url = f"{tunnel_url}/report/{target_date}/"
                logger.info(f"✅ 隧道就绪: {report_url}")
            else:
                logger.warning("cloudflared 隧道未就绪")

        if not report_url:
            report_url = github_url or "https://riisekanon.github.io/sanpin-daily/"

        # 4) 发送钉钉通知
        citable = sum(
            len(report[p]["events"] if p != "part_2" else report[p].get("events", []))
            for p in ["part_1", "part_2", "part_3"]
        )
        leads = len(report.get("part_leads", {}).get("events", []))
        stats = report.get("stats", {})
        notify_stats = {
            "total_events": stats.get("total_events", citable + leads),
            "citable": citable,
            "leads": leads,
        }

        ok = send_report_link(report_url, github_url, target_date, notify_stats)
        if ok:
            logger.info(f"📨 钉钉通知已发送: {report_url}")
        else:
            logger.warning("钉钉通知发送失败")
    except Exception as e:
        logger.error(f"部署/通知异常: {e}")
        import traceback
        logger.error(traceback.format_exc())


def _build_focus_brand(target_date: str) -> dict:
    """Part 4: 从43个核心竞品中按日期轮流选取焦点品牌"""
    lib = config.load_brand_library()
    core = [b for b in lib["brands"] if b["circle"] == "核心竞品"]
    core_sorted = sorted(core, key=lambda x: x["id"])

    # 基于日期计算轮换索引（每天轮换一个）
    try:
        d = date.fromisoformat(target_date)
        base = date(2026, 7, 8)  # 起始日期
        days_diff = (d - base).days
        idx = days_diff % len(core_sorted)
    except (ValueError, TypeError):
        idx = 0

    brand = core_sorted[idx]

    # 重叠维度分析
    overlap_count = brand.get("overlap_count", 0)
    overlap_dims = brand.get("overlap_dimensions", [])
    level = brand.get("level", "?")

    overlap_analysis = _analyze_overlap(brand)

    return {
        "title": "今日焦点品牌",
        "rating_icon": "📖",
        "focus_brand": {
            "name": brand["name"],
            "circle": brand["circle"],
            "rotation_index": idx + 1,
            "category": brand.get("category", ""),
            "price_zone": brand.get("price_zone", ""),
            "store_count": brand.get("store_count", ""),
            "headquarters": brand.get("headquarters", ""),
            "founded": brand.get("founded", ""),
            "regions": brand.get("regions", ""),
            "confidence": brand.get("confidence", ""),
            "level": level,
            "overlap_count": overlap_count,
            "overlap_dimensions": overlap_dims,
            "overlap_analysis": overlap_analysis,
            # 近期动态已取消（V2.3：焦点品牌聚焦深度分析而非新闻罗列）
            "recent_news": [],
            "risk_assessment": _assess_risk(brand),
            # AI 查询分析（基于品牌名搜索，AI 提炼洞察）
            "ai_analysis": _ai_brand_analysis(brand),
        },
        "empty": False,
    }


def _analyze_overlap(brand: dict) -> str:
    """分析与三品王的重叠维度"""
    parts = []
    dims = brand.get("overlap_dimensions", [])
    count = brand.get("overlap_count", 0)

    if "品类" in dims:
        parts.append("品类相同（粉面米线类），在同一赛道上直接竞争")
    if "价格带" in dims:
        parts.append(f"价格带重叠（{brand.get('price_zone','?')}），面向同一消费群体")
    if "区域" in dims:
        parts.append(f"主营区域重叠（广西），门店覆盖范围存在正面竞争")
    if "场景" in dims:
        parts.append("消费场景一致（到店+外卖双渠道快餐），争夺同一批客流")

    if not parts:
        parts.append("重叠维度较少，竞争关系较弱")

    return "；".join(parts) + f"。重叠度 {count}/4，核心竞品 Level {brand.get('level','?')}。"


def _assess_risk(brand: dict) -> str:
    """评估品牌对三品王的竞争威胁"""
    level = brand.get("level", 3)
    store_count_str = brand.get("store_count", "?")

    try:
        store_count = int(store_count_str.replace("+", "").replace(">", "").replace("约", "").strip())
    except (ValueError, TypeError):
        store_count = 0

    if level >= 4:
        return f"⚠️ 高威胁：四维完全重叠，直接竞品。门店数 {store_count_str}，需重点监控其新品、促销和扩张动作。"
    elif level >= 3:
        if store_count > 200:
            return f"⚠️ 中高威胁：三维重叠。门店数 {store_count_str}，规模较大，需关注其区域扩张和营销策略。"
        else:
            return f"📊 中等威胁：三维重叠。门店数 {store_count_str}，规模中等，重点监控其区域扩张动态。"
    elif level >= 2:
        return f"📊 低威胁：二维重叠。门店数 {store_count_str}，常规监控即可。"
    else:
        return f"✅ 低威胁：重叠度低。门店数 {store_count_str}，保持观察。"


def _ai_brand_analysis(brand: dict) -> str:
    """AI 查询分析：基于品牌库基础数据 + 品牌描述生成竞争态势分析。

    设计：sandbox 环境内百度/必应均有反爬限制，不依赖实时搜索抓取。
    改为基于品牌库已有字段（品类/价格带/门店数/总部/区域/notes/level）
    进行结构化推理，生成可读的竞争态势分析。

    若品牌信息不足（字段大面积缺失），返回空字符串。
    """
    name = brand.get("name", "")
    category = brand.get("category", "")
    store = brand.get("store_count", "")
    price = brand.get("price_zone", "")
    hq = brand.get("headquarters", "")
    regions = brand.get("regions", "")
    founded = brand.get("founded", "")
    notes = brand.get("notes", "")
    level = brand.get("level", "?")
    overlap_dims = brand.get("overlap_dimensions", [])
    overlap_count = brand.get("overlap_count", 0)

    # 信息不足则不产出
    if not name or not category:
        return ""

    # 尝试 LLM
    analysis = _try_llm_brand_analysis(name, category, store, price, hq, regions,
                                       founded, notes, level, overlap_dims, overlap_count)
    if analysis:
        return analysis

    # 回退：规则引擎生成结构化分析
    return _rules_brand_analysis(name, category, store, price, hq, regions,
                                 founded, notes, level, overlap_dims, overlap_count, brand)


def _try_llm_brand_analysis(name, category, store, price, hq, regions,
                            founded, notes, level, overlap_dims, overlap_count) -> str:
    """尝试通过本地 Ollama LLM 生成分析"""
    try:
        import requests as req
        dims_str = "、".join(overlap_dims) if overlap_dims else "无"
        prompt = f"""你是餐饮行业分析专家。请对「{name}」进行一段精炼的品牌背景与经营现状分析（200~300字）。

品牌基础数据：
- 品类: {category} | 价格带: {price} | 门店数: {store}
- 总部: {hq} | 主营区域: {regions} | 创立: {founded}
- 品牌备注: {notes or "无"}

要求：聚焦品牌自身的经营方式、出品过程、供应链模式、资本参与历史、扩张路径等背景和现状。
不要分析与三品王的竞争关系（那部分在报告其他区块已有）。
语气客观专业，不确定信息用"可能/据公开信息"限定。仅输出分析文本。"""

        resp = req.post(
            "http://localhost:11434/api/generate",
            json={"model": "qwen2.5:7b", "prompt": prompt, "stream": False},
            timeout=90
        )
        if resp.status_code == 200:
            text = resp.json().get("response", "").strip()
            if text and len(text) > 50:
                return text + "\n\n— 分析基于品牌库数据，仅供参考"
    except Exception:
        pass
    return ""


def _rules_brand_analysis(name, category, store, price, hq, regions,
                          founded, notes, level, overlap_dims, overlap_count, brand=None) -> str:
    """规则引擎：基于品牌库字段生成品牌背景与经营现状分析。

    聚焦：经营方式、出品特点、供应链模式、资本参与、扩张路径。
    不重复已有的竞争分析（overlap_analysis / risk_assessment）。
    """
    parts = []

    # 第1段：品牌定位与规模
    if store and founded:
        fy = str(founded).rstrip("年") if str(founded).endswith("年") else str(founded)
        parts.append(f"{name}创立于{fy}年，是{category}领域代表性连锁品牌，"
                     f"总部位于{hq}，全国门店约{store}家，主营{regions or '全国'}市场。")
    elif store:
        parts.append(f"{name}是{category}领域连锁品牌，总部位于{hq}，"
                     f"全国门店约{store}家，主营{regions or '全国'}市场。")
    else:
        parts.append(f"{name}是{category}领域品牌，总部位于{hq}，"
                     f"主营{regions or '全国'}市场。")

    # 第2段：经营方式与出品特点
    mode_parts = []
    if notes:
        # 从 notes 中提取关键信息
        if "加盟" in notes:
            mode_parts.append("以加盟模式为主")
        if "直营" in notes:
            mode_parts.append("含直营体系")
        if "中央厨房" in notes or "中央工厂" in notes:
            mode_parts.append("拥有中央厨房/工厂支持标准化出品")
        if "供应链" in notes:
            mode_parts.append("自建供应链体系")
        if "培训" in notes:
            mode_parts.append("有完善的培训体系")

    if mode_parts:
        # 千店品牌但 notes 未提加盟 → 合理推断
        if "加盟" not in str(mode_parts) and "直营" not in str(mode_parts):
            if store and any(c in str(store) for c in ["+", "千", "万"]):
                mode_parts.insert(0, "以加盟连锁模式为主")
        parts.append(f"经营方式：{'，'.join(mode_parts)}。")
    elif store and any(c in str(store) for c in ["+", "千", "万"]):
        parts.append(f"经营方式：以加盟连锁模式为主，依托标准化供应链实现快速复制。")
    else:
        parts.append(f"经营方式：以连锁门店为主，产品标准化程度较高。")

    # 出品特点
    product_notes = []
    if "螺蛳粉" in category or "螺蛳粉" in str(notes):
        product_notes.append("主打柳州螺蛳粉品类")
    if "米粉" in category:
        product_notes.append("以米粉为核心产品线")
    if "酸笋" in str(notes) or "酸豆角" in str(notes):
        product_notes.append("自制核心配料（酸笋/酸豆角等）")
    if price:
        px = str(price).rstrip("元") if str(price).endswith("元") else str(price)
        product_notes.append(f"客单价约{px}元，定位大众快餐消费")
    if product_notes:
        parts.append(f"出品特点：{'；'.join(product_notes)}。")

    # 第3段：资本与扩张背景
    cap_parts = []
    has_weinian = False
    if notes:
        if "微念" in notes or "控股" in notes:
            cap_parts.append(f"2022年微念（李子柒品牌背后公司）完成控股，持股约70%，"
                           f"为品牌注入了资本与供应链资源")
            has_weinian = True
        if "融资" in notes:
            cap_parts.append("有融资记录")
        if "上市" in notes:
            cap_parts.append("有上市计划/传闻")
        # 行业认可：取 notes 第一句（不含微念部分）
        if "十大品牌" in notes or "排名" in notes:
            note_first = notes.split("。")[0] if "。" in notes else notes[:40]
            # 避免重复微念信息
            if has_weinian and "微念" in note_first:
                note_first = notes.split("。")[1].strip() if "。" in notes and len(notes.split("。")) > 1 else ""
            if note_first and "微念" not in note_first:
                cap_parts.append(f"行业认可：{note_first}")

    # 从品牌库原始字段提取额外信息
    bd = brand or {}
    sub_cat = bd.get("sub_category", "")
    official = bd.get("official_channels", [])
    if official:
        ch = [c for c in official if any(k in c for k in ["官网", "公众号", "小程序", "微博"])]
        if ch:
            cap_parts.append(f"官方渠道：{'、'.join(ch[:3])}")

    if cap_parts:
        parts.append(f"背景信息：{'；'.join(cap_parts)}。")

    # 扩张路径
    expand_parts = []
    if "全国" in str(regions) or "跨省" in str(notes) or "多省" in str(notes):
        expand_parts.append("已从广西向全国扩张")
    elif "广西" in str(regions) and "全国" not in str(regions):
        expand_parts.append("目前以广西为根据地，深耕区域市场")
    if store and ("1500" in str(store) or "2000" in str(store) or "1000" in str(store)):
        expand_parts.append("门店规模已进入千店级别")
    if expand_parts:
        parts.append(f"扩张路径：{'；'.join(expand_parts)}。")

    # 补充品牌库原始备注（仅当 notes 信息未被前面覆盖时）
    if notes and len(notes) > 20:
        # 检查是否 notes 的核心信息已在前面段落中
        note_keywords = [w for w in ["微念", "控股", "十大品牌", "加盟", "供应链", "中央厨房"] if w in notes]
        already_covered = all(w in str(parts) for w in note_keywords)
        if not already_covered:
            parts.append(f"补充信息：{notes[:150]}。")

    result = "\n\n".join(parts)
    result += "\n\n— 分析基于品牌库数据（规则引擎），仅供参考"
    return result


def _build_library_brief(target_date: str) -> dict:
    """Part 5: 品牌库变动简报（仅周一输出）"""
    try:
        d = date.fromisoformat(target_date)
        if d.weekday() != 0:  # 非周一
            return {"title": "品牌库变动简报", "rating_icon": "📋", "empty": True}
    except (ValueError, TypeError):
        pass

    lib = config.load_brand_library()
    brands = lib.get("brands", [])

    # 统计
    circles = {}
    confidence = {"高": 0, "中": 0, "低": 0}
    for b in brands:
        c = b.get("circle", "其他")
        circles[c] = circles.get(c, 0) + 1
        conf = b.get("confidence", "中")
        confidence[conf] = confidence.get(conf, 0) + 1

    low_conf = [b["name"] for b in brands if b.get("confidence") == "低"]

    health = (
        f"品牌总数: {len(brands)}\n"
        f"  核心竞品: {circles.get('核心竞品',0)} | 区域竞品: {circles.get('区域竞品',0)} | "
        f"场景竞品: {circles.get('场景竞品',0)} | 替代竞品: {circles.get('替代竞品',0)}\n"
        f"  置信度: 高={confidence['高']} | 中={confidence['中']} | 低={confidence['低']}"
    )

    return {
        "title": "品牌库变动简报",
        "rating_icon": "📋",
        "empty": False,
        "health": health,
        "pending_review": low_conf if low_conf else None,
        "new_brands": None,
        "confidence_changes": None,
        "removed_brands": None,
    }


def _build_history_index(serve_dir: Path):
    """生成历史日报索引页，作为首页"""
    archive_dir = config.ARCHIVE_DIR
    if not archive_dir.exists():
        return

    dates = sorted(
        [d.name for d in archive_dir.iterdir() if d.is_dir() and (d / "report.html").exists()],
        reverse=True
    )

    links = []
    for d in dates:
        links.append(f'    <li><a href="report/{d}/">{d}</a></li>')

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>三品王日报 · 历史索引</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 600px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }}
  h1 {{ color: #d35400; }}
  p.sub {{ color: #666; font-size: 14px; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ background: white; margin: 8px 0; padding: 12px 16px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  a {{ color: #2980b9; text-decoration: none; font-weight: 500; }}
  a:hover {{ text-decoration: underline; }}
  .note {{ color: #999; font-size: 13px; margin-top: 30px; }}
</style>
</head>
<body>
<h1>🍜 三品王每日品牌动态日报</h1>
<p class="sub">历史日报索引 · 共 {len(dates)} 期</p>
<ul>
{chr(10).join(links)}
</ul>
<p class="note">📦 日报永久存档于 <a href="https://github.com/RiiseKanon/sanpin-daily">GitHub</a>，可随时下载查看所有历史日报。</p>
</body>
</html>'''

    (serve_dir / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="三品王每日品牌动态日报")
    parser.add_argument("--date", type=str, default=None, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="试运行（不写入文件）")
    parser.add_argument("--monday-update", action="store_true", help="仅执行周一品牌库更新（不跑完整日报流程）")
    parser.add_argument("--export-searches", action="store_true", help="导出竞品搜索任务清单到 data/_search_tasks.json")
    args = parser.parse_args()

    # 配置日志
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
    )

    if args.monday_update:
        # 仅执行品牌库周更新，不走完整日报流程
        target_date = args.date or date.today().isoformat()
        logger.info(f"📋 品牌库周更新 — {target_date}")
        brief = _build_library_brief(target_date)
        if brief.get("empty"):
            logger.info("今天不是周一，跳过品牌库更新")
        else:
            logger.info(f"品牌库健康度: {brief.get('health','')}")
            pending = brief.get("pending_review")
            if pending:
                logger.info(f"待复核品牌: {', '.join(pending)}")
            # 发送钉钉通知
            try:
                from notifier.dingtalk import send_text_message
                msg = f"📋 三品王品牌库周更新 ({target_date})\n\n{brief.get('health','')}\n"
                if pending:
                    msg += f"\n⚠️ 待复核（低置信度）: {', '.join(pending)}"
                send_text_message(msg)
            except Exception as e:
                logger.warning(f"品牌库更新通知发送失败: {e}")
        sys.exit(0)

    if args.export_searches:
        # 仅导出搜索任务清单
        target_date = args.date or date.today().isoformat()
        _export_search_tasks(target_date)
        sys.exit(0)

    run_daily_job(target_date=args.date, dry_run=args.dry_run)
