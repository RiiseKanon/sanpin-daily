"""
三品王每日品牌动态日报 — 全局配置
所有可调参数集中管理，从 brand_library.json 读取品牌库配置
"""
import os
import json
from pathlib import Path

# === 路径配置 ===
PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
ARCHIVE_DIR = DATA_DIR / "archive"
TEMPLATE_DIR = PROJECT_ROOT / "generator" / "templates"

# 品牌库路径（优先环境变量，其次项目根目录，最后 /workspace）
BRAND_LIBRARY_PATH = Path(os.environ.get("BRAND_LIBRARY_PATH", "")) if os.environ.get("BRAND_LIBRARY_PATH") else None
if BRAND_LIBRARY_PATH is None:
    candidates = [
        PROJECT_ROOT / "brand_library.json",
        Path("/workspace/brand_library.json"),
    ]
    for p in candidates:
        if p.exists():
            BRAND_LIBRARY_PATH = p
            break
    else:
        BRAND_LIBRARY_PATH = PROJECT_ROOT / "brand_library.json"  # 兜底

# 事件库路径
EVENTS_PATH = DATA_DIR / "events.json"

# 输出路径
LATEST_HTML_PATH = OUTPUT_DIR / "latest.html"

# === 采集参数 ===
TIME_WINDOW_DAYS = 14  # 采集覆盖近14天内容
MAX_RETRIES = 3        # Part4排名采集最大重试次数
RETRY_INTERVAL = 30    # 重试间隔（秒）

# === 去重参数 ===
JACCARD_THRESHOLD = 0.7  # Jaccard相似度阈值

# === 状态机参数 ===
ACTIVE_AFTER_DAYS = 1      # 再次命中即升级为ACTIVE
COOLING_AFTER_DAYS = 3     # N天无命中 → COOLING
CLOSE_AFTER_DAYS = 7       # COOLING后N天 → CLOSED
ESCALATE_SCORE_DELTA = 2   # 评分变动≥±2 → ESCALATED

# === 评级参数 ===
RATING_RANGE = (-5, 5)

# 品牌圈层权重（核心竞品权重最高）
CIRCLE_WEIGHTS = {
    "核心竞品": 1.5,
    "区域竞品": 1.2,
    "场景竞品": 1.0,
    "替代竞品": 0.8,
    "自身品牌": 2.0,
}

# 时效性权重（按事件类别区分）
# 短期/中性事件（促销、数据披露、舆情）：越近权重越高，随时间快速衰减
# 长期影响事件（扩张、品牌升级、资本/供应链）：14天窗口内权重恒为1
SHORT_TERM_CATEGORIES = {
    "高热度营销活动", "价格/促销/团购变动", "数据/业绩披露",
    "舆情/食安风险",
}
LONG_TERM_CATEGORIES = {
    "渠道/扩张动作", "新品牌/新产品出现", "门店运营升级",
    "组织/资本/供应链动态",
}

def get_freshness_weight(days_ago: int, category: str = "") -> float:
    """根据事件类别返回时效权重"""
    if category in LONG_TERM_CATEGORIES:
        return 1.0  # 长期事件：14天窗口内权重不变
    # 短期/中性事件：随时间衰减
    if days_ago <= 0:
        return 1.0
    elif days_ago <= 1:
        return 0.9
    elif days_ago <= 2:
        return 0.8
    elif days_ago <= 3:
        return 0.7
    else:
        return 0.5

# 来源可信度权重（按来源类型，用于事件评分加权）
SOURCE_CREDIBILITY = {
    "官方": 1.0,
    "行业媒体": 0.9,
    "本地媒体": 0.85,
    "社交媒体": 0.7,
    "平台排名": 0.8,
    "未知": 0.5,
}

