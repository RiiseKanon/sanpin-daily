"""
Part 2 采集器: 竞品动态新闻搜索

采集策略:
  1. 逐层扩展: 核心竞品43→区域竞品69→场景竞品8→替代竞品80，直至Part 2 ≥5条动态
  2. 纯品牌名搜索: 搜索时只使用品牌名，不附加品类限定词（如"米粉""螺蛳粉"等）
     目的: 避免品类词过滤掉品牌在跨界合作、资本运作、人事变动等非品类维度的动态
  3. 每个品牌至少搜索1次，搜索结果需人工/AI验证时效性和内容相关性
  4. 时效窗口: 5天（超过5天的内容自动丢弃）
  5. 硬性规则: 不满5条则必须搜完全部201个品牌，不允许提前终止

搜索任务清单格式:
  {"brand_name": "尝不忘", "brand_id": "BR002", "circle": "核心竞品", "query": "尝不忘"}
  query = 纯品牌名，不加任何限定词

实现方式: 生成搜索任务清单，由AI辅助执行 WebSearch，解析结果后入库
"""
import asyncio
import httpx
from datetime import date, timedelta
from loguru import logger

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

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    def build_search_tasks(self, target_date: str) -> list[dict]:
        """
        生成搜索任务清单
        每个品牌一个任务，query = 纯品牌名，不附加品类词
        """
        all_brands = config.get_all_brands_by_circle()
        tasks = []

        layer_order = ["核心竞品", "区域竞品", "场景竞品", "替代竞品"]

        for circle in layer_order:
            brands = all_brands.get(circle, [])
            for brand in brands:
                tasks.append({
                    "brand_name": brand["name"],
                    "brand_id": brand["id"],
                    "circle": circle,
                    "query": brand["name"],  # 纯品牌名，不加品类限定词
                    "searched": False,
                })

        logger.info(f"[竞品动态] 生成 {len(tasks)} 个搜索任务（纯品牌名模式）")
        return tasks

    async def collect(self, target_date: str) -> list[RawItem]:
        """生成搜索任务清单，实际采集由AI辅助完成"""
        tasks = self.build_search_tasks(target_date)
        logger.info(f"[竞品动态] 搜索任务清单已生成（{len(tasks)}个品牌，纯品牌名搜索）")
        return []

    def _classify_category(self, query: str) -> str:
        """根据搜索词判断热点类别"""
        if any(w in query for w in ["新品", "新产品", "上市"]):
            return "新品牌/新产品出现"
        if any(w in query for w in ["活动", "促销", "优惠"]):
            return "高热度营销活动"
        if any(w in query for w in ["价格", "团购", "降价"]):
            return "价格/促销/团购变动"
        if any(w in query for w in ["新店", "加盟", "扩张", "开店"]):
            return "渠道/扩张动作"
        if any(w in query for w in ["融资", "上市", "财报"]):
            return "数据/业绩披露"
        if any(w in query for w in ["投诉", "卫生", "安全", "事故"]):
            return "舆情/食安风险"
        return "行业趋势/政策"

    async def close(self):
        await self.client.aclose()
