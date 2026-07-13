"""
评级打分器 — ±5分制，三个维度独立评分

评级维度:
- 正向评级 (Part 1): 自身舆情，越正面分值越高
- 威胁评级 (Part 2): 竞品动态，威胁越大分值越高
- 利好评级 (Part 3): 行业趋势，越利好我方分值越高

评分公式:
    base_score ∈ [-5, 5]
    final = clamp(round(base × 圈层权重 × 时效权重 × 可信度权重), -5, 5)
"""
from datetime import date, timedelta
from typing import Optional
from loguru import logger
import config


class EventRater:
    """事件评级打分器"""

    def rate_self_sentiment(self, event: dict) -> tuple[int, str]:
        """
        Part 1: 正向评级
        越正面→分值越高
        """
        return self._rate(event, "正向评级")

    def rate_competitor_threat(self, event: dict) -> tuple[int, str]:
        """
        Part 2: 威胁评级
        威胁越大→分值越高
        """
        return self._rate(event, "威胁评级")

    def rate_industry_favorable(self, event: dict) -> tuple[int, str]:
        """
        Part 3: 利好评级
        越利好→分值越高
        """
        return self._rate(event, "利好评级")

    def _rate(self, event: dict, rating_type: str) -> tuple[int, str]:
        """
        统一评分逻辑
        返回: (final_score, reason)

        公式: final = clamp(round(base × 圈层权重 × 时效权重), -5, 5)
        注意: 可信度是独立指标（见 source_credibility），不参与威胁/利好评分加权
        """
        base = event.get("base_score", 0)
        if base == 0:
            return 0, "无显著影响，中性"

        # 圈层权重
        circle = event.get("circle", "替代竞品")
        circle_weight = config.CIRCLE_WEIGHTS.get(circle, 0.8)

        # 时效性权重（按事件类别区分：长期事件14天内恒为1，短期事件衰减）
        category = event.get("category", "")
        published = event.get("published_at", "")
        days_ago = self._days_ago(published)
        freshness_weight = config.get_freshness_weight(days_ago, category)

        # 计算最终分数
        raw = base * circle_weight * freshness_weight
        final = max(-5, min(5, round(raw)))

        reason = (
            f"基础分={base:+d} × "
            f"圈层({circle}={circle_weight}) × "
            f"时效({days_ago}天前/{category or '未分类'}={freshness_weight})"
        )

        if raw != final:
            reason += f" → 钳位至 {final:+d}"

        return final, reason

    def _days_ago(self, date_str: str) -> int:
        """计算距离今天的天数"""
        if not date_str:
            return 0
        try:
            d = date.fromisoformat(date_str)
            today = date.today()
            return (today - d).days
        except (ValueError, TypeError):
            return 0

    def classify_event_for_part(self, event: dict) -> str:
        """
        判断事件属于日报哪个部分
        """
        circle = event.get("circle", "")
        brand_id = event.get("brand_id", "")

        # Part 1: 自身品牌
        if circle == "自身品牌" or brand_id == "BR001":
            return "part_1"

        # Part 3: 行业趋势（无具体品牌的事件）
        if not brand_id and not circle:
            return "part_3"

        # Part 2: 竞品动态
        return "part_2"

    def score_event(self, event: dict) -> dict:
        """
        对事件进行完整评分，返回更新后的事件字典
        """
        # 若无 base_score，根据 category 回退推断（兼容手动注入事件）
        if "base_score" not in event or event.get("base_score") is None:
            event["base_score"] = self._infer_base_score(event)

        part = self.classify_event_for_part(event)

        if part == "part_1":
            score, reason = self.rate_self_sentiment(event)
            rating_type = "正向评级"
        elif part == "part_2":
            score, reason = self.rate_competitor_threat(event)
            rating_type = "威胁评级"
        else:
            score, reason = self.rate_industry_favorable(event)
            rating_type = "利好评级"

        event["score"] = score
        event["score_reason"] = reason
        event["rating_type"] = rating_type
        event["report_part"] = part

        logger.debug(f"评分: [{rating_type}] {event.get('title', '')[:30]}... = {score:+d}")
        return event

    def _infer_base_score(self, event: dict) -> int:
        """从 category 推断基础分（兼容未经过 _raw_item_to_event 的事件）"""
        category = event.get("category", "")
        circle = event.get("circle", "")

        if category in ("舆情/食安风险",):
            return -3 if circle == "自身品牌" else 3
        if category in ("渠道/扩张动作", "新品牌/新产品出现"):
            return 3 if circle and circle != "自身品牌" else 2
        if category in ("高热度营销活动", "价格/促销/团购变动"):
            return 2
        if category in ("组织/资本/供应链动态", "数据/业绩披露"):
            return 1
        if category == "行业趋势/政策":
            return 1
        return 0

    def get_score_label(self, score: int) -> str:
        """获取分数的语义标签"""
        if score >= 5:
            return "极强"
        elif score >= 3:
            return "明显"
        elif score >= 1:
            return "轻微"
        elif score >= 0:
            return "中性"
        elif score >= -1:
            return "轻微"
        elif score >= -3:
            return "明显"
        else:
            return "重大"

    def get_score_color(self, score: int, rating_type: str) -> str:
        """获取分数的显示颜色"""
        if score >= 3:
            return "#e53e3e" if rating_type == "威胁评级" else "#38a169"
        elif score >= 1:
            return "#ed8936" if rating_type == "威胁评级" else "#68d391"
        elif score <= -3:
            return "#38a169" if rating_type == "威胁评级" else "#e53e3e"
        elif score <= -1:
            return "#68d391" if rating_type == "威胁评级" else "#ed8936"
        else:
            return "#a0aec0"