# ============================================================================
# 流程策略 SPEC：来源可信度 + 内容信号 + 引证分级（V1 → V2 → V2.2）
# 所有规则集中管理，processor/source_checker 与 processor/content_signal 据此执行
# ============================================================================
PROCESS_STRATEGY = {
    # ——— 核心公式 ———
    # 综合指数(composite) = 网站权威度(authority) × 消息内容可信度(content) / 100
    # 乘法融合：任一维度低 → 综合指数整体低，域名权威是主锚点，正文信号只做修正
    "formula": "composite = authority × content / 100",

    # ——— 等级阈值 ———
    "composite_level": {
        "high_threshold": 70,   # >=70 → 高
        "mid_threshold": 45,    # 45~69 → 中；<45 → 低
    },

    # ——— 匹配优先级（check_source_credibility） ———
    # 1) URL 域名包含 domain_table 关键词 → 取最长命中项
    # 2) 未命中 → 按 source 文本匹配 fallback_types
    # 3) 均无 → unknown(50×50=25, 低)
    "matching": {
        "priority": ["domain_longest", "source_text_fallback", "unknown"],
        "unknown_authority": 50,
        "unknown_content": 50,
    },

    # ——— 回退类型（source 文本 → (权威, 内容)） ———
    "fallback_types": {
        "官方":     (90, 92),
        "行业媒体": (72, 75),
        "本地媒体": (75, 74),
        "社交媒体": (55, 52),
        "平台排名": (63, 60),
        "未知":     (50, 50),
    },

    # ——— 域名权威基准表 ———
    # 每项：(域名关键词, 网站权威度%, 消息内容可信度%, 说明)
    "domain_table": [
        # 政府 / 行业协会 / 品牌官网（权威度高，官方发布内容可信度高）
        (".gov.cn",              95, 92, "政府官方网站，权威发布"),
        ("cppc.com.cn",          88, 90, "中国烹饪协会官方网站，权威公告"),
        ("chinapp.com",          85, 88, "中国餐饮行业协会相关"),
        ("mcdonalds.com.cn",     88, 92, "麦当劳中国官方网站，官方动态"),
        ("nestle.com.cn",        88, 92, "雀巢中国官方网站，官方动态"),
        ("chinanews.com.cn",     88, 86, "中国新闻网，中央重点新闻网站"),
        ("xfrb.com.cn",          75, 75, "消费日报网，行业媒体"),
        # 中央 / 主流媒体（权威度高，新闻报道内容可信度高）
        ("people.com.cn",        92, 88, "人民网，中央重点新闻网站"),
        ("xinhuanet.com",        93, 88, "新华网，国家通讯社主办"),
        ("cctv.com",             90, 88, "央视网，权威媒体报道"),
        # 省级官方媒体（权威度高，省级广播电视台/党报主办）
        ("gxtv.cn",              88, 86, "广西广播电视台，省级官方媒体，权威报道"),
        ("gxnews.com.cn",        86, 85, "广西新闻网，省级重点新闻网站"),
        ("sina.com.cn",          82, 83, "新浪网，主流门户新闻报道"),
        ("qq.com",               80, 85, "腾讯网，主流门户新闻报道"),
        ("163.com",              78, 80, "网易，主流门户新闻报道"),
        ("ifeng.com",            78, 80, "凤凰网，新闻报道"),
        ("sohu.com",             60, 78, "搜狐，门户资讯"),
        # 顶级权威研究机构（招股书 / 上市公司引用级，方法论透明 → 高 80+）
        ("frostchina.com",       90, 90, "弗若斯特沙利文(Frost&Sullivan)：全球增长咨询，报告广泛引用于港股/A股招股书"),
        ("euromonitor.com",      90, 90, "欧睿(Euromonitor)：全球消费市场数据库，方法论透明、被国际机构引用"),
        ("nielsen",              90, 89, "尼尔森IQ(NielsenIQ)：全球消费者与零售监测权威，数据驱动"),
        ("cninsights.com",       90, 89, "灼识咨询(CIC)：投融资全流程专业咨询，'行业第一股'经验领先，招股书常用"),
        # 一线研究机构（国内权威，方法论较透明 → 高 70+）
        ("iimedia.cn",           86, 86, "艾媒咨询(iMedia)：行业研究机构，报告有方法论披露，常被媒体引用"),
        ("iresearch.com.cn",     86, 86, "艾瑞咨询(iResearch)：行业研究机构，数据模型成熟，国内广泛引用"),
        # 行业垂直媒体（编辑独立 → 中）
        ("canyin88.com",         76, 78, "红餐网，餐饮头部垂直媒体（'内容+数据+活动'产业平台）"),
        ("watcn.com",            74, 76, "餐饮老板内参，餐饮垂直媒体（获吴晓波/源码资本/美团点评投资）"),
        ("naixi.com",            68, 72, "窄门餐眼，餐饮数据平台"),
        ("deliveryinsight.com",  70, 72, "掌柜参谋/外卖行业分析"),
        # 聚合资讯平台
        ("toutiao.com",          55, 70, "今日头条，聚合资讯平台"),
        ("yidianzixun.com",      52, 68, "一点资讯，聚合平台"),
        # 本地生活 / 电商平台（含用户主观评价 → 中低）
        ("meituan.com",          65, 62, "美团，本地生活平台（含用户评价，主观性强）"),
        ("dianping.com",         62, 58, "大众点评，用户主观评价"),
        ("ele.me",               62, 60, "饿了么，本地生活平台"),
        # 工商 / 企业数据平台（权威度中高，数据来自工商登记）
        ("aiqicha.baidu.com",    78, 80, "爱企查/企查查，工商数据平台（数据来自官方工商登记）"),
        ("qcc.com",              78, 80, "企查查，工商数据平台"),
        ("tianyancha.com",       78, 80, "天眼查，工商数据平台"),
        # 纯自评选 / 招商推广榜（权威度中低 + 自评选内容可信度低 → 低）
        ("cnpp.cn",              60, 55, "十大品牌网(CNPP)：网站权威度低，且为商业自评选榜单，缺乏独立背书"),
        ("hzcnpp.com",           58, 53, "CNPP 家族自评选榜站（域名含cnpp），商业榜单属性，缺乏独立背书"),
        ("cn10.cn",              58, 53, "CN10排行：商业榜单站，自评选+招商推广"),
        ("maigoo.com",           62, 55, "买购网(Maigoo)：行业榜单，商业属性强"),
        ("top10.com.cn",         55, 52, "十大排行榜类站点，商业推广属性"),
        ("phb123.com",           55, 52, "排行榜123网，商业榜单"),
        # 社交媒体 / UGC 内容平台（中低可信，主观性强）
        ("weibo.com",            55, 52, "微博，社交平台 UGC 内容"),
        ("douyin.com",           68, 68, "抖音（商家号团购/促销为事实性内容，非随机UGC）"),
        ("xiaohongshu.com",      50, 48, "小红书，种草社区 UGC 内容主观性强"),
        ("baidu.com",            68, 70, "百度搜索聚合（含文库/百科等结构化内容，与百家号碎片不同）"),
        ("zhihu.com",            60, 55, "知乎，问答社区 UGC 内容"),
        ("tieba.baidu.com",      45, 45, "百度贴吧，论坛讨论主观性强"),
        ("douban.com",           50, 48, "豆瓣，UGC 社区"),
    ],

    # ——— 商业榜单/研究机构分层（供人工审核与 AI 信号判定参考） ———
    # 关键原则：并非所有榜单都低
    "ranking_tiers": {
        "顶级权威研究机构": {
            "authority_range": (88, 90), "content_range": (88, 90),
            "example": "弗若斯特沙利文 / 尼尔森 / 欧睿 / 灼识（招股书引用级）",
            "composite": "高(80+)",
            "signals": "方法论透明、被上市公司招股书 / 国际机构引用",
        },
        "一线研究机构": {
            "authority_range": (84, 87), "content_range": (84, 87),
            "example": "艾媒咨询 / 艾瑞咨询",
            "composite": "高(70+)",
            "signals": "有方法论披露、国内广泛引用",
        },
        "行业媒体/协会榜": {
            "authority_range": (70, 78), "content_range": (72, 78),
            "example": "红餐网 / 餐饮老板内参 / 行业协会",
            "composite": "中",
            "signals": "编辑独立、垂直专业，但非一手数据",
        },
        "平台数据榜": {
            "authority_range": (60, 68), "content_range": (55, 65),
            "example": "美团必吃榜 / 大众点评 / 抖音热榜",
            "composite": "中低",
            "signals": "数据驱动、体量大，但可被刷单 / 算法操纵",
        },
        "纯自评选招商榜": {
            "authority_range": (55, 62), "content_range": (50, 55),
            "example": "CNPP / 买购 / CN10 / 排行榜123",
            "composite": "低",
            "signals": "自报名 / 付费、无方法论、招商属性强",
        },
    },

    # ——— 内容信号分析（V2：正文关键词微调 content 分） ———
    # 设计原则：域名权威是主锚点，正文信号只做修正（不推翻域名权威）
    # 零外部依赖：纯中文关键词匹配，无需 LLM API 调用
    "content_signal": {
        "bounds": {"min": 10, "max": 98},  # content 分可调上下限
        "note": "当前基于搜索摘要（非全文），信号强弱受摘要丰富度影响",
        "rules": [
            {"name": "methodology_disclosed", "label": "披露方法论/数据来源",
             "keywords": ["评选标准","评选维度","评选方法","评选体系","评价体系",
                          "基于问卷","份问卷","样本量","样本覆盖","调研显示",
                          "数据来源","据调研","覆盖城市","指标体系","评分模型",
                          "综合评分","统计口径","权重","大数据"],
             "adjust": +8},
            {"name": "independent_endorsed", "label": "独立背书/权威引用",
             "keywords": ["招股书","上市文件","官方认证","行业协会","据媒体报道",
                          "援引","被引用","第三方机构","权威机构认定","工商数据",
                          "年报","官方数据"],
             "adjust": +6},
            {"name": "official_announce", "label": "官方正式发布",
             "keywords": ["正式宣布","官方宣布","郑重声明","正式发布","官方回应","确权"],
             "adjust": +6},
            {"name": "sponsored", "label": "含赞助/商业合作标签",
             "keywords": ["赞助","商业合作","推广","广告","特约","冠名","品牌展示",
                          "合作媒体","推广位","商务合作","定制","投放"],
             "adjust": -12},
            {"name": "self_nomination", "label": "自评选/招商拉票",
             "keywords": ["网友投票","全民票选","自荐","报名通道","投票通道","招商",
                          "入驻申请","榜单征集","海选","打榜","拉票","助力","点赞投票"],
             "adjust": -15},
            {"name": "rumor_tone", "label": "网传/推测语气",
             "keywords": ["网传","据称","疑似","可能","传言","网曝","小道消息",
                          "听说","外媒称","有消息称"],
             "adjust": -8},
        ],
    },

    # ——— 引证分级（V2.2） ———
    # 可引证 = 有可核实公开链接(非登录墙) ∩ 可信等级∈citable_levels
    # 不满足者归入「需核实线索」（仅作跟进线索，不用于决策引证）
    "citation": {
        "citable_levels": ("高", "中"),
        "lead_block_title": "需核实线索",
        "lead_block_note": "以下动态来源不可核实或可信度低，仅作跟进线索，请勿直接用于决策引证。",
        "lead_badge": "🔍 需核实",
        "citable_badge": "✅ 可引证",
        "login_gated_badge": "🔒 需登录",
        # V2.3.1：行业趋势/政策类 + 低可信 → 不进入任何板块（连线索都不是）
        "discard_low_credibility_industry": True,
    },

    # ——— 焦点品牌策略（V2.3） ———
    # 取消近期动态罗列（品牌库 notes 碎片化、时效性不足），改为：
    #   · 基础资料以带背景色块的卡片行展示（视觉层次更清晰）
    #   · 新增 AI 竞争态势分析：基于品牌库字段（品类/价格带/门店数/总部/区域/notes/level）
    #     通过 LLM（Ollama qwen2.5:7b）或规则引擎生成三段式分析：
    #       1) 品牌定位与规模  2) 与三品王竞争关系  3) 跟进建议
    "focus_brand": {
        "ai_model": "qwen2.5:7b",
        "ai_timeout": 90,
        "analysis_sections": ["品牌定位与规模", "经营方式与出品特点", "资本参与与扩张路径"],
        "fallback_engine": "规则引擎（基于品牌库字段的结构化推理，聚焦品牌自身背景不重复竞争分析）",
        "show_recent_news": False,  # V2.3 取消近期动态
    },

    # ——— 评级体系（V2.3.2 优化） ———
    # 评分公式: final = clamp(round(base × 圈层权重 × 时效权重), -5, 5)
    # 可信度是独立指标，不参与威胁/利好评分加权（通过可信徽章和引证分级呈现）
    "rating": {
        "formula": "clamp(round(base × circle_weight × freshness_weight), -5, 5)",
        "range": [-5, 5],
        "base_score_by_category": {
            # 竞品视角：扩张/新品=威胁大 → +3；自身品牌同样动作=利好 → +2
            "渠道/扩张动作":       {"competitor": 3, "self": 2},
            "新品牌/新产品出现":    {"competitor": 3, "self": 2},
            "高热度营销活动":       {"competitor": 2, "self": 2},
            "价格/促销/团购变动":   {"competitor": 2, "self": 2},
            "组织/资本/供应链动态":  {"competitor": 1, "self": 1},
            "数据/业绩披露":        {"competitor": 1, "self": 1},
            "行业趋势/政策":        {"competitor": 1, "self": 1},
            "门店运营升级":         {"competitor": 2, "self": 2},
            "舆情/食安风险":        {"competitor": 3, "self": -3},  # 自身负面=利空
        },
        "circle_weights": {
            "自身品牌": 2.0, "核心竞品": 1.5, "区域竞品": 1.2,
            "场景竞品": 1.0, "替代竞品": 0.8,
        },
        # 时效权重：按事件类别区分
        #   长期事件(扩张/品牌升级/资本/供应链)：14天窗口内权重恒为1
        #   短期事件(促销/数据披露/舆情)：随时间衰减
        "freshness": {
            "long_term_categories": [
                "渠道/扩张动作", "新品牌/新产品出现", "门店运营升级",
                "组织/资本/供应链动态",
            ],
            "short_term_categories": [
                "高热度营销活动", "价格/促销/团购变动", "数据/业绩披露",
                "舆情/食安风险", "行业趋势/政策",
            ],
            "long_term_weight": 1.0,  # 14天内恒为1
            "short_term_decay": {
                "0天": 1.0, "1天": 0.9, "2天": 0.8, "3天": 0.7, "4天+": 0.5,
            },
        },
        "score_labels": {
            5: "极强", 3: "明显", 1: "轻微", 0: "中性", -1: "轻微", -3: "明显", -5: "重大",
        },
        "color_rules": {
            "威胁评级": {"≥3": "#e53e3e(红)", "1~2": "#ed8936(橙)", "0": "#a0aec0(灰)", "-1~-2": "#68d391(浅绿)", "≤-3": "#38a169(绿)"},
            "正向/利好评级": {"≥3": "#38a169(绿)", "1~2": "#68d391(浅绿)", "0": "#a0aec0(灰)", "-1~-2": "#ed8936(橙)", "≤-3": "#e53e3e(红)"},
        },
        "note": "颜色镜像设计：威胁评级红=坏事，正向评级红=坏事；同一分数在不同评级维度颜色相反",
    },

    # 非公开文章页（打开需认证），不应作为可点击文章链接
    # 命中后：可点击链接改用搜索兜底，原 url 仍用于可信度域名匹配
    "login_gated_domains": [
        "source.meituan.com",   # 美团商家/城市内容平台（城市攻略宝），noindex，需登录
    ],

    # ——— 数据质量门禁（V2.3.1） ———
    # 日报不是垃圾桶：以下规则在采集→处理→聚合全链路生效
    "data_quality": {
        # ① 最低内容门槛：摘要/标题过短或为空壳（如"社媒提及:XX"）→ 丢弃
        "min_summary_length": 15,
        "min_title_length": 15,
        "reject_empty_shell": True,
        "shell_keywords": ["社媒提及", "社交媒体搜索"],
        # ② 时效窗口：published_at 距今超过 N 天的内容 → 丢弃
        #    采集器用 CONTENT_FRESHNESS_DAYS(5天)，流水线用 TIME_WINDOW_DAYS(14天)兜底
        #    无法核实发布时间的内容 → 视为不可靠，不进入日报
        "require_verifiable_publish_date": True,
        "max_age_days_collector": 5,
        "max_age_days_pipeline": 14,
        # ③ 行业趋势/政策类 + 低可信 → 不进入任何板块（连线索都不是）
        "discard_low_credibility_industry": True,
        # ④ 每日最低可引证动态条数（Part1+Part2+Part3，不含线索区）
        "min_citable_dynamics": 5,
        "min_citable_part2": 5,  # Part2 竞品动态硬性最低（不满则需搜完201个品牌）
        "enforce_on": "daily_job",  # 生效位置
    },

    # ——— 通知投递（V2.3.3） ———
    # 日报生成后通过 cloudflared 隧道暴露公网 URL，发送链接到钉钉群
    "notification": {
        "channel": "dingtalk",
        "format": "markdown_link",
        "webhook_env": "DINGTALK_WEBHOOK",
        "deploy": {
            "provider": "cloudflared",  # 免费 HTTPS 隧道，国内可访问
            "serve_port": 8888,
            "serve_dir": "serve/",
            "timeout_seconds": 30,
        },
        "message_template": {
            "title": "🍜 三品王每日品牌动态日报",
            "sections": ["日期", "统计(事件/可引证/需核实)", "链接"],
        },
    },
}

