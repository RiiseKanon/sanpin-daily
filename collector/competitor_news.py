"""
Part 2 采集器: 竞品动态新闻搜索

采集策略（V2.4.2 重构）:
  1. 搜索代理模式: 百度对非浏览器 HTTP 请求返回安全验证页，直连搜索已不可用
     → collect() 导出搜索任务到 _search_tasks.json，由外部 Agent 使用 WebSearch 执行
     → 如果 _search_results.json 已存在且日期匹配，直接读取
  2. 智能查询词（V2.4.1）:
     - 长品牌名（≥4字）→ 纯品牌名搜索（足够独特）
     - 短品牌名（≤3字）→ 附加品类限定词（如"秋菊 螺蛳粉"），避免噪音
  3. 时效窗口: 7 天（超过 7 天的内容在 _inject_external_searches 中过滤）
  4. 硬性规则: 不满 5 条则必须搜完全部 201 个品牌，不允许提前终止
  5. 多层防线:
     - 搜索代理层: 时间过滤参数 + 要求 published_at 必须填写
     - 注入层: _inject_external_searches 做无日期/过期/旧年份三重过滤
     - 流水线层: _is_stale 对无日期内容不再放行
     - 相关性层: _filter_irrelevant 要求标题必须包含品牌名

搜索任务清单格式:
  {"name": "秋菊", "id": "BRxxx", "circle": "核心竞品", "query": "秋菊 螺蛳粉"}
"""
import asyncio
import httpx
import re
from datetime import date, timedelta
from loguru import logger
from bs4 import BeautifulSoup

from collector.base import BaseCollector, RawItem
from processor.dedup import extract_keywords
import config


