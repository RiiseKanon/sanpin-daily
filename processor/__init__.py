"""
处理流水线 — 串联去重→状态机→评级→聚合的完整流程
"""
from datetime import date, timedelta
from loguru import logger

from processor.dedup import match_event, compute_fingerprint_from_event, extract_keywords, find_semantic_duplicates, merge_duplicate_event
from processor.state_machine import EventStateMachine
from processor.rater import EventRater
from processor.aggregator import ReportAggregator
from processor.source_checker import (
    check_source_credibility, LEVEL_BADGE_CLASS,
    assess_verifiability, is_login_gated,
)
from storage.event_store import EventStore
import config


class ProcessingPipeline:
    """事件处理流水线"""

    def __init__(self, event_store: EventStore):
        self.store = event_store
        self.state_machine = EventStateMachine()
        self.rater = EventRater()
        self.aggregator = ReportAggregator()

    def process_raw_items(self, raw_items: list, today: str) -> dict:
        """
        处理原始采集条目 → 生成日报数据

        步骤:
        1. 将 RawItem 转为事件字典
        2. 与已有事件库匹配（去重/追踪）
        3. 更新事件状态
        4. 评级打分
        5. 聚合为日报结构
        """
        # Step 1: 转换为事件字典
        candidate_events = []
        stale_count = 0
        for item in raw_items:
            evt = self._raw_item_to_event(item, today)
            if evt:
                # 时效校验：丢弃超过时间窗口的内容
                if self._is_stale(evt, today):
                    stale_count += 1
                    continue
                candidate_events.append(evt)

        if stale_count > 0:
            logger.info(f"时效过滤: 丢弃 {stale_count} 条过期内容")

        # Step 1.5: 内容相关性过滤（V2.4.1）
        # 兜底防线：标题/摘要不含任何餐饮信号词的事件视为噪音，直接丢弃
        # 典型场景：品牌名与常见词重叠（如"秋菊"→花的新闻、"喜茶"→非茶饮内容）
        if candidate_events:
            candidate_events, noise_count = _filter_irrelevant(candidate_events)
            if noise_count > 0:
                logger.info(f"相关性过滤: 丢弃 {noise_count} 条不相关内容")

        if not candidate_events:
            logger.info("无新候选事件，直接聚合存量可见事件")

        # Step 2: 与已有事件库匹配
        existing_events = self.store.all()
        new_events = []
        updated_events = []

        for candidate in candidate_events:
            matched = match_event(candidate, existing_events, config.JACCARD_THRESHOLD)

            if matched:
                # 追踪更新：追加快照
                snapshot = {
                    "date": today,
                    "type": "UPDATE",
                    "title": candidate.get("title", ""),
                    "source": candidate.get("source", ""),
                    "delta": candidate.get("content", ""),
                }
                self.store.add_snapshot(matched["event_id"], snapshot)

                # 更新状态
                new_status = self.state_machine.on_new_hit(matched, today)
                old_score = matched.get("score", 0)
                new_score = candidate.get("score", 0)

                # 检查是否需要升级
                escalate = self.state_machine.on_score_change(matched, old_score, new_score)
                if escalate:
                    new_status = escalate

                # 构建 update 字典
                update_dict = {
                    "status": new_status,
                    "last_seen": today,
                    "score": max(old_score, new_score) if new_status != "ESCALATED" else new_score,
                }

                # V2.4.3: 同步 report_part，防止旧事件保留错误的路由归属
                # 场景：行业趋势/政策事件在 _raw_item_to_event 中已被正确设为 part_3，
                # 但旧事件可能保留了 part_2（来自早期版本的错误路由）。
                # 匹配更新时，以新候选的 report_part 为准覆盖旧值。
                candidate_part = candidate.get("report_part", "")
                if candidate_part and candidate_part != matched.get("report_part", ""):
                    logger.info(
                        f"report_part 修正: {matched['event_id']} "
                        f"\"{matched.get('report_part', '')}\" → \"{candidate_part}\""
                    )
                    update_dict["report_part"] = candidate_part

                self.store.update(matched["event_id"], update_dict)
                # 同步内存中的 matched 对象，确保后续聚合使用最新值
                matched["report_part"] = candidate_part

                updated_events.append(matched)
                logger.debug(f"事件追踪: {matched['event_id']} → {new_status}")
            else:
                # 新建事件
                event_id = self.store.generate_id(today)
                candidate["event_id"] = event_id
                candidate["fingerprint"] = compute_fingerprint_from_event(candidate)
                candidate["status"] = "NEW"
                candidate["first_seen"] = today
                candidate["last_seen"] = today
                candidate["snapshots"] = [{
                    "date": today,
                    "type": "NEW",
                    "title": candidate.get("title", ""),
                    "source": candidate.get("source", ""),
                    "delta": None,
                }]

                self.store.add(candidate)
                new_events.append(candidate)
                logger.info(f"新事件: {event_id} - {candidate.get('title', '')}")

        # Step 2.5: 跨类别语义去重（V2.4）
        # 在同品牌事件中，用标题/摘要文本相似度发现"同一消息不同归类"的重复事件
        # 重复事件合并到主事件中（追加 related_urls），不再作为独立事件展示
        all_stored = self.store.all()
        dup_groups = find_semantic_duplicates(all_stored, title_threshold=0.25)
        if dup_groups:
            removed_ids = set()
            for main_evt, dup_evt in dup_groups:
                dup_id = dup_evt.get("event_id", "")
                main_id = main_evt.get("event_id", "")
                if dup_id in removed_ids or main_id in removed_ids:
                    continue
                logger.info(
                    f"语义去重合并: [{main_evt.get('brand_name')}] "
                    f"\"{main_evt.get('title','')[:50]}...\" ← \"{dup_evt.get('title','')[:50]}...\""
                )
                merge_duplicate_event(main_evt, dup_evt)
                # 删除重复事件
                self.store.delete(dup_id)
                removed_ids.add(dup_id)
            if removed_ids:
                logger.info(f"语义去重: 合并 {len(removed_ids)} 条重复事件")

        # Step 3: 每日维护 — 检查冷却/关闭
        maintenance_updates = self.state_machine.daily_maintenance(
            self.store.all(), today
        )
        for event, new_status in maintenance_updates:
            self.store.update(event["event_id"], {"status": new_status})

        # Step 4: 聚合日报数据
        all_visible = [e for e in self.store.all()
                       if self.state_machine.is_visible_in_daily(e.get("status", ""))
                       and e.get("last_seen", "") >= self._window_start(today)]

        # V2.4.3: report_part 兜底修正 — 确保行业趋势/政策事件不会出现在竞品板块
        # 早期版本可能将行业趋势事件错误路由到 part_2，此处做最终校验
        for e in all_visible:
            if e.get("category") == "行业趋势/政策" and e.get("report_part") != "part_3":
                logger.info(f"report_part 兜底修正: {e['event_id']} "
                            f"\"{e.get('report_part', '')}\" → \"part_3\"")
                e["report_part"] = "part_3"

        # 重新评分所有可见事件
        for e in all_visible:
            self.rater.score_event(e)

        # 获取排名数据（从raw_items中提取Part4的内容）
        ranking_items = [item for item in raw_items
                        if getattr(item, 'category', '') == "每日排名"]

        report = self.aggregator.aggregate(all_visible, ranking_items, today)

        # 聚合阶段 _refresh_hero_images 会原地刷新 hero_image / source_credibility，
        # 这些改动不经过 add/update，需显式落盘，避免磁盘事件库数据滞后
        self.store.persist()

        # 附加统计
        report["pipeline_stats"] = {
            "candidates": len(candidate_events),
            "new_events": len(new_events),
            "updated_events": len(updated_events),
            "maintenance_updates": len(maintenance_updates),
            "total_events_in_store": self.store.total(),
        }

        return report

    def _raw_item_to_event(self, item, today: str) -> dict:
        """将 RawItem 转为事件字典"""
        # 跳过排名类条目
        if hasattr(item, 'category') and item.category == "每日排名":
            return None

        # 内容最低门槛：摘要过短或无实质内容的条目不生成事件
        content = getattr(item, 'content', '') or ''
        title = getattr(item, 'title', '') or ''
        if len(content) < 15 and len(title) < 15:
            logger.debug(f"内容过短，跳过: {title[:30]}")
            return None
        # 标题仅含"社媒提及"/"搜索"等空壳关键词且无实质摘要 → 跳过
        if ('社媒提及' in title or '社交媒体搜索' in content) and len(content) < 30:
            logger.debug(f"空壳社媒条目，跳过: {title[:30]}")
            return None

        # 行业趋势/政策类事件：自动归入 part_3（行业趋势），不应归入品牌自身/竞品
        category = getattr(item, 'category', '') or ''
        if category == "行业趋势/政策":
            report_part = "part_3"
        else:
            report_part = getattr(item, 'report_part', '') or ""

        # 提取关键词
        text = f"{item.title} {item.content}"
        keywords = item.keywords if item.keywords else extract_keywords(text)

        # 基础评分（根据类别预设）
        base_score = self._estimate_base_score(item)

        event = {
            "brand_name": item.brand_name,
            "brand_id": item.brand_id,
            "circle": item.circle,
            "category": item.category,
            "title": item.title,
            "summary": item.content[:150] if item.content else "",
            "keywords": keywords,
            "source": item.source,
            "url": item.url,
            "published_at": item.published_at,
            "hero_image": self._get_brand_image(item.brand_id),
            "base_score": base_score,
            "score": 0,
            "score_reason": "",
            "rating_type": "",
            "report_part": report_part,
            "metadata": item.metadata,
            # 来源链接可信度校验（V2：传入正文摘要做内容信号微调）
            "source_credibility": check_source_credibility(
                item.url, item.source, article_text=item.content),
        }

        # 引证分级（V2.2）：可核实链接 ∩ 中高可信 = 可引证；否则为「需核实线索」
        vf = assess_verifiability(item.url, item.title)
        cred_level = event["source_credibility"].get("level", "低")
        citable = vf["verifiable"] and cred_level in config.PROCESS_STRATEGY["citation"]["citable_levels"]
        event["verifiable"] = vf["verifiable"]
        event["citation_url"] = vf["citation_url"]
        event["citation_tier"] = "citable" if citable else "lead"
        event["link_gated"] = is_login_gated(item.url)
        event["gate_note"] = vf.get("gate_note", "")
        return event

    def _estimate_base_score(self, item) -> int:
        """根据类别和内容预估基础分"""
        category = item.category if hasattr(item, 'category') else ""
        circle = item.circle if hasattr(item, 'circle') else ""

        # 高风险类别
        if category in ("舆情/食安风险",):
            return -3 if circle == "自身品牌" else 3  # 自身负面 / 竞品负面(利好)

        # 扩张类
        if category in ("渠道/扩张动作", "新品牌/新产品出现"):
            return 3 if circle and circle != "自身品牌" else 2

        # 营销类
        if category in ("高热度营销活动", "价格/促销/团购变动"):
            return 2

        # 组织类
        if category in ("组织/资本/供应链动态", "数据/业绩披露"):
            return 1

        # 行业类
        if category == "行业趋势/政策":
            return 1

        return 0

    def _is_stale(self, event: dict, today: str) -> bool:
        """
        时效校验（流水线级别的兜底防线）— V2.4.2 强化

        确保不管数据来源如何，超过时间窗口的内容都会被过滤。
        对无法核实发布日期的内容不再放行。
        """
        published = event.get("published_at", "")
        title = event.get("title", "")

        # V2.4.2: 无日期 → 视为不可靠内容，直接丢弃（不再放行）
        if not published:
            logger.debug(f"时效过滤(无日期): {title[:40]}...")
            return True

        # 检查是否超过时间窗口
        try:
            pub_date = date.fromisoformat(published)
            target_date = date.fromisoformat(today)
            days_diff = (target_date - pub_date).days
        except (ValueError, TypeError):
            # V2.4.2: 日期格式异常 → 视为不可靠
            logger.debug(f"时效过滤(日期格式异常): {published}")
            return True

        if days_diff > config.TIME_WINDOW_DAYS:
            logger.debug(f"时效过滤: [{published}] {title[:40]}... ({days_diff}天前)")
            return True

        # V2.4.2: 未来日期 → 异常数据
        if days_diff < 0:
            logger.debug(f"时效过滤(未来日期): [{published}] {title[:40]}...")
            return True

        # 检查标题是否含过期关键词
        for kw in config.STALE_KEYWORDS:
            if kw in title:
                logger.debug(f"时效过滤(关键词): [{kw}] {title[:40]}...")
                return True

        return False

    def _get_brand_image(self, brand_id: str) -> str:
        """从品牌库获取品牌门店图片URL"""
        if not brand_id:
            return ""
        try:
            lib = config.load_brand_library()
            for b in lib.get("brands", []):
                if b.get("id") == brand_id:
                    return b.get("hero_image", "")
        except Exception:
            pass
        return ""

    def _window_start(self, today: str) -> str:
        """计算5天窗口的起始日期"""
        d = date.fromisoformat(today)
        return (d - timedelta(days=config.TIME_WINDOW_DAYS)).isoformat()

    def _empty_report(self, today: str) -> dict:
        """生成空日报"""
        return self.aggregator.aggregate([], [], today)