# 后向兼容别名（processor/ 模块引用，避免全量改写）
SOURCE_DOMAIN_CREDIBILITY = PROCESS_STRATEGY["domain_table"]
RANKING_CREDIBILITY_TIERS = PROCESS_STRATEGY["ranking_tiers"]
COMPOSITE_LEVEL = PROCESS_STRATEGY["composite_level"]
CITABLE_LEVELS = PROCESS_STRATEGY["citation"]["citable_levels"]
LOGIN_GATED_DOMAINS = PROCESS_STRATEGY["login_gated_domains"]

# === 用户声量快报配置 ===
VOICE_SNAPSHOT_SPEC = {
    "platforms": ["微博", "抖音", "百度"],
    "brands_to_track": ["三品王", "柳螺香", "粉之都", "尝不忘", "螺公堂", "王味螺"],
    "note": "基于各平台公开搜索结果的声量快照，反映品牌近期线上热度",
}

# === 排名加权权重（保留供后续接入真实API时使用） ===
RANKING_WEIGHTS = {
    "美团外卖": 0.38,
    "淘宝闪购": 0.27,
    "抖音团购": 0.12,
    "美团团购": 0.10,
    "高德地图": 0.08,
    "京东外卖": 0.05,
}

# === 采集时效参数 ===
CONTENT_FRESHNESS_DAYS = 5  # 只保留近N天发布的内容
BAIDU_TIME_FILTER = "4"     # rtt=4 表示近一周新闻（百度搜索参数）
# 时效性关键词黑名单（标题含这些词的内容标记为低时效）
STALE_KEYWORDS = ["回顾", "盘点", "历年", "历史", "几年前", "回忆", "曾经"]

