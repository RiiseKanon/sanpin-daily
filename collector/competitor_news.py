"""
Part 2 采集器: 竞品动态新闻搜索

采集策略:
  1. 逐层扩展: 核心竞品43→区域竞品69→场景竞品8→替代竞品80，直至Part 2 ≥5条动态
  2. 纯品牌名搜索: 搜索时只使用品牌名，不附加品类限定词（如"米粉""螺蛳粉"等）
     目的: 避免品类词过滤掉品牌在跨界合作、资本运作、人事变动等非品类维度的动态
  3. 每个品牌搜索百度新闻，解析结果提取标题/摘要/链接/日期
  4. 时效窗口: 5天（超过5天的内容自动丢弃）
  5. 硬性规则: 不满5条则必须搜完全部201个品牌，不允许提前终止

搜索任务清单格式:
  {"brand_name": "尝不忘", "brand_id": "BR002", "circle": "核心竞品", "query": "尝不忘"}
  query = 纯品牌名，不加任何限定词
"""
import asyncio
import httpx
import re
from datetime import date, timedelta
from loguru import logger
from bs4 import BeautifulSoup

from collector.base import BaseCollector, RawItem
from processor.dedup import extract_keywords
import config


class CompetitorNewsCollector(BaseCollector):
    """竞品动态新闻采集器 — 纯品牌名搜索 + 逐层扩展"""

    source_id = "competitor_news"
    source_name = "竞品动态"
    priority = 1
    circle_scope = ["核心竞品", "区域竞品", "场景竞品", "替代竞品"]

    TARGET_MIN_DYNAMICS = 5  # Part 2 最低5条目标
    MAX_BRANDS_PER_LAYER = 0  # 0 = 不限，搜完该层全部品牌

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)

    def build_search_tasks(self, target_date: str) -> list[dict]:
        """
        生成搜索任务清单
        每个品牌一个任务，query = 纯品牌名，不附加品类词
        """
        all_brands = config.get_all_brands_by_circle()
        tasks = []

        layer_order = ["核心竞品", "区域竞品", "场景竞品", "替代竞品"]

        for circle in layer_order:
            brands = all_brands.get(circle, [])
            for brand in brands:
                tasks.append({
                    "name": brand["name"],
                    "id": brand["id"],
                    "circle": circle,
                    "query": brand["name"],  # 纯品牌名，不加品类限定词
                    "searched": False,
                })

        logger.info(f"[竞品动态] 生成 {len(tasks)} 个搜索任务（纯品牌名模式）")
        return tasks

    async def collect(self, target_date: str) -> list[RawItem]:
        """
        逐层执行竞品搜索，直至满足最低条数或搜完全部品牌。

        每层内的品牌并行搜索（控制并发数），层间串行。
        """
        all_brands = config.get_all_brands_by_circle()
        layer_order = ["核心竞品", "区域竞品", "场景竞品", "替代竞品"]
        all_items = []
        total_searched = 0

        for circle in layer_order:
            brands = all_brands.get(circle, [])
            if not brands:
                continue

            logger.info(f"[竞品动态] 开始搜索 {circle} 层（{len(brands)} 个品牌）...")

            # 并行搜索该层品牌，控制并发为 5
            sem = asyncio.Semaphore(5)

            async def search_one(brand: dict) -> list[RawItem]:
                async with sem:
                    return await self._search_brand(brand, target_date)

            tasks = [search_one(b) for b in brands]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            layer_items = []
            for i, result in enumerate(results):
                if isinstance(result, list):
                    layer_items.extend(result)
                elif isinstance(result, Exception):
                    logger.debug(f"[{brands[i]['name']}] 搜索异常: {result}")

            total_searched += len(brands)
            all_items.extend(layer_items)

            logger.info(f"[竞品动态] {circle} 层完成: {len(layer_items)} 条动态（已搜 {total_searched} 个品牌）")

            # 满足最低条数可提前终止
            if len(all_items) >= self.TARGET_MIN_DYNAMICS:
                logger.info(f"[竞品动态] 已达到最低 {self.TARGET_MIN_DYNAMICS} 条目标，提前终止搜索")
                break

        logger.info(f"[竞品动态] 采集完成: 共 {len(all_items)} 条，搜索 {total_searched} 个品牌")
        return all_items

    async def _search_brand(self, brand: dict, target_date: str) -> list[RawItem]:
        """
        搜索单个品牌的百度新闻，解析结果。

        搜索词: 品牌名（纯品牌名，不加品类限定词）
        解析百度新闻搜索结果页，提取标题/链接/摘要/日期
        """
        items = []
        name = brand["name"]
        bid = brand.get("id", "")

        try:
            # 构建百度新闻搜索 URL
            query = name
            url = f"https://www.baidu.com/s?tn=news&word={query}&rtt={config.BAIDU_TIME_FILTER}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            resp = await self.client.get(url, headers=headers)

            if resp.status_code != 200:
                return items

            # 解析搜索结果
            news_items = self._parse_baidu_news(resp.text, name)

            for news in news_items:
                title = news.get("title", "")
                summary = news.get("summary", "")
                news_url = news.get("url", "")
                pub_date = news.get("date", target_date)

                # 跳过空标题或无实质内容
                if not title or len(title) < 5:
                    continue
                if len(summary) < 10 and len(title) < 15:
                    continue

                # 分类
                category = self._classify_title(title)

                item = RawItem(
                    source="新闻搜索",
                    url=news_url,
                    title=title,
                    content=summary,
                    published_at=pub_date,
                    brand_name=name,
                    brand_id=bid,
                    circle=brand["circle"],
                    category=category,
                    keywords=extract_keywords(title + " " + summary),
                    metadata={
                        "query": name,
                        "source_type": "baidu_news",
                        "search_url": url,
                    },
                )
                items.append(item)

        except asyncio.TimeoutError:
            logger.debug(f"[{name}] 搜索超时")
        except Exception as e:
            logger.debug(f"[{name}] 搜索失败: {e}")

        return items

    def _parse_baidu_news(self, html: str, brand_name: str) -> list[dict]:
        """
        解析百度新闻搜索结果页。

        百度新闻搜索结果结构:
        - 每条结果通常在一个 div.result 或 div.c-container 中
        - 标题在 h3 > a 中
        - 摘要可能在 span.c-abstract 或 div.c-summary 中
        - 来源和日期在 span.c-author 或 div.c-info 中
        """
        results = []
        try:
            soup = BeautifulSoup(html, "html.parser")

            # 方法1: 查找所有 h3 标签（百度新闻搜索结果标题在 h3 中）
            for container in soup.find_all(["div", "li"], class_=re.compile(r"result|c-container|news-item")):
                try:
                    h3 = container.find("h3")
                    if not h3:
                        continue

                    link = h3.find("a")
                    if not link:
                        continue

                    title = link.get_text(strip=True)
                    href = link.get("href", "")

                    if not title or len(title) < 5:
                        continue

                    # 提取摘要
                    summary = ""
                    abstract_elem = container.find(["span", "div"], class_=re.compile(r"abstract|summary|content"))
                    if abstract_elem:
                        summary = abstract_elem.get_text(strip=True)
                    else:
                        # 尝试从容器文本中提取（排除标题和来源行）
                        full_text = container.get_text(separator=" ", strip=True)
                        # 去掉标题部分
                        if title in full_text:
                            remaining = full_text.replace(title, "", 1).strip()
                            # 取前200字符作为摘要
                            if len(remaining) > 20:
                                summary = remaining[:200]

                    # 提取来源和日期
                    pub_date = ""
                    source_info = container.find(["span", "div"], class_=re.compile(r"author|info|source|time"))
                    if source_info:
                        info_text = source_info.get_text(strip=True)
                        # 尝试提取日期
                        date_match = re.search(
                            r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)', info_text
                        )
                        if date_match:
                            pub_date = self._normalize_date(date_match.group(1))
                        # 尝试 "X小时前" / "X天前"
                        hours_match = re.search(r'(\d+)\s*小时前', info_text)
                        days_match = re.search(r'(\d+)\s*天前', info_text)
                        if hours_match:
                            from datetime import date as dt_date
                            pub_date = dt_date.today().isoformat()
                        elif days_match:
                            from datetime import date as dt_date, timedelta
                            d = int(days_match.group(1))
                            pub_date = (dt_date.today() - timedelta(days=d)).isoformat()

                    results.append({
                        "title": title,
                        "url": href,
                        "summary": summary[:300] if summary else "",
                        "date": pub_date,
                    })
                except Exception:
                    continue

            # 方法2: 如果方法1没有结果，尝试更简单的解析
            if not results:
                for h3 in soup.find_all("h3"):
                    link = h3.find("a")
                    if link:
                        title = link.get_text(strip=True)
                        href = link.get("href", "")
                        if title and len(title) >= 5 and brand_name in title:
                            results.append({
                                "title": title,
                                "url": href,
                                "summary": "",
                                "date": "",
                            })

            # 去重（按标题）
            seen = set()
            unique_results = []
            for r in results:
                key = r["title"][:30]
                if key not in seen:
                    seen.add(key)
                    unique_results.append(r)

            return unique_results[:5]  # 每个品牌最多5条

        except Exception as e:
            logger.debug(f"解析百度新闻失败 [{brand_name}]: {e}")
            return []

    def _normalize_date(self, date_str: str) -> str:
        """标准化日期格式为 YYYY-MM-DD"""
        # 2024年7月13日 → 2024-07-13
        m = re.match(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?', date_str)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""

    def _classify_title(self, title: str) -> str:
        """根据标题判断热点类别"""
        if any(w in title for w in ["新品", "新产品", "上市", "推出", "首发"]):
            return "新品牌/新产品出现"
        if any(w in title for w in ["活动", "促销", "优惠", "福利", "打折"]):
            return "高热度营销活动"
        if any(w in title for w in ["价格", "团购", "降价", "涨价", "调价"]):
            return "价格/促销/团购变动"
        if any(w in title for w in ["新店", "加盟", "扩张", "开店", "门店", "开业"]):
            return "渠道/扩张动作"
        if any(w in title for w in ["融资", "上市", "财报", "营收", "业绩", "利润"]):
            return "数据/业绩披露"
        if any(w in title for w in ["投诉", "卫生", "安全", "事故", "曝光", "查处", "罚款"]):
            return "舆情/食安风险"
        if any(w in title for w in ["供应链", "资本", "收购", "投资", "合并", "重组"]):
            return "组织/资本/供应链动态"
        return "行业趋势/政策"

    def _classify_category(self, query: str) -> str:
        """根据搜索词判断热点类别（保留向后兼容）"""
        return self._classify_title(query)

    async def close(self):
        await self.client.aclose()
