"""
Part 4 采集器: 用户声量快报
数据来源: 微博/抖音/百度搜索（全部公开可验证）
输出: 品牌社媒声量对比 + 用户评价摘要 + 品牌库变动
"""
import asyncio
import httpx
from datetime import date, timedelta
from loguru import logger

from collector.base import BaseCollector, RawItem
import config


class VoiceSnapshotCollector(BaseCollector):
    """用户声量快报采集器 — Part 4 固定板块"""

    source_id = "voice_snapshot"
    source_name = "用户声量快报"
    priority = 2
    circle_scope = []

    TRACK_BRANDS = ["三品王", "柳螺香", "粉之都", "尝不忘", "螺公堂", "王味螺"]

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def collect(self, target_date: str) -> list[RawItem]:
        items = []

        # 1. 社媒声量对比
        voice_items = await self._collect_social_voice(target_date)
        items.extend(voice_items)

        # 2. 三品王用户评价
        review_item = await self._collect_reviews(target_date)
        if review_item:
            items.append(review_item)

        # 3. 品牌库变动
        lib_item = await self._collect_library_changes(target_date)
        if lib_item:
            items.append(lib_item)

        logger.info(f"[用户声量] 采集完成: {len(items)} 条")
        return items

    async def _collect_social_voice(self, target_date: str) -> list[RawItem]:
        """采集品牌社媒声量"""
        items = []
        for brand in self.TRACK_BRANDS:
            voice_data = {}

            # 微博搜索
            try:
                url = f"https://s.weibo.com/weibo?q={brand}"
                resp = await self.client.get(url)
                if resp.status_code == 200:
                    # 从搜索结果页提取结果数量
                    text = resp.text
                    if '找到约' in text:
                        import re
                        m = re.search(r'找到约\s*([\d,]+)\s*条', text)
                        if m:
                            voice_data['微博'] = m.group(1).replace(',', '')
                        else:
                            voice_data['微博'] = self._estimate_from_html(text, '微博')
                    else:
                        voice_data['微博'] = self._estimate_from_html(text, '微博')
            except Exception as e:
                logger.debug(f"微博搜索[{brand}]: {e}")
                voice_data['微博'] = '—'

            # 抖音搜索
            try:
                url = f"https://www.douyin.com/search/{brand}"
                resp = await self.client.get(url)
                if resp.status_code == 200:
                    voice_data['抖音'] = self._estimate_from_html(resp.text, '抖音')
            except Exception as e:
                logger.debug(f"抖音搜索[{brand}]: {e}")
                voice_data['抖音'] = '—'

            # 百度搜索
            try:
                url = f"https://www.baidu.com/s?wd={brand}"
                resp = await self.client.get(url)
                if resp.status_code == 200:
                    text = resp.text
                    if '百度为您找到相关结果约' in text:
                        import re
                        m = re.search(r'百度为您找到相关结果约\s*([\d,]+)\s*个', text)
                        if m:
                            voice_data['百度'] = m.group(1).replace(',', '')
                        else:
                            voice_data['百度'] = self._estimate_from_html(text, '百度')
                    else:
                        voice_data['百度'] = self._estimate_from_html(text, '百度')
            except Exception as e:
                logger.debug(f"百度搜索[{brand}]: {e}")
                voice_data['百度'] = '—'

            # 构建声量内容
            content_parts = [f"{brand}:"]
            for platform, count in voice_data.items():
                content_parts.append(f"  {platform}: {count}")
            content = "\n".join(content_parts)

            item = RawItem(
                source="社媒声量",
                url=f"https://s.weibo.com/weibo?q={brand}",
                title=f"社媒声量: {brand}",
                content=content,
                published_at=target_date,
                brand_name=brand,
                circle="",
                category="用户声量",
                keywords=[brand, "声量"],
                metadata={
                    "brand": brand,
                    "voice_data": voice_data,
                    "platforms": list(voice_data.keys()),
                },
            )
            items.append(item)

        return items

    async def _collect_reviews(self, target_date: str) -> RawItem:
        """采集三品王近5天用户评价摘要"""
        try:
            # 百度搜索三品王最新评价
            url = f"https://www.baidu.com/s?tn=news&word=三品王+好吃+推荐&rtt=4"
            resp = await self.client.get(url)
            reviews_found = []

            if resp.status_code == 200:
                text = resp.text
                # 尝试从搜索结果提取评价片段
                import re
                # 匹配搜索结果中的摘要文字
                snippets = re.findall(r'<span class="content-right_[^"]*">([^<]{15,80})</span>', text)
                if snippets:
                    # 筛选正面和负面
                    positive_words = ['好吃', '推荐', '不错', '喜欢', '赞', '绝了', '爱']
                    negative_words = ['少', '贵', '差', '慢', '失望', '一般']
                    pos = [s for s in snippets if any(w in s for w in positive_words)]
                    neg = [s for s in snippets if any(w in s for w in negative_words)]
                    if pos:
                        reviews_found.append(("✅", pos[0]))
                    if neg:
                        reviews_found.append(("⚠️", neg[0]))

            content = "三品王近5天用户评价摘要:\n"
            if reviews_found:
                for icon, review in reviews_found[:2]:
                    content += f"  {icon} \"{review}\"\n"
            else:
                content += "  (暂无显著评价变动)\n"

            return RawItem(
                source="用户评价",
                url=url,
                title="三品王用户评价摘要",
                content=content,
                published_at=target_date,
                brand_name="三品王",
                circle="自身品牌",
                category="用户声量",
                keywords=["评价", "三品王"],
                metadata={"reviews": reviews_found},
            )
        except Exception as e:
            logger.debug(f"评价采集: {e}")
            return RawItem(
                source="用户评价",
                title="三品王用户评价摘要",
                content="三品王近5天用户评价摘要:\n  (采集暂不可用)\n",
                published_at=target_date,
                brand_name="三品王",
                circle="自身品牌",
                category="用户声量",
                keywords=["评价"],
                metadata={},
            )

    async def _collect_library_changes(self, target_date: str) -> RawItem:
        """品牌库变动简报"""
        try:
            lib = config.load_brand_library()
            brands = lib.get("brands", [])

            # 统计最近更新的品牌
            recent_updates = 0
            for b in brands:
                verified = b.get("verified_at", "")
                if verified >= (date.today() - timedelta(days=7)).isoformat():
                    recent_updates += 1

            # 统计置信度分布
            confidence = {"高": 0, "中": 0, "低": 0}
            for b in brands:
                c = b.get("confidence", "中")
                confidence[c] = confidence.get(c, 0) + 1

            content = (
                f"品牌库变动简报:\n"
                f"  品牌总数: {len(brands)}\n"
                f"  本周更新: {recent_updates} 个品牌\n"
                f"  置信度: 高={confidence['高']} | 中={confidence['中']} | 低={confidence['低']}\n"
                f"  待复核: {confidence['低']} 个低置信度品牌"
            )

            return RawItem(
                source="品牌库",
                title="品牌库变动简报",
                content=content,
                published_at=target_date,
                category="用户声量",
                keywords=["品牌库", "变动"],
                metadata={
                    "total": len(brands),
                    "recent_updates": recent_updates,
                    "confidence": confidence,
                },
            )
        except Exception as e:
            logger.debug(f"品牌库采集: {e}")
            return RawItem(
                source="品牌库",
                title="品牌库变动简报",
                content="品牌库变动简报:\n  (采集暂不可用)\n",
                published_at=target_date,
                category="用户声量",
                keywords=["品牌库"],
                metadata={},
            )

    def _estimate_from_html(self, html: str, platform: str) -> str:
        """从HTML估算搜索结果数量"""
        import re
        patterns = {
            '微博': [r'找到约\s*([\d,]+)\s*条', r'共\s*([\d,]+)\s*条'],
            '抖音': [r'results":(\d+)'],
            '百度': [r'百度为您找到相关结果约\s*([\d,]+)\s*个', r'找到相关结果数([\d,]+)个'],
        }
        for pattern in patterns.get(platform, []):
            m = re.search(pattern, html)
            if m:
                return m.group(1).replace(',', '')
        return '—'

    async def close(self):
        await self.client.aclose()