# === 热点类别映射 ===
HOTSPOT_CATEGORIES = {
    "cat_1": {"name": "新品牌/新产品出现", "icon": "🆕"},
    "cat_2": {"name": "高热度营销活动", "icon": "🔥"},
    "cat_3": {"name": "价格/促销/团购变动", "icon": "💰"},
    "cat_4": {"name": "渠道/扩张动作", "icon": "📢"},
    "cat_5": {"name": "门店运营升级", "icon": "🏪"},
    "cat_6": {"name": "组织/资本/供应链动态", "icon": "🏢"},
    "cat_7": {"name": "数据/业绩披露", "icon": "📊"},
    "cat_8": {"name": "舆情/食安风险", "icon": "⚠️"},
    "cat_9": {"name": "行业趋势/政策", "icon": "📋"},
}


# ============================================================================
# 框架规范：品牌门店图片抓取策略（brand_library.json 的 hero_image 字段）
# ============================================================================
BRAND_IMAGE_SPEC = {
    # 搜索词优先级：门头/招牌 > 店面/门店外观 > 餐厅/门面
    "search_phrases": ["门头 招牌", "店面 外观", "餐厅 门面"],
    # 每个搜索词拉取候选数量
    "candidates_per_phrase": 15,
    # 去重后按评分排序，取最高分
    "scoring": {
        "aspect_ratio": {          # 宽高比评分区间（横向图优先，门头特征不被裁切）
            "best": (1.2, 2.0),    # 1.2~2.0 得满分（最适合卡片头部比例）
            "best_score": 30,
            "near_square": (1.0, 1.2),  # 近正方形
            "near_square_score": 20,
            "slightly_tall": (0.8, 1.0),
            "slightly_tall_score": 10,
        },
        "min_width": 800,          # 宽度≥800px 得满分
        "width_score": 20,
        "pixels_threshold": 500000,  # 总像素>50万 得满分
        "pixels_score": 15,
    },
    # 质量门槛：图片必须突出「品牌自身门头/招牌」，禁止以下情况
    "quality_rules": [
        "禁止广场/商场等第三方招牌盖过品牌标识（如误用'小宝味道'广场招牌代替'尝不忘'门头）",
        "禁止纯玻璃幕墙/无品牌标识的门店外观",
        "优先横向图（宽高比1.2~2.0），竖版图（如0.7）门头特征易被 cover 裁切",
        "人工复核：对核心竞品、自身品牌图片需确认门头主体为品牌自身",
    ],
    "source": "百度图片 JSON API (thumbURL 字段，返回可直接访问的缩略图URL)",
    "note": "搜索词纯品牌名+门头/招牌，不附加品类词，避免召回偏差",
}


