"""
Part 1 采集器: 三品王自身舆情监控
信息源: 社媒搜索、新闻搜索、监管公告、平台排名、投诉平台
"""
import asyncio
import httpx
from datetime import date, timedelta
from typing import Optional
from loguru import logger

from collector.base import BaseCollector, RawItem
from processor.dedup import extract_keywords
import config


class SelfMonitorCollector(BaseCollector):
    """三品王自身品牌监控采集器"""

    source_id = "self_monitor"
    source_name = "自身舆情监控"
    priority = 1
    circle_scope = ["自身品牌"]

    def __init__(self):
        self.brand_name = "三品王"
        self.brand_id = "BR001"
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def collect(self, target_date: str) -> list[RawItem]:
        """
        采集三品王自身舆情
        策略: 使用搜索引擎搜索近期新闻 + 社媒提及
        """
        items = []
        tasks = [
            self._search_news(target_date),
            self._search_social(target_date),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, list):
                items.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"采集异常: {result}")

        logger.info(f"[自身监控] 采集到 {len(items)} 条原始条目")
        # 时效过滤
        items = self.filter_fresh_items(items, target_date)
        logger.info(f"[自身监控] 时效过滤后: {len(items)} 条")
        return items

    async def _search_news(self, target_date: str) -> list[RawItem]:
        """搜索三品王相关新闻，解析百度搜索结果"""
        items = []
        queries = ["三品王 米粉", "三品王 新店", "三品王 新品"]

        for query in queries:
            try:
                url = self.build_search_url(query)
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = await self.client.get(url, headers=headers)
                if resp.status_code == 200:
                    news_items = self._parse_news_results(resp.text, query)
                    for news in news_items:
                        item = RawItem(
                            source="新闻搜索",
                            url=news.get("url", url),
                            title=news.get("title", f"搜索: {query}"),
                            content=news.get("summary", ""),
                            published_at=news.get("date", target_date),
                            brand_name=self.brand_name,
                            brand_id=self.brand_id,
                            circle="自身品牌",
                            category=self._classify_title(news.get("title", "")),
                            keywords=extract_keywords(news.get("title", "")),
                            metadata={"query": query, "source_type": "news_search"},
                        )
                        items.append(item)
            except Exception as e:
                logger.warning(f"新闻搜索失败 [{query}]: {e}")

        return items

    async def _search_social(self, target_date: str) -> list[RawItem]:
        """搜索社媒提及"""
        items = []
        queries = ["三品王", "三品王米粉"]

        for query in queries:
            try:
                # 微博搜索
                url = self.build_search_url(query)
                resp = await self.client.get(url)
                if resp.status_code == 200:
                    keywords = extract_keywords(query)
                    item = RawItem(
                        source="社交媒体",
                        url=url,
                        title=f"社媒提及: {query}",
                        content=f"社交媒体搜索: {query}",
                        published_at=target_date,
                        brand_name=self.brand_name,
                        brand_id=self.brand_id,
                        circle="自身品牌",
                        category="舆情/食安风险",
                        keywords=keywords,
                        metadata={"query": query, "source_type": "social_search"},
                    )
                    items.append(item)
            except Exception as e:
                logger.warning(f"社媒搜索失败 [{query}]: {e}")

        return items

    def _parse_news_results(self, html: str, query: str) -> list[dict]:
        """解析百度新闻搜索结果"""
        from bs4 import BeautifulSoup
        import re
        results = []
        try:
            soup = BeautifulSoup(html, "html.parser")
            for h3 in soup.find_all("h3"):
                link = h3.find("a")
                if link:
                    title = link.get_text(strip=True)
                    url = link.get("href", "")
                    if title and len(title) > 5:
                        results.append({"title": title, "url": url, "summary": "", "date": ""})
        except Exception:
            pass
        return results[:5]

    def _classify_title(self, title: str) -> str:
        """根据标题判断热点类别"""
        if any(w in title for w in ["新品", "上市", "推出"]):
            return "新品牌/新产品出现"
        if any(w in title for w in ["新店", "开业", "扩张", "加盟"]):
            return "渠道/扩张动作"
        if any(w in title for w in ["活动", "促销", "优惠", "团购"]):
            return "价格/促销/团购变动"
        if any(w in title for w in ["投诉", "卫生", "安全", "事故", "曝光"]):
            return "舆情/食安风险"
        return "行业趋势/政策"

    async def close(self):
        await self.client.aclose()
