"""
日报存储 — 日报归档管理
"""
import json
from pathlib import Path
from datetime import date
from loguru import logger
import config


class ReportStore:
    """日报归档管理器"""

    def __init__(self):
        self.archive_dir = config.ARCHIVE_DIR
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def save_report(self, report: dict) -> Path:
        """保存日报数据到归档目录"""
        report_date = report.get("date", date.today().isoformat())
        day_dir = self.archive_dir / report_date
        day_dir.mkdir(parents=True, exist_ok=True)

        # 保存原始数据JSON
        data_path = day_dir / "data.json"
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"日报数据已归档: {data_path}")
        return data_path

    def list_archives(self, limit: int = 30) -> list[str]:
        """列出最近的归档日期"""
        dirs = sorted(
            [d.name for d in self.archive_dir.iterdir() if d.is_dir()],
            reverse=True,
        )
        return dirs[:limit]

    def load_report(self, report_date: str) -> dict:
        """加载指定日期的日报数据"""
        data_path = self.archive_dir / report_date / "data.json"
        if data_path.exists():
            with open(data_path, "r") as f:
                return json.load(f)
        return None

    def get_latest_date(self) -> str:
        """获取最新归档日期"""
        archives = self.list_archives(limit=1)
        return archives[0] if archives else None