# ============================================================================
# 框架规范：日报卡片头部样式（report.html 的 .card-head 系列样式）
# ============================================================================
REPORT_CARD_STYLE_SPEC = {
    # 整体容器：固定移动端宽度，PC/移动端渲染完全一致
    "container_max_width": 430,        # body max-width(px)，PC端居中显示此宽度
    "container_padding": 12,           # body padding(px)
    # 事件卡片
    "card_width": "100%",              # 跟随容器宽度
    # 头部：固定高度确保任意卡片尺寸一致
    "card_head_height": 120,           # 头部固定高度(px)，box-sizing:border-box
    "card_head_padding": "12px",       # 头部内边距
    # 背景图
    "bg_size": "cover",
    "bg_position_brand": "top",        # 品牌图：上对齐，门头标识优先显示
    "bg_position_industry": "center",  # 行业/政策图：居中
    "bg_blur": 1.5,                    # 模糊(px)，柔化背景不影响文字
    "overlay_color": "rgba(0,0,0,0.3)",  # 黑色半透明蒙版，保证白字可读
    # 文字：单行截断，不溢出图片区域
    "brand_name_font_size": 16,
    "brand_name_max_width": "calc(100% - 80px)",
    "title_font_size": 14,
    "text_color": "white",
    "text_shadow": "0 1px 3px rgba(0,0,0,0.5)",
    "ellipsis": "white-space:nowrap; overflow:hidden; text-overflow:ellipsis;",
    "rules": [
        "所有卡片头部高度必须一致（固定120px），禁止因内容多少而撑开/收缩",
        "品牌图 background-position:top，行业/政策图 background-position:center",
        "背景图模糊1.5px + 黑色30%蒙版，文字白色+阴影确保可读",
        "品牌名、标题均单行截断(ellipsis)，禁止换行或溢出图片区域",
        "PC端浏览器与移动端渲染效果完全一致（容器固定430px宽度）",
        "hero_image 在生成日报时由 aggregator 实时从品牌库同步，事件库旧图不会污染",
    ],
}


def load_brand_library():
    """加载品牌库"""
    with open(BRAND_LIBRARY_PATH, "r") as f:
        return json.load(f)


def get_core_competitors():
    """获取核心竞品列表"""
    lib = load_brand_library()
    return [b for b in lib["brands"] if b["circle"] == "核心竞品"]


def get_all_brands_by_circle():
    """按圈层分组获取所有品牌"""
    lib = load_brand_library()
    groups = {"自身品牌": [], "核心竞品": [], "区域竞品": [], "场景竞品": [], "替代竞品": []}
    for b in lib["brands"]:
        groups.setdefault(b["circle"], []).append(b)
    return groups


def ensure_dirs():
    """确保所有数据目录存在"""
    for d in [DATA_DIR, OUTPUT_DIR, ARCHIVE_DIR, TEMPLATE_DIR]:
        d.mkdir(parents=True, exist_ok=True)
