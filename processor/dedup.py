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
