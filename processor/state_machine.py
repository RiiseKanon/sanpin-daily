"""
事件状态机 — 管理事件的完整生命周期

状态流转规则：
    NEW ──(再次命中)──→ ACTIVE
    NEW ──(超过1天未出现)──→ CLOSED
    ACTIVE ──(3天无命中)──→ COOLING
    COOLING ──(7天无命中)──→ CLOSED
    COOLING ──(重新出现)──→ ACTIVE
    任意 ──(评分变动≥±2)──→ ESCALATED
    ESCALATED ──(连续2天未出现)──→ CLOSED
"""
from datetime import date, timedelta
from typing import Optional
from loguru import logger

# 状态枚举
STATUS_NEW = "NEW"
STATUS_ACTIVE = "ACTIVE"
STATUS_COOLING = "COOLING"
STATUS_CLOSED = "CLOSED"
STATUS_ESCALATED = "ESCALATED"

# 默认阈值
ACTIVE_AFTER_DAYS = 1      # 再次命中 → ACTIVE
COOLING_AFTER_DAYS = 3     # N天无命中 → COOLING
CLOSE_AFTER_DAYS = 7       # COOLING后N天 → CLOSED
ESCALATE_SCORE_DELTA = 2   # 评分变动 ≥ ±2 → ESCALATED
ESCALATED_CLOSE_DAYS = 2   # ESCALATED后N天无命中 → CLOSED


class EventStateMachine:
    """事件状态机"""

    def __init__(
        self,
        active_after_days: int = ACTIVE_AFTER_DAYS,
        cooling_after_days: int = COOLING_AFTER_DAYS,
        close_after_days: int = CLOSE_AFTER_DAYS,
        escalate_score_delta: int = ESCALATE_SCORE_DELTA,
        escalated_close_days: int = ESCALATED_CLOSE_DAYS,
    ):
        self.active_after_days = active_after_days
        self.cooling_after_days = cooling_after_days
        self.close_after_days = close_after_days
        self.escalate_score_delta = escalate_score_delta
        self.escalated_close_days = escalated_close_days

    def on_new_hit(self, event: dict, today: str) -> str:
        """
        当事件被再次命中时，判断状态转换
        返回新状态
        """
        current = event.get("status", STATUS_NEW)

        if current == STATUS_CLOSED:
            # 已关闭的事件重新出现 → 新建（外部处理）
            return STATUS_NEW

        if current == STATUS_NEW:
            # 再次命中 → ACTIVE
            return STATUS_ACTIVE

        if current == STATUS_COOLING:
            # 冷却中的事件重新出现 → ACTIVE
            return STATUS_ACTIVE

        # ACTIVE / ESCALATED 保持
        return current

    def on_score_change(self, event: dict, old_score: int, new_score: int) -> Optional[str]:
        """
        评分发生显著变化时判断是否升级
        返回新状态，或None表示不变
        """
        delta = abs(new_score - old_score)
        if delta >= self.escalate_score_delta:
            return STATUS_ESCALATED
        return None

    def check_cooling(self, event: dict, today: str) -> Optional[str]:
        """
        检查事件是否应该冷却或关闭
        基于 last_seen 与 today 的天数差
        """
        current = event.get("status", STATUS_NEW)
        last_seen = event.get("last_seen", today)

        try:
            last_date = date.fromisoformat(last_seen)
            today_date = date.fromisoformat(today)
            days_since = (today_date - last_date).days
        except (ValueError, TypeError):
            return None

        if current == STATUS_NEW and days_since > self.active_after_days:
            return STATUS_CLOSED

        if current == STATUS_ACTIVE and days_since >= self.cooling_after_days:
            return STATUS_COOLING

        if current == STATUS_COOLING and days_since >= self.close_after_days:
            return STATUS_CLOSED

        if current == STATUS_ESCALATED and days_since >= self.escalated_close_days:
            return STATUS_CLOSED

        return None

    def advance(self, event: dict, today: str, new_score: Optional[int] = None) -> tuple[str, bool]:
        """
        推进事件状态，返回 (新状态, 是否发生了变化)

        每天对所有活跃事件调用此方法：
        1. 检查是否需要冷却/关闭
        2. 如果传入新评分，检查是否需要升级
        """
        old_status = event.get("status", STATUS_NEW)
        new_status = old_status
        changed = False

        # 先检查冷却
        cooling_result = self.check_cooling(event, today)
        if cooling_result and cooling_result != old_status:
            new_status = cooling_result
            changed = True

        # 再检查评分变化
        if new_score is not None:
            old_score = event.get("score", 0)
            escalate_result = self.on_score_change(event, old_score, new_score)
            if escalate_result and escalate_result != new_status:
                new_status = escalate_result
                changed = True

        if changed:
            logger.info(f"状态转换: {event.get('event_id')} [{old_status}] → [{new_status}]")

        return new_status, changed

    def daily_maintenance(self, events: list[dict], today: str) -> list[dict]:
        """
        每日维护：遍历所有事件，推进状态
        返回需要更新的事件列表 [(event, new_status), ...]
        """
        updates = []
        for event in events:
            current = event.get("status", STATUS_NEW)
            # 跳过已关闭的
            if current == STATUS_CLOSED:
                continue

            new_status, changed = self.advance(event, today)
            if changed:
                updates.append((event, new_status))

        return updates

    @staticmethod
    def get_status_label(status: str) -> str:
        """获取状态的中文标签"""
        labels = {
            STATUS_NEW: "🆕 新发现",
            STATUS_ACTIVE: "🔄 追踪中",
            STATUS_COOLING: "🌡️ 热度消退",
            STATUS_CLOSED: "✅ 已完结",
            STATUS_ESCALATED: "🔥 重大升级",
        }
        return labels.get(status, status)

    @staticmethod
    def is_visible_in_daily(status: str) -> bool:
        """判断事件在日报中是否可见"""
        return status in (STATUS_NEW, STATUS_ACTIVE, STATUS_ESCALATED)
