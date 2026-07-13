"""
HTML渲染器 — 使用Jinja2将日报数据渲染为单文件HTML
"""
import os
from pathlib import Path
from datetime import datetime
from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger
import config


class ReportRenderer:
    """日报HTML渲染器"""

    def __init__(self):
        self.env = Environment(
            loader=FileSystemLoader(str(config.TEMPLATE_DIR)),
            autoescape=select_autoescape(['html', 'xml']),
        )
        self.template = self.env.get_template("daily_report.html")

    def render(self, report: dict) -> str:
        """
        将日报数据渲染为HTML字符串
        """
        # 为模板添加辅助函数
        def render_event_card(event):
            """内联事件卡片渲染（由Jinja2宏处理，此处为Python端预留）"""
            return ""

        html = self.template.render(
            report=report,
            render_event_card=render_event_card,
            style=config.REPORT_CARD_STYLE_SPEC,
            image_spec=config.BRAND_IMAGE_SPEC,
        )
        logger.info(f"HTML渲染完成: {len(html)} 字符")
        return html

    def render_to_file(self, report: dict, output_path: Path = None) -> Path:
        """
        渲染日报并写入文件
        """
        if output_path is None:
            output_path = config.LATEST_HTML_PATH

        output_path.parent.mkdir(parents=True, exist_ok=True)

        html = self.render(report)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"日报已输出: {output_path}")
        return output_path

    def archive(self, report: dict) -> Path:
        """
        归档日报到 archive/YYYY-MM-DD/
        """
        date_str = report.get("date", datetime.now().strftime("%Y-%m-%d"))
        archive_dir = config.ARCHIVE_DIR / date_str
        archive_dir.mkdir(parents=True, exist_ok=True)

        html_path = archive_dir / "report.html"
        html = self.render(report)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"日报已归档: {html_path}")
        return html_path