class CompetitorNewsCollector(BaseCollector):
    """竞品动态新闻采集器 — 纯品牌名搜索 + 逐层扩展"""

    source_id = "competitor_news"
    source_name = "竞品动态"
    priority = 1
    circle_scope = ["核心竞品", "区域竞品", "场景竞品", "替代竞品"]

    TARGET_MIN_DYNAMICS = 5  # Part 2 最低5条目标
    MAX_BRANDS_PER_LAYER = 0  # 0 = 不限，搜完该层全部品牌

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)

    def build_search_tasks(self, target_date: str) -> list[dict]:
        """
        生成搜索任务清单
        每个品牌一个任务，query 由 _build_smart_query 智能生成：
        - 长品牌名（≥4字）：纯品牌名搜索
        - 短品牌名/常见词（≤3字）：自动附加品类词，避免噪音
        """
        all_brands = config.get_all_brands_by_circle()
        tasks = []

        layer_order = ["核心竞品", "区域竞品", "场景竞品", "替代竞品"]

        for circle in layer_order:
            brands = all_brands.get(circle, [])
            for brand in brands:
                tasks.append({
                    "name": brand["name"],
                    "id": brand["id"],
                    "circle": circle,
                    "query": self._build_smart_query(brand),
                    "searched": False,
                })

        smart_count = sum(1 for t in tasks if t["query"] != t["name"])
        logger.info(f"[竞品动态] 生成 {len(tasks)} 个搜索任务（{smart_count} 个附加品类词）")
        return tasks

    async def collect(self, target_date: str) -> list[RawItem]:
        """
        竞品动态采集 — 搜索代理模式（V2.4.2）

        百度对非浏览器 HTTP 请求返回安全验证页，直连搜索已不可用。
        改为纯搜索代理模式：
        1. collect() 导出搜索任务清单到 _search_tasks.json
        2. 外部 Agent 使用 WebSearch 工具执行搜索
        3. Agent 将结果写入 _search_results.json
        4. daily_job.py 的 _inject_external_searches() 读取并注入流水线

        注：如果 _search_results.json 已存在且日期匹配，直接读取返回。
        """
        import json as _json
        result_file = config.DATA_DIR / "_search_results.json"

        # 如果有已准备好的搜索结果，直接读取
        if result_file.exists():
            try:
                data = _json.loads(result_file.read_text(encoding="utf-8"))
                if data.get("target_date") == target_date:
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
                    logger.info(f"[竞品动态] 从 _search_results.json 读取 {len(items)} 条结果")
                    return items
            except Exception as e:
                logger.warning(f"[竞品动态] 读取搜索结果失败: {e}")

        # 导出搜索任务供外部 Agent 使用
        tasks = self.build_search_tasks(target_date)
        task_file = config.DATA_DIR / "_search_tasks.json"
        task_file.write_text(_json.dumps({
            "target_date": target_date,
            "total_tasks": len(tasks),
            "tasks": tasks,
            "instruction": (
                "对每个品牌的 query 使用 WebSearch 执行搜索。"
                "每个 query 搜 3-5 条相关新闻。"
                "⚠️ 时效要求：只收录近 7 天内发布的新闻，标题中含「2020年」「2021年」「2022年」「2023年」「2024年」"
                "或摘要无明显时效标识的旧闻一律丢弃。"
                "每条结果需包含: brand_name, brand_id, circle, title, url, summary, published_at(YYYY-MM-DD格式), category。"
                "published_at 无法确定具体日期的条目不要收录。"
                "将结果写入 data/_search_results.json。"
            ),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info(f"[竞品动态] 已导出 {len(tasks)} 个搜索任务到 _search_tasks.json（等待搜索代理执行）")
        return []

    async def _search_brand(self, brand: dict, target_date: str) -> list[RawItem]:
        """
        搜索单个品牌的百度新闻，解析结果。

        搜索词: 由 _build_smart_query 智能生成（短品牌名附加品类词）
        解析百度新闻搜索结果页，提取标题/链接/摘要/日期
        """
        items = []
        name = brand["name"]
        bid = brand.get("id", "")

        try:
            # 构建百度新闻搜索 URL（智能查询词）
            query = self._build_smart_query(brand)
            url = f"https://www.baidu.com/s?tn=news&word={query}&rtt={config.BAIDU_TIME_FILTER}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            resp = await self.client.get(url, headers=headers)

            if resp.status_code != 200:
                return items

            # 解析搜索结果
            news_items = self._parse_baidu_news(resp.text, name)

            for news in news_items:
                title = news.get("title", "")
                summary = news.get("summary", "")
                news_url = news.get("url", "")
                pub_date = news.get("date", target_date)

                # 跳过空标题或无实质内容
                if not title or len(title) < 5:
                    continue
                if len(summary) < 10 and len(title) < 15:
                    continue

                # 分类
                category = self._classify_title(title)

                item = RawItem(
                    source="新闻搜索",
                    url=news_url,
                    title=title,
                    content=summary,
                    published_at=pub_date,
                    brand_name=name,
                    brand_id=bid,
                    circle=brand["circle"],
                    category=category,
                    keywords=extract_keywords(title + " " + summary),
                    metadata={
                        "query": query,
                        "raw_name": name,
                        "source_type": "baidu_news",
                        "search_url": url,
                    },
                )
                items.append(item)

        except asyncio.TimeoutError:
            logger.debug(f"[{name}] 搜索超时")
        except Exception as e:
            logger.debug(f"[{name}] 搜索失败: {e}")

        return items

    def _parse_baidu_news(self, html: str, brand_name: str) -> list[dict]:
        """
        解析百度新闻搜索结果页。

        百度新闻搜索结果结构:
        - 每条结果通常在一个 div.result 或 div.c-container 中
        - 标题在 h3 > a 中
        - 摘要可能在 span.c-abstract 或 div.c-summary 中
        - 来源和日期在 span.c-author 或 div.c-info 中
        """
        results = []
        try:
            soup = BeautifulSoup(html, "html.parser")

            # 方法1: 查找所有 h3 标签（百度新闻搜索结果标题在 h3 中）
            for container in soup.find_all(["div", "li"], class_=re.compile(r"result|c-container|news-item")):
                try:
                    h3 = container.find("h3")
                    if not h3:
                        continue

                    link = h3.find("a")
                    if not link:
                        continue

                    title = link.get_text(strip=True)
                    href = link.get("href", "")

                    if not title or len(title) < 5:
                        continue

                    # 提取摘要
                    summary = ""
                    abstract_elem = container.find(["span", "div"], class_=re.compile(r"abstract|summary|content"))
                    if abstract_elem:
                        summary = abstract_elem.get_text(strip=True)
                    else:
                        # 尝试从容器文本中提取（排除标题和来源行）
                        full_text = container.get_text(separator=" ", strip=True)
                        # 去掉标题部分
                        if title in full_text:
                            remaining = full_text.replace(title, "", 1).strip()
                            # 取前200字符作为摘要
                            if len(remaining) > 20:
                                summary = remaining[:200]

                    # 提取来源和日期
                    pub_date = ""
                    source_info = container.find(["span", "div"], class_=re.compile(r"author|info|source|time"))
                    if source_info:
                        info_text = source_info.get_text(strip=True)
                        # 尝试提取日期
                        date_match = re.search(
                            r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)', info_text
                        )
                        if date_match:
                            pub_date = self._normalize_date(date_match.group(1))
                        # 尝试 "X小时前" / "X天前"
                        hours_match = re.search(r'(\d+)\s*小时前', info_text)
                        days_match = re.search(r'(\d+)\s*天前', info_text)
                        if hours_match:
                            from datetime import date as dt_date
                            pub_date = dt_date.today().isoformat()
                        elif days_match:
                            from datetime import date as dt_date, timedelta
                            d = int(days_match.group(1))
                            pub_date = (dt_date.today() - timedelta(days=d)).isoformat()

                    results.append({
                        "title": title,
                        "url": href,
                        "summary": summary[:300] if summary else "",
                        "date": pub_date,
                    })
                except Exception:
                    continue

            # 方法2: 如果方法1没有结果，尝试更简单的解析
            if not results:
                for h3 in soup.find_all("h3"):
                    link = h3.find("a")
                    if link:
                        title = link.get_text(strip=True)
                        href = link.get("href", "")
                        if title and len(title) >= 5 and brand_name in title:
                            results.append({
                                "title": title,
                                "url": href,
                                "summary": "",
                                "date": "",
                            })

            # 去重（按标题）
            seen = set()
            unique_results = []
            for r in results:
                key = r["title"][:30]
                if key not in seen:
                    seen.add(key)
                    unique_results.append(r)

            return unique_results[:5]  # 每个品牌最多5条

        except Exception as e:
            logger.debug(f"解析百度新闻失败 [{brand_name}]: {e}")
            return []

    def _normalize_date(self, date_str: str) -> str:
        """标准化日期格式为 YYYY-MM-DD"""
        # 2024年7月13日 → 2024-07-13
        m = re.match(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?', date_str)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""

    def _classify_title(self, title: str) -> str:
        """根据标题判断热点类别"""
        if any(w in title for w in ["新品", "新产品", "上市", "推出", "首发"]):
            return "新品牌/新产品出现"
        if any(w in title for w in ["活动", "促销", "优惠", "福利", "打折"]):
            return "高热度营销活动"
        if any(w in title for w in ["价格", "团购", "降价", "涨价", "调价"]):
            return "价格/促销/团购变动"
        if any(w in title for w in ["新店", "加盟", "扩张", "开店", "门店", "开业"]):
            return "渠道/扩张动作"
        if any(w in title for w in ["融资", "上市", "财报", "营收", "业绩", "利润"]):
            return "数据/业绩披露"
        if any(w in title for w in ["投诉", "卫生", "安全", "事故", "曝光", "查处", "罚款"]):
            return "舆情/食安风险"
        if any(w in title for w in ["供应链", "资本", "收购", "投资", "合并", "重组"]):
            return "组织/资本/供应链动态"
        return "行业趋势/政策"

    def _classify_category(self, query: str) -> str:
        """根据搜索词判断热点类别（保留向后兼容）"""
        return self._classify_title(query)

    # ========================================================================
    # 智能查询词构建（V2.4.1）
    # ========================================================================

    # 品牌名 ≤3 字 或 匹配以下常见词列表 → 附加品类限定词
    _GENERIC_NAME_PATTERNS = [
        "秋菊", "菊花", "春天", "夏天", "秋天", "冬天",
        "大碗", "小碗", "一口", "三品", "回味", "寻味",
        "好味", "美味", "品味", "香", "辣", "酸", "甜",
        "阳光", "彩虹", "蓝天", "白云", "星星", "月亮",
        "故乡", "家乡", "老家", "外婆", "妈妈",
    ]

    # 品牌品类映射 — 从品牌库 category 字段提取
    _CATEGORY_SUFFIX = {
        "米粉": ["米粉", "米线", "粉店"],
        "螺蛳粉": ["螺蛳粉"],
        "面馆": ["面馆", "面条", "拌面"],
        "快餐": ["快餐", "小吃", "餐饮"],
        "茶饮": ["茶饮", "奶茶", "咖啡"],
        "烘焙": ["烘焙", "面包", "甜点"],
        "酸辣粉": ["酸辣粉", "粉面"],
        "饺子": ["饺子", "馄饨"],
        "卤味": ["卤味", "卤菜"],
    }

    def _build_smart_query(self, brand: dict) -> str:
        """
        为品牌构建智能搜索查询词。

        策略：
        1. 品牌名 ≥4 字 → 纯品牌名（足够独特，不需附加词）
        2. 品牌名 ≤3 字 或 匹配常见词模式 → 附加品类限定词
           - 优先使用品牌库中的 category 字段
           - 回退到品牌名所在圈层的典型品类
        3. 附加格式: "品牌名 品类词"（如 "秋菊 螺蛳粉"）

        目的：减少因品牌名与常见词重叠而产生的噪音（如"秋菊"→花而非餐饮）
        """
        name = brand.get("name", "")
        if not name:
            return ""

        # 长品牌名（≥4字）足够独特，纯品牌名搜索
        if len(name) >= 4:
            return name

        # 短品牌名或常见词 → 附加品类限定词
        category = brand.get("category", "")

        # 先尝试精确匹配
        suffixes = self._CATEGORY_SUFFIX.get(category, [])
        if suffixes:
            return f"{name} {suffixes[0]}"

        # 复合品类（如 "米粉（螺蛳粉）"）→ 取括号内的核心品类
        if category:
            for key, vals in self._CATEGORY_SUFFIX.items():
                if key in category:
                    return f"{name} {vals[0]}"
            # 品类映射未命中但有 category 值 → 直接用 category 的第一个词
            main_cat = category.split("（")[0].split("(")[0].strip()
            if main_cat:
                return f"{name} {main_cat}"

        # 品牌库无 category，根据圈层推断
        circle = brand.get("circle", "")
        if circle in ("核心竞品", "区域竞品"):
            # 粉面米线类为主
            return f"{name} 米粉"
        elif circle == "场景竞品":
            return f"{name} 快餐"
        elif circle == "替代竞品":
            return f"{name} 餐饮"

        return name

    async def close(self):
        await self.client.aclose()
