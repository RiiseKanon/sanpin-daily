"""
采集器抽象基类 — 所有信息源采集器的父类

每个采集器实现:
- source_id: 唯一标识
- priority: 优先级（1最高，数字越大越低）
- circle_scope: 采集的品牌圈层范围
- collect(): 异步采集方法，返回 RawItem 列表
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional
import re
from loguru import logger
import config


@dataclass
class RawItem:
    """采集到的原始条目"""
    source: str              # 信息源标识
    url: str = ""            # 原始URL
    title: str = ""          # 标题
    content: str = ""        # 正文摘要
    published_at: str = ""   # 发布时间 (YYYY-MM-DD)
    brand_name: str = ""     # 关联品牌名
    brand_id: str = ""       # 品牌库ID
    circle: str = ""         # 品牌圈层
    category: str = ""       # 热点类别
    keywords: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    # 时效标记
    is_stale: bool = False   # 是否为过期/低时效内容
    freshness_score: float = 1.0  # 时效评分 0-1


class BaseCollector(ABC):
    """采集器基类"""

    source_id: str = "base"
    source_name: str = "未命名采集器"
    priority: int = 3
    circle_scope: list[str] = []  # 空列表 = 不限圈层

    # 时效过滤
    freshness_days: int = config.CONTENT_FRESHNESS_DAYS

    @abstractmethod
    async def collect(self, target_date: str) -> list[RawItem]:
        ...

    def should_collect_brand(self, brand: dict) -> bool:
        """根据分层策略判断是否采集该品牌"""
        if not self.circle_scope:
            return True
        return brand.get("circle", "") in self.circle_scope

    def build_search_url(self, query: str, time_filter: bool = True) -> str:
        """构建百度新闻搜索URL，自动添加时效参数"""
        url = f"https://www.baidu.com/s?tn=news&word={query}"
        if time_filter:
            url += f"&rtt={config.BAIDU_TIME_FILTER}"
        return url

    def check_freshness(self, item: RawItem, target_date: str) -> RawItem:
        """
        三重时效校验：
        1. 解析发布时间，与target_date比较
        2. 检查标题是否含过期关键词
        3. 计算时效评分
        """
        published = item.published_at
        if not published:
            # 无日期信息，保守标记
            item.freshness_score = 0.5
            item.metadata["freshness_note"] = "无发布日期"
            return item

        try:
            pub_date = date.fromisoformat(published)
            target = date.fromisoformat(target_date)
            days_diff = (target - pub_date).days
        except (ValueError, TypeError):
            item.freshness_score = 0.5
            item.metadata["freshness_note"] = "日期解析失败"
            return item

        # 过滤1: 超过 freshness_days 天的内容
        if days_diff > self.freshness_days:
            item.is_stale = True
            item.freshness_score = 0.0
            item.metadata["freshness_note"] = f"过期({days_diff}天前)"
            return item

        # 过滤2: 标题含过期关键词
        for kw in config.STALE_KEYWORDS:
            if kw in item.title:
                item.is_stale = True
                item.freshness_score = 0.1
                item.metadata["freshness_note"] = f"标题含过期词'{kw}'"
                return item

        # 过滤3: 计算时效评分
        if days_diff <= 1:
            item.freshness_score = 1.0
        elif days_diff <= 2:
            item.freshness_score = 0.9
        elif days_diff <= 3:
            item.freshness_score = 0.7
        else:
            item.freshness_score = 0.5

        item.metadata["freshness_note"] = f"近{days_diff}天"
        return item

    def filter_fresh_items(self, items: list[RawItem], target_date: str) -> list[RawItem]:
        """过滤并标记，保留非过期条目"""
        fresh = []
        stale_count = 0
        for item in items:
            item = self.check_freshness(item, target_date)
            if item.is_stale:
                stale_count += 1
            else:
                fresh.append(item)
        if stale_count > 0:
            logger.info(f"[{self.source_name}] 时效过滤: 丢弃 {stale_count} 条过期内容，保留 {len(fresh)} 条")
        return fresh

    def __repr__(self):
        return f"<{self.source_name}({self.source_id}) priority={self.priority}>"
