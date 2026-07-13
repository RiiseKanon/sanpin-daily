"""
数据聚合器 — 将处理后的事件按日报四部分聚合
"""
from datetime import date, timedelta
from typing import Optional
from loguru import logger
import config
from processor.source_checker import (
    check_source_credibility, assess_verifiability, is_login_gated,
)


def _get_meta(item) -> dict:
    """安全获取 RawItem 或 dict 的 metadata"""
    if isinstance(item, dict):
        return item.get("metadata", {})
    return getattr(item, "metadata", {})


class ReportAggregator:
    """日报数据聚合器"""

    def _refresh_hero_images(self, events: list[dict]) -> list[dict]:
        """从品牌库实时刷新事件的 hero_image，并确保来源可信度字段存在"""
        try:
            lib = config.load_brand_library()
            brand_map = {b["id"]: b.get("hero_image", "") for b in lib.get("brands", [])}
        except Exception:
            brand_map = {}

        for e in events:
            bid = e.get("brand_id")
            if bid and bid in brand_map and brand_map[bid]:
                e["hero_image"] = brand_map[bid]
            # V2：每次聚合都基于正文摘要重算来源综合可信指数，
            # 让旧事件的 content 分也能吃到正文信号微调（而非停留在 v1 域名基准）
            article_text = e.get("content") or e.get("summary", "") or ""
            e["source_credibility"] = check_source_credibility(
                e.get("url", ""), e.get("source", ""), article_text=article_text)
            # V2.2 引证分级：评估可核实链接 + 结合可信等级判定可引证/需核实
            vf = assess_verifiability(e.get("url", ""), e.get("title", ""))
            cred_level = e["source_credibility"].get("level", "低")
            citable = vf["verifiable"] and cred_level in config.PROCESS_STRATEGY["citation"]["citable_levels"]
            e["verifiable"] = vf["verifiable"]
            e["citation_url"] = vf["citation_url"]
            e["citation_tier"] = "citable" if citable else "lead"
            e["link_gated"] = is_login_gated(e.get("url", ""))
            e["gate_note"] = vf.get("gate_note", "")
        return events

    def aggregate(self, events: list[dict], rankings: list[dict], report_date: str) -> dict:
        """
        将事件和排名数据聚合成日报数据结构
        """
        # 实时刷新品牌图片
        events = self._refresh_hero_images(events)

        # 按部分分组（引证分级 V2.2：品牌板块只放「可引证动态」，
        # 所有「需核实线索」统一汇入 part_leads，避免两块重叠）
        is_lead = lambda e: e.get("citation_tier") == "lead"
        part1_events = [e for e in events if e.get("report_part") == "part_1" and not is_lead(e)]
        part2_events = [e for e in events if e.get("report_part") == "part_2" and not is_lead(e)]
        part3_events = [e for e in events if e.get("report_part") == "part_3" and not is_lead(e)]

        # Part 2 按圈层分组
        by_circle = {"核心竞品": [], "区域竞品": [], "场景竞品": [], "替代竞品": []}
        for e in part2_events:
            circle = e.get("circle", "替代竞品")
            by_circle.setdefault(circle, []).append(e)

        # 引证分级（V2.2）：所有「需核实线索」汇入独立板块
        # V2.3.1 过滤：行业趋势/政策 + 低可信 → 不进入任何板块
        #   （无确凿来源的行业趋势类内容属于噪音，连线索都不算）
        leads = []
        for e in events:
            if e.get("citation_tier") != "lead":
                continue
            cat = e.get("category", "")
            if cat == "行业趋势/政策":
                cred = e.get("source_credibility", {})
                if cred.get("level") == "低":
                    logger.debug(f"行业趋势/政策低可信丢弃: {e.get('title','')[:40]}")
                    continue
            leads.append(e)

        # 统计
        stats = {
            "total_events": len(events),
            "new_events": len([e for e in events if e.get("status") == "NEW"]),
            "active_events": len([e for e in events if e.get("status") == "ACTIVE"]),
            "escalated_events": len([e for e in events if e.get("status") == "ESCALATED"]),
            "total_rankings": len(rankings),
            "ranking_success": len([r for r in rankings if _get_meta(r).get("status") != "failed"]),
            "ranking_failed": len([r for r in rankings if _get_meta(r).get("status") == "failed"]),
        }

        report = {
            "date": report_date,
            "generated_at": date.today().isoformat(),
            "part_1": {
                "title": "自身舆情监控",
                "rating_name": "正向评级",
                "rating_icon": "🌟",
                "events": part1_events,
                "empty": len(part1_events) == 0,
            },
            "part_2": {
                "title": "竞品动态",
                "rating_name": "威胁评级",
                "rating_icon": "⚔️",
                "by_circle": by_circle,
                "events": part2_events,
                "empty": len(part2_events) == 0,
            },
            "part_3": {
                "title": "行业趋势/政策",
                "rating_name": "利好评级",
                "rating_icon": "📈",
                "events": part3_events,
                "empty": len(part3_events) == 0,
            },
            "part_leads": {
                "title": "需核实线索",
                "rating_icon": "🔍",
                "events": leads,
                "empty": len(leads) == 0,
                "note": "以下动态来源不可核实或可信度低，仅作跟进线索，请勿直接用于决策引证。",
            },
            "part_4": {
                "title": "今日焦点品牌",
                "rating_icon": "📖",
                "focus_brand": None,
                "empty": True,
            },
            "part_5": {
                "title": "品牌库变动简报",
                "rating_icon": "📋",
                "empty": True,
            },
            "stats": stats,
        }

        logger.info(
            f"日报聚合: P1={len(part1_events)} P2={len(part2_events)} "
            f"P3={len(part3_events)} P4={len(rankings)}"
        )
        return report

    def get_empty_behavior(self, part_num: int) -> str:
        """获取空内容时的标注文案"""
        behaviors = {
            1: "今日无自身舆情动态",
            2: "今日无竞品重大动态",
            3: "今日无行业政策动态",
            4: "今日排名数据源异常，已记录待重试",
        }
        return behaviors.get(part_num, "")
