"""
事件库 CRUD — events.json 持久化存储
支持事件的增删改查、按状态筛选、按日期范围查询
"""
import json
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from loguru import logger


class EventStore:
    """事件库管理器"""

    def __init__(self, filepath: Path):
        self.filepath = Path(filepath)
        self._data = {"events": [], "_meta": {"total": 0, "last_updated": ""}}
        self._load()

    def _load(self):
        """从磁盘加载事件库"""
        if self.filepath.exists():
            try:
                with open(self.filepath, "r") as f:
                    self._data = json.load(f)
                logger.info(f"事件库已加载: {len(self._data['events'])} 条事件")
            except (json.JSONDecodeError, KeyError):
                logger.warning("事件库文件损坏，使用空库")
                self._data = {"events": [], "_meta": {"total": 0, "last_updated": ""}}
        else:
            self._save()

    def _save(self):
        """持久化到磁盘"""
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        if "_meta" not in self._data:
            self._data["_meta"] = {}
        self._data["_meta"]["total"] = len(self._data["events"])
        self._data["_meta"]["last_updated"] = datetime.now().isoformat()
        with open(self.filepath, "w") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def all(self) -> list[dict]:
        """获取所有事件"""
        return self._data["events"]

    def persist(self) -> None:
        """将内存中（可能被外部原地修改的）事件写回磁盘

        例如 aggregator._refresh_hero_images 会原地刷新 hero_image 与
        source_credibility，但不经过 add/update，因此需要显式落盘以避免
        磁盘数据滞后于内存。
        """
        self._save()

    def find_by_id(self, event_id: str) -> Optional[dict]:
        """按事件ID查找"""
        for e in self._data["events"]:
            if e["event_id"] == event_id:
                return e
        return None

    def find_by_fingerprint(self, fingerprint: str) -> Optional[dict]:
        """按事件指纹查找"""
        for e in self._data["events"]:
            if e["fingerprint"] == fingerprint:
                return e
        return None

    def find_active(self) -> list[dict]:
        """获取所有活跃事件（NEW / ACTIVE / ESCALATED）"""
        return [e for e in self._data["events"]
                if e["status"] in ("NEW", "ACTIVE", "ESCALATED")]

    def find_by_brand(self, brand_id: str) -> list[dict]:
        """按品牌ID查找"""
        return [e for e in self._data["events"] if e["brand_id"] == brand_id]

    def find_by_date_range(self, start_date: str, end_date: str) -> list[dict]:
        """按日期范围查找（基于 last_seen）"""
        return [e for e in self._data["events"]
                if start_date <= e["last_seen"] <= end_date]

    def find_recent(self, days: int = 5) -> list[dict]:
        """查找最近N天内活跃的事件"""
        from datetime import timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        return [e for e in self._data["events"] if e["last_seen"] >= cutoff]

    def add(self, event: dict) -> str:
        """新增事件，返回 event_id"""
        existing = self.find_by_fingerprint(event.get("fingerprint", ""))
        if existing:
            logger.debug(f"事件已存在（指纹匹配）: {event.get('title')}")
            return existing["event_id"]

        self._data["events"].append(event)
        self._save()
        logger.info(f"新增事件: {event['event_id']} - {event.get('title', '')}")
        return event["event_id"]

    def update(self, event_id: str, updates: dict) -> bool:
        """更新事件字段"""
        event = self.find_by_id(event_id)
        if not event:
            return False
        event.update(updates)
        event["updated_at"] = datetime.now().isoformat()
        self._save()
        return True

    def add_snapshot(self, event_id: str, snapshot: dict) -> bool:
        """给事件追加一个快照"""
        event = self.find_by_id(event_id)
        if not event:
            return False
        if "snapshots" not in event:
            event["snapshots"] = []
        event["snapshots"].append(snapshot)
        event["last_seen"] = snapshot.get("date", datetime.now().isoformat()[:10])
        event["updated_at"] = datetime.now().isoformat()
        self._save()
        return True

    def delete(self, event_id: str) -> bool:
        """删除事件"""
        original_len = len(self._data["events"])
        self._data["events"] = [e for e in self._data["events"] if e["event_id"] != event_id]
        if len(self._data["events"]) < original_len:
            self._save()
            return True
        return False

    def generate_id(self, date_str: str = None) -> str:
        """生成唯一事件ID: EVT-YYYYMMDD-NNN"""
        if date_str is None:
            date_str = date.today().strftime("%Y%m%d")
        existing = [e for e in self._data["events"] if e["event_id"].startswith(f"EVT-{date_str}")]
        seq = len(existing) + 1
        return f"EVT-{date_str}-{seq:03d}"

    def count_by_status(self) -> dict:
        """按状态统计"""
        counts = {}
        for e in self._data["events"]:
            s = e.get("status", "UNKNOWN")
            counts[s] = counts.get(s, 0) + 1
        return counts

    def total(self) -> int:
        return len(self._data["events"])
