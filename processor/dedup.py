"""
事件去重引擎 — 事件指纹计算 + Jaccard相似度匹配

机制：
1. 事件指纹：MD5(品牌名 + 热点类别 + 排序后Top5关键词) → 16位哈希
   用于快速精确匹配（O(1)查找）
2. Jaccard相似度：对指纹未匹配的事件，计算与已有事件的相似度
   品牌相同 + 类别相同 + 关键词集合Jaccard ≥ 70% → 视为同一事件
"""
import hashlib
import jieba
import jieba.analyse
from typing import Optional
from loguru import logger


def extract_keywords(text: str, top_k: int = 5) -> list[str]:
    """使用jieba提取关键词"""
    if not text or len(text.strip()) < 3:
        return []
    try:
        keywords = jieba.analyse.extract_tags(text, topK=top_k, withWeight=False)
        return keywords
    except Exception:
        # 降级：简单分词
        words = [w for w in jieba.cut(text) if len(w) > 1]
        return list(set(words))[:top_k]


def compute_fingerprint(brand_name: str, category: str, keywords: list[str]) -> str:
    """
    计算事件指纹
    公式: MD5(brand_name | category | keyword1 | keyword2 | ...) 前16位
    """
    sorted_kw = sorted(keywords)[:5]
    raw = f"{brand_name}|{category}|{'|'.join(sorted_kw)}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def compute_fingerprint_from_event(event: dict) -> str:
    """从事件字典直接计算指纹"""
    brand = event.get("brand_name", "")
    category = event.get("category", "")
    keywords = event.get("keywords", [])
    return compute_fingerprint(brand, category, keywords)


def jaccard_similarity(set_a: set, set_b: set) -> float:
    """
    计算Jaccard相似度
    J(A, B) = |A ∩ B| / |A ∪ B|
    """
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def build_event_set(event: dict) -> set:
    """从事件构建用于Jaccard比较的集合"""
    elements = set()
    # 品牌名
    elements.add(event.get("brand_name", ""))
    # 类别
    elements.add(event.get("category", ""))
    # 关键词
    for kw in event.get("keywords", []):
        elements.add(kw)
    return elements


def match_event(
    new_event: dict,
    existing_events: list[dict],
    threshold: float = 0.7,
) -> Optional[dict]:
    """
    将新事件与已有事件库匹配
    返回匹配到的已有事件，或None（表示新事件）

    匹配策略：
    1. 先用指纹精确匹配（O(1)）
    2. 指纹不匹配时，用Jaccard相似度匹配同品牌+同类别的事件
    """
    fp = new_event.get("fingerprint") or compute_fingerprint_from_event(new_event)

    # Step 1: 指纹精确匹配
    for e in existing_events:
        if e.get("fingerprint") == fp:
            logger.debug(f"指纹精确匹配: {new_event.get('title')} → {e.get('title')}")
            return e

    # Step 2: Jaccard相似度匹配（仅同品牌+同类别）
    new_set = build_event_set(new_event)
    new_brand = new_event.get("brand_name", "")
    new_cat = new_event.get("category", "")

    best_match = None
    best_score = 0.0

    for e in existing_events:
        # 必须是同品牌
        if e.get("brand_name") != new_brand:
            continue
        # 必须是同类别
        if e.get("category") != new_cat:
            continue

        existing_set = build_event_set(e)
        score = jaccard_similarity(new_set, existing_set)

        if score >= threshold and score > best_score:
            best_match = e
            best_score = score

    if best_match:
        logger.debug(f"Jaccard匹配 (score={best_score:.2f}): {new_event.get('title')} → {best_match.get('title')}")

    return best_match


def deduplicate_items(
    raw_items: list[dict],
    existing_events: list[dict],
    threshold: float = 0.7,
) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """
    对原始采集条目进行去重和事件匹配

    返回:
    - new_events: 新事件列表（需要新建的事件）
    - matched_pairs: 匹配对列表 [(新条目, 匹配到的已有事件), ...]
    """
    new_events = []
    matched_pairs = []

    for item in raw_items:
        matched = match_event(item, existing_events, threshold)
        if matched:
            matched_pairs.append((item, matched))
        else:
            new_events.append(item)

    logger.info(f"去重结果: {len(raw_items)} 条 → {len(new_events)} 新事件 + {len(matched_pairs)} 匹配更新")
    return new_events, matched_pairs