# ============================================================================
# 内容相关性过滤器（V2.4.1）
# ============================================================================

# 餐饮行业核心信号词 — 标题/摘要至少命中一个才视为相关内容
_FOOD_SIGNAL_WORDS = [
    # 品类词
    "米粉", "米线", "螺蛳粉", "酸辣粉", "面皮", "面条", "拌面", "汤面",
    "牛肉粉", "羊肉粉", "桂林米粉", "老友粉", "卤菜粉", "砂锅粉", "干捞粉",
    "快餐", "小吃", "餐饮", "餐厅", "饭店", "食堂", "外卖", "团购",
    "茶饮", "奶茶", "咖啡", "烘焙", "面包", "甜点",
    # 品牌动作词
    "新店", "开业", "加盟", "扩张", "连锁", "门店", "店面",
    "新品", "上市", "菜单", "口味", "食材", "供应链", "中央厨房",
    "融资", "上市", "营收", "业绩", "利润", "财报",
    "促销", "活动", "优惠", "折扣", "会员",
    # 食安监管词
    "食品", "卫生", "安全", "抽检", "超标", "投诉", "监管", "市场监督",
    "美团", "饿了么", "大众点评", "抖音团购", "百度外卖",
    # 行业词
    "螺蛳", "酸笋", "酸豆角", "卤蛋", "卤味", "叉烧", "牛肉", "锅烧",
    "嗦粉", "吃粉", "粉店", "粉面", "面馆",
    # 品牌/企业标识词
    "品牌", "公司", "企业", "创始人", "CEO", "总裁",
    # 广西地域 + 餐饮
    "南宁", "柳州", "桂林", "广西", "北海", "玉林", "贵港",
]

