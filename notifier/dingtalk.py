"""
钉钉机器人通知模块

通过钉钉自定义机器人 Webhook 发送日报链接（Markdown 格式）。
文档: https://open.dingtalk.com/document/orgapp/custom-robot-access
"""
import json
import os
import requests
from datetime import date
from loguru import logger


# Webhook URL（优先环境变量，回退硬编码）
DINGTALK_WEBHOOK = os.getenv(
    "DINGTALK_WEBHOOK",
    "https://oapi.dingtalk.com/robot/send?access_token=227c90706de626835f2ee13c091a191518f3c2ecf2797957f31383f1ccc1bf9d"
)


def send_report_link(report_url: str, github_url: str = None, report_date: str = None, stats: dict = None) -> bool:
    """
    向钉钉群发送日报链接。

    参数:
        report_url:  日报实时链接（cloudflared 隧道，沙箱存活期间有效）
        github_url:  GitHub Pages 永久存档链接（国内可能需翻墙）
        report_date: 日报日期 (YYYY-MM-DD)，默认今天
        stats:       日报统计信息 {"total_events": N, "citable": N, "leads": N}
    """
    if report_date is None:
        report_date = date.today().isoformat()

    stats = stats or {}
    total = stats.get("total_events", "?")
    citable = stats.get("citable", "?")
    leads = stats.get("leads", "?")

    github_line = f"\n\n📦 [GitHub 永久存档]({github_url})" if github_url else ""
    markdown_text = (
        f"## 🍜 三品王每日品牌动态日报\n\n"
        f"**日期**: {report_date}\n\n"
        f"**统计**: 事件 {total} 条 | 可引证 {citable} 条 | 需核实 {leads} 条\n\n"
        f"---\n\n"
        f"📎 [点击查看完整日报]({report_url})"
        f"{github_line}\n\n"
        f"> 支持可信指数、引证分级、焦点品牌 AI 分析等全部功能。"
    )

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"三品王日报 {report_date}",
            "text": markdown_text,
        },
    }

    try:
        resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=15)
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info(f"钉钉通知发送成功: {report_date}")
            return True
        else:
            logger.error(f"钉钉通知发送失败: {result}")
            return False
    except Exception as e:
        logger.error(f"钉钉通知异常: {e}")
        return False


def send_text_message(text: str) -> bool:
    """发送纯文本消息到钉钉（用于测试或告警）"""
    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=10)
        return resp.json().get("errcode") == 0
    except Exception:
        return False