def find_semantic_duplicates(
    events: list[dict],
    title_threshold: float = 0.25,
) -> list[tuple[dict, dict]]:
    """
    跨类别语义去重：在同品牌事件中，对标题和摘要做字符级 Jaccard 相似度匹配。

    与 match_event 不同：
    - match_event 要求同品牌 + 同类别 + 关键词 ≥ 70% → 追踪更新
    - 本函数不限制类别，只用标题/摘要文本的字符重叠度来判断「同一消息」

    典型场景：两个来源报道同一事件，但分类不同（如一条归为"行业趋势"，
    另一条归为"组织/资本/供应链动态"），应合并为一条动态并列出多来源链接。

    返回:
    - duplicate_groups: [(主事件, 重复事件), ...] 每组中保留先出现的事件为主事件
    """
    if len(events) < 2:
        return []

    groups = []
    # 按品牌分组
    by_brand: dict[str, list[tuple[int, dict]]] = {}
    for idx, e in enumerate(events):
        brand = e.get("brand_name", "") or ""
        by_brand.setdefault(brand, []).append((idx, e))

    for brand, indexed in by_brand.items():
        if len(indexed) < 2:
            continue
        n = len(indexed)
        for i in range(n):
            for j in range(i + 1, n):
                idx_a, evt_a = indexed[i]
                idx_b, evt_b = indexed[j]

                # 已经是同一类别的不重复处理（交给 match_event）
                if evt_a.get("category") == evt_b.get("category"):
                    continue

                # 构建文本集合（标题 + 摘要的字符 trigram）
                text_a = _build_text_set(evt_a)
                text_b = _build_text_set(evt_b)

                if not text_a or not text_b:
                    continue

                sim = jaccard_similarity(text_a, text_b)
                if sim >= title_threshold:
                    # 保留 idx 小的为主事件
                    if idx_a < idx_b:
                        groups.append((evt_a, evt_b))
                    else:
                        groups.append((evt_b, evt_a))

    return groups


def _build_text_set(event: dict) -> set:
    """从事件的标题和摘要构建字符 trigram 集合，用于语义去重。

    策略：摘要包含核心信息，权重更高。将摘要中的关键词提取后
    以多份形式加入 gram 集合，放大核心信息的重叠度。
    """
    title = event.get("title", "") or ""
    summary = event.get("summary", "") or ""

    # 提取核心关键词（品牌名、动作词等长词）
    import re
    # 从摘要中提取中文长词（4字以上）
    long_words = set(re.findall(r'[\u4e00-\u9fff]{4,}', summary))
    # 品牌名始终加入
    brand = event.get("brand_name", "")
    if brand:
        long_words.add(brand)

    # 构建基础文本（标题1份 + 摘要3份 = 摘要权重更高）
    text = f"{title} {summary} {summary}"

    grams = set()
    for length in (2, 3):
        for i in range(len(text) - length + 1):
            grams.add(text[i:i + length])

    # 将核心关键词的 2-gram 也加入（这些是语义重叠的关键信号）
    for word in long_words:
        for length in (2, 3):
            for i in range(len(word) - length + 1):
                grams.add(f"KW:{word[i:i + length]}")

    return grams


def merge_duplicate_event(main_event: dict, dup_event: dict) -> dict:
    """
    将重复事件合并到主事件中。

    合并策略:
    - 追加 dup_event 的 URL 到 main_event 的 related_urls 列表
    - 如果 dup_event 的摘要更长，替换主事件摘要
    - 保留主事件的类别、评分等核心字段不变
    - 如果 dup_event 的可信度更高，升级主事件的 citation_tier
    """
    import re

    def _source_label(event: dict) -> str:
        """从 URL 提取来源简称"""
        url = event.get("url", "")
        source = event.get("source", "")
        if source and source not in ("新闻搜索", "AI搜索代理", ""):
            return source
        # 从域名提取
        m = re.search(r'https?://(?:www\.)?([^/]+)', url)
        if m:
            domain = m.group(1)
            # 知名域名映射
            domain_map = {
                "bjnews.com.cn": "新京报",
                "sohu.com": "搜狐",
                "yule.sohu.com": "搜狐娱乐",
                "sina.com.cn": "新浪",
                "news.sina.cn": "新浪新闻",
                "qq.com": "腾讯",
                "news.qq.com": "腾讯新闻",
                "163.com": "网易",
                "ifeng.com": "凤凰网",
                "ishare.ifeng.com": "凤凰网",
                "toutiao.com": "今日头条",
                "weibo.com": "微博",
                "douyin.com": "抖音",
                "mp.weixin.qq.com": "微信公号",
                "chinastarmarket.cn": "科创板日报",
                "baidu.com": "百度",
                "baijiahao.baidu.com": "百家号",
                "xhby.net": "新华报业",
                "tech.china.com": "中国网科技",
                "canyin88.com": "红餐网",
            }
            return domain_map.get(domain, domain)
        return "未知来源"

    # 初始化 related_urls
    if "related_urls" not in main_event or not main_event["related_urls"]:
        main_event["related_urls"] = []
        main_url = main_event.get("url", "")
        if main_url:
            main_event["related_urls"].append({
                "url": main_url,
                "source": _source_label(main_event),
                "title": main_event.get("title", ""),
            })

    # 追加重复事件的 URL
    dup_url = dup_event.get("url", "")
    if dup_url and dup_url not in [r["url"] for r in main_event["related_urls"]]:
        main_event["related_urls"].append({
            "url": dup_url,
            "source": _source_label(dup_event),
            "title": dup_event.get("title", ""),
        })

    # 如果重复事件摘要更长，替换
    main_summary = main_event.get("summary", "") or ""
    dup_summary = dup_event.get("summary", "") or ""
    if len(dup_summary) > len(main_summary):
        main_event["summary"] = dup_summary

    # 如果重复事件可信度更高，升级 citation_tier
    if dup_event.get("citation_tier") == "citable" and main_event.get("citation_tier") != "citable":
        main_event["citation_tier"] = "citable"

    return main_event