# 明确的非餐饮噪声词 — 命中任意一个且未命中上述信号词 → 丢弃
_NOISE_PATTERNS = [
    "电影", "演唱会", "综艺", "电视剧", "娱乐圈", "明星",
    "菊花", "花卉", "植物", "园林", "公园",
    "中秋", "重阳", "春节", "清明", "端午", "节令",
    "高铁", "铁路", "航班", "航线",
    "篮球", "足球", "运动会", "赛事",
    "房价", "楼盘", "地产",
    # V2.4.2 新增：搜索代理漏网噪声
    "设计之都", "价格违法", "北京市属公园", "文物中的花神",
    "第二十条", "法学三十年", "米粉大会", "凉拌粉",
]


def _filter_irrelevant(events: list[dict]) -> tuple[list[dict], int]:
    """
    过滤与餐饮行业不相关的事件（V2.4.2 增强）。

    规则：
    1. 标题必须包含品牌名或其核心字（≥2字匹配）→ 否则视为不相关
    2. 标题/摘要命中任一餐饮信号词 → 保留
    3. 命中噪声模式且无信号词 → 丢弃
    4. 既无信号也无噪声 → 保守保留
    """
    kept = []
    noise = 0

    for evt in events:
        title = evt.get("title", "") or ""
        summary = evt.get("summary", "") or ""
        brand = evt.get("brand_name", "") or ""
        text = f"{title} {summary}"

        # V2.4.2: 标题必须包含品牌名（≥2字匹配）
        # 排除品牌名过短（≤1字）和品牌名含特殊字符的情况
        if len(brand) >= 2:
            # 尝试品牌名的 2-gram 子串匹配
            brand_in_title = False
            for i in range(len(brand) - 1):
                bigram = brand[i:i+2]
                if bigram in title:
                    brand_in_title = True
                    break
            # 全名匹配（品牌名可能部分出现）
            if not brand_in_title and brand in title:
                brand_in_title = True

            if not brand_in_title:
                logger.debug(f"相关性过滤(标题不含品牌): [{brand}] {title[:50]}...")
                noise += 1
                continue

        # 命中任一餐饮信号词 → 保留
        has_signal = any(w in text for w in _FOOD_SIGNAL_WORDS)

        if has_signal:
            kept.append(evt)
            continue

        # 未命中信号词，检查是否命中噪声模式
        has_noise = any(w in text for w in _NOISE_PATTERNS)

        if has_noise:
            logger.debug(f"相关性过滤(噪声): [{brand}] {title[:50]}...")
            noise += 1
            continue

        # 既无信号也无噪声 → 保守保留（可能是新品类或罕见表述）
        kept.append(evt)

    return kept, noise
