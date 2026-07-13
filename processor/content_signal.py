"""V2 内容信号分析

核心思路：同一域名的不同文章，应拿到不同的「内容可信度」分，而不是只看域名一刀切。
本模块对文章标题 + 摘要做**确定性信号提取**（中文关键词/正则，零外部依赖、可解释），
信号规则统一由 config.PROCESS_STRATEGY["content_signal"] 管理。

说明：当前采集到的是「正文摘要」(搜索片段级)，非全文。信号强弱受摘要丰富度影响；
若后续接入全文抓取，信号识别会更准。本层只对 content 分做*修正*，不会推翻域名权威度
（composite = authority × adjusted_content / 100，域名权威仍是主锚点）。
"""

from typing import Dict, List
import config

# content 分可调上下限（从 PROCESS_STRATEGY 读取）
_CFG = config.PROCESS_STRATEGY["content_signal"]
CONTENT_MIN, CONTENT_MAX = _CFG["bounds"]["min"], _CFG["bounds"]["max"]

# 信号规则：每组命中即计一次调整
SIGNAL_RULES: Dict[str, dict] = {
    rule["name"]: {"label": rule["label"], "keywords": rule["keywords"], "adjust": rule["adjust"]}
    for rule in _CFG["rules"]
}


def analyze_content_signals(text: str) -> Dict:
    """从文章文本提取内容可信度信号。

    返回:
        {
          "hits":    [{"signal","label","adjust","matched":[...]}],
          "delta":   int,             # 总调整量（已封顶在组内，最终再 clamp）
          "note":    str,             # 人类可读的信号说明（空串表示无信号）
          "applied": bool,            # 是否有正文可分析
        }
    """
    text = (text or "").strip()
    if not text:
        return {"hits": [], "delta": 0, "note": "", "applied": False}

    hits: List[dict] = []
    delta = 0
    for name, spec in SIGNAL_RULES.items():
        matched = [kw for kw in spec["keywords"] if kw in text]
        if matched:
            adj = spec["adjust"]
            delta += adj
            hits.append({
                "signal": name,
                "label": spec["label"],
                "adjust": adj,
                "matched": matched[:3],  # 最多展示 3 个命中词，避免 note 过长
            })

    note = "；".join(f"{h['label']}({h['adjust']:+d})" for h in hits) if hits else ""
    return {"hits": hits, "delta": delta, "note": note, "applied": True}


def adjust_content(base_content: int, text: str) -> Dict:
    """基于正文信号微调 content 分，返回调整后分值与信号明细。"""
    sig = analyze_content_signals(text)
    adjusted = max(CONTENT_MIN, min(CONTENT_MAX, base_content + sig["delta"]))
    sig["base_content"] = base_content
    sig["adjusted_content"] = adjusted
    return sig
