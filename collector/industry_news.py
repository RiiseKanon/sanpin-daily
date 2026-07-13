"""
Part 3 采集器: 行业趋势/政策
信息源: 行业媒体（红餐网/餐饮老板内参/Foodaily/赢商网）+ 本地媒体
"""
import asyncio
import httpx
from datetime import date, timedelta
from loguru import logger

from collector.base import BaseCollector, RawItem
from processor.dedup import extract_keywords
import config


class IndustryNewsCollector(BaseCollector):
    """行业趋势/政策采集器"""

    source_id = "industry_news"
    source_name = "行业趋势/政策"
    priority = 2
    circle_scope = []  # 行业级别，不限品牌

    # 行业媒体源
    INDUSTRY_SOURCES = [
        {"name": "红餐网", "url": "https://www.canyin88.com/"},
        {"name": "餐饮老板内参", "url": "https://www.cylbnc.com/"},
        {"name": "Foodaily", "url": "https://www.foodaily.com/"},
        {"name": "赢商网", "url": "https://www.winshang.com/"},
    ]

    # 本地媒体源
    LOCAL_SOURCES = [
        {"name": "南国早报", "url": "https://www.ngzb.com.cn/"},
        {"name": "广西新闻网", "url": "https://www.gxnews.com.cn/"},
    ]

    # 行业关键词
    INDUSTRY_KEYWORDS = [
        "米粉行业", "餐饮趋势", "粉面市场", "快餐连锁",
        "食品监管", "餐饮政策", "广西餐饮", "米粉品牌",
        "牛肉粉", "螺蛳粉", "桂林米粉", "米粉加盟",
    ]

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def collect(self, target_date: str) -> list[RawItem]:
        """采集行业趋势和政策动态"""
        items = []
        tasks = [
            self._search_industry_news(target_date),
            self._search_local_news(target_date),
            self._search_policy(target_date),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, list):
                items.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"行业采集异常: {result}")

        logger.info(f"[行业趋势] 采集到 {len(items)} 条原始条目")
        # 时效过滤
        items = self.filter_fresh_items(items, target_date)
        logger.info(f"[行业趋势] 时效过滤后: {len(items)} 条")
        return items

    async def _search_industry_news(self, target_date: str) -> list[RawItem]:
        """搜索行业媒体新闻，解析百度新闻搜索结果"""
        items = []

        for kw in self.INDUSTRY_KEYWORDS[:8]:
            try:
                url = self.build_search_url(kw)
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = await self.client.get(url, headers=headers)

                if resp.status_code == 200:
                    # 解析搜索结果
                    news_items = self._parse_news_results(resp.text, kw)
                    for news in news_items:
                        item = RawItem(
                            source="行业媒体",
                            url=news.get("url", url),
                            title=news.get("title", f"行业动态: {kw}"),
                            content=news.get("summary", ""),
                            published_at=news.get("date", target_date),
                            category="行业趋势/政策",
                            keywords=extract_keywords(news.get("title", "")),
                            metadata={"query": kw, "source_type": "industry_search"},
                        )
                        items.append(item)
            except Exception as e:
                logger.debug(f"行业搜索失败 [{kw}]: {e}")

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
                        # 提取日期
                        parent = h3.parent
                        date_str = ""
                        if parent:
                            spans = parent.find_all("span")
                            for s in spans:
                                text = s.get_text()
                                if re.search(r'\d+', text) and len(text) < 20:
                                    date_str = text
                                    break
                        results.append({
                            "title": title,
                            "url": url,
                            "summary": "",
                            "date": date_str,
                        })
        except Exception:
            pass
        return results[:5]

    async def _search_local_news(self, target_date: str) -> list[RawItem]:
        """搜索本地媒体新闻，解析结果"""
        items = []
        local_queries = ["广西餐饮", "南宁美食", "南宁米粉"]

        for query in local_queries:
            try:
                url = self.build_search_url(query)
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = await self.client.get(url, headers=headers)

                if resp.status_code == 200:
                    news_items = self._parse_news_results(resp.text, query)
                    for news in news_items:
                        item = RawItem(
                            source="本地媒体",
                            url=news.get("url", url),
                            title=news.get("title", f"本地动态: {query}"),
                            content=news.get("summary", ""),
                            published_at=news.get("date", target_date),
                            category="行业趋势/政策",
                            keywords=extract_keywords(news.get("title", "")),
                            metadata={"query": query, "source_type": "local_search"},
                        )
                        items.append(item)
            except Exception as e:
                logger.debug(f"本地搜索失败 [{query}]: {e}")

        return items

    async def _search_policy(self, target_date: str) -> list[RawItem]:
        """搜索相关政策，解析结果"""
        items = []
        policy_queries = ["餐饮监管", "食品安全政策", "餐饮行业标准"]

        for query in policy_queries:
            try:
                url = self.build_search_url(query)
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = await self.client.get(url, headers=headers)

                if resp.status_code == 200:
                    news_items = self._parse_news_results(resp.text, query)
                    for news in news_items:
                        item = RawItem(
                            source="监管公告",
                            url=news.get("url", url),
                            title=news.get("title", f"政策动态: {query}"),
                            content=news.get("summary", ""),
                            published_at=news.get("date", target_date),
                            category="行业趋势/政策",
                            keywords=extract_keywords(news.get("title", "")),
                            metadata={"query": query, "source_type": "policy_search"},
                        )
                        items.append(item)
            except Exception as e:
                logger.debug(f"政策搜索失败 [{query}]: {e}")

        return items

    async def close(self):
        await self.client.aclose()
