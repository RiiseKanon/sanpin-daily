"""
来源综合可信指数校验模块

设计：
  综合指数 = 网站权威度(authority) × 消息内容可信度(content) / 100
  反映运营团队「对该动态的重视/认知程度」，而非单纯网站权威度。

  - 网站权威度：域名公信力（政府/央媒/官网高，排行榜/社媒低）
  - 消息内容可信度：消息性质（官方公告/媒体报道高；商业自评选榜单/UGC低）
  - 乘法融合：任一维度低 → 综合指数整体低
      例：CNPP十大品牌榜 → 60 × 55 = 33% → 低（自评选、权威性低，不太需要在意）

匹配优先级：
  1. URL 域名包含 SOURCE_DOMAIN_CREDIBILITY 关键词 → 取最长命中项
  2. 未命中 → 按 source 文本回退到 SOURCE_CREDIBILITY 类型权重
  3. 均无 → 未知(权威50 × 内容50 = 25, 低)
"""
from urllib.parse import urlparse
import config
from processor.content_signal import adjust_content


def _domain_of(url: str) -> str:
    """从 URL 中提取域名（小写，去端口/www.）"""
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc.lower()
        netloc = netloc.split(":")[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def is_login_gated(url: str) -> bool:
    """判断链接是否为登录墙 / 商家后台域名（非公开可读文章页）。

    如美团的 source.meituan.com（城市攻略宝）是商家内容平台入口，
    打开需美团/微信认证，不应作为可点击文章链接。
    """
    return _domain_of(url) in set(config.PROCESS_STRATEGY["login_gated_domains"])


def resolve_clickable_link(url: str, title: str = "") -> tuple:
    """解析卡片「可点击链接」与是否登录墙。

    返回 (link_url, gated)：
      - 非登录墙：link_url = 原 url，gated = False
      - 登录墙：link_url = 该主题的公开搜索兜底（避免点到登录墙），gated = True
    注意：原 url 始终保留用于可信度域名匹配，这里只决定「点什么」。
    """
    if is_login_gated(url):
        from urllib.parse import quote
        q = quote((title or "").strip() or "美团")
        return ("https://www.baidu.com/s?wd=" + q, True)
    return (url, False)


def assess_verifiability(url: str, title: str = "") -> dict:
    """评估事件是否「可引证」（拥有可点击核实的公开来源链接）。

    返回：
      verifiable  : 是否有可核实的公开链接（非登录墙）
      citation_url: 可引证链接（登录墙退化为标题搜索兜底，便于找公开佐证）
      gate_note   : 说明（登录墙场景）

    注：verifiable 只判断「链接能否核实」，最终是否「可引证」还需结合
        可信等级（见 processor 中 CITABLE_LEVELS 的判定）。
    """
    if is_login_gated(url):
        from urllib.parse import quote
        q = quote((title or "").strip() or "相关动态")
        return {
            "verifiable": False,
            "citation_url": "https://www.baidu.com/s?wd=" + q,
            "gate_note": "原始链接为登录墙，内容来自公开搜索片段，建议交叉核实",
        }
    return {
        "verifiable": True,
        "citation_url": url or "",
        "gate_note": "",
    }


def _level_of(composite: int) -> str:
    levels = config.PROCESS_STRATEGY["composite_level"]
    if composite >= levels["high_threshold"]:
        return "高"
    elif composite >= levels["mid_threshold"]:
        return "中"
    return "低"


# source 文本类型 → (权威度, 内容可信度) 回退映射（从 PROCESS_STRATEGY 读取）
_TYPE_FALLBACK = config.PROCESS_STRATEGY["fallback_types"]


def check_source_credibility(url: str, source_text: str = "", article_text: str = "") -> dict:
    """
    校验来源综合可信指数

    V2 增强：传入 article_text（标题+正文摘要）时，会基于正文信号动态微调
    content 分（披露方法论/赞助标签/自评选拉票等），同一域名不同文章得分不同。

    返回：
    {
        "authority": int,     # 网站权威度 0~100
        "content": int,       # 消息内容可信度 0~100（可能已被正文信号微调）
        "composite": int,     # 综合指数 = authority × content / 100
        "level": str,         # 高 / 中 / 低（基于 composite）
        "note": str,          # 说明（含正文信号微调明细）
        "matched": str,       # 命中的域名关键词或回退类型
        "signals": list,      # 命中的正文信号明细（无 article_text 时为空）
    }
    """
    domain = _domain_of(url)

    # 1) 按域名关键词匹配（最长优先）
    candidates = []
    for kw, auth, content, note in config.PROCESS_STRATEGY["domain_table"]:
        if kw and kw.lower() in domain:
            candidates.append((len(kw), kw, auth, content, note))
    if candidates:
        _, kw, auth, base_content, note = max(candidates, key=lambda x: x[0])
        return _finalize(url, kw, auth, base_content, note, article_text)

    # 2) 回退：按 source 文本匹配类型
    text = (source_text or "").strip()
    if text:
        for stype, (auth, content) in _TYPE_FALLBACK.items():
            if stype != "未知" and stype in text:
                note = f"来源类型「{stype}」估算（权威{auth}%×内容{content}%）"
                return _finalize(url, stype, auth, content, note, article_text)

    # 3) 未知
    auth, base_content = _TYPE_FALLBACK["未知"]
    note = "来源未知，建议交叉核实"
    return _finalize(url, "未知", auth, base_content, note, article_text)


def _finalize(url: str, matched: str, authority: int, base_content: int,
              note: str, article_text: str = "") -> dict:
    """统一收口：应用正文信号微调（V2），计算 composite 与等级。"""
    signals = []
    content = base_content
    if article_text and article_text.strip():
        sig = adjust_content(base_content, article_text)
        content = sig["adjusted_content"]
        signals = sig["hits"]
        if sig["note"]:
            note = (note + "；正文信号微调：" + sig["note"]
                    + f"（内容分 {sig['base_content']}→{sig['adjusted_content']}）")

    composite = round(authority * content / 100)
    return {
        "authority": authority, "content": content, "composite": composite,
        "level": _level_of(composite), "note": note, "matched": matched,
        "signals": signals,
    }


# 等级 → 徽章样式 Class（供模板使用）
LEVEL_BADGE_CLASS = {
    "高": "cred-high",
    "中": "cred-mid",
    "低": "cred-low",
}
