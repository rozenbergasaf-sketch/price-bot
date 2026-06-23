import asyncio
import aiohttp
import re
import json
import logging
import os
from urllib.parse import urlparse, quote_plus

logger = logging.getLogger(__name__)

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()


def _proxied(url: str) -> str:
    if not SCRAPER_API_KEY:
        return url
    return f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}&render=true&country_code=us&url={quote_plus(url)}"


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class PriceSearcher:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=60)
        if SCRAPER_API_KEY:
            logger.info(f"✅ SCRAPER_API_KEY loaded (starts: {SCRAPER_API_KEY[:6]}...)")
        else:
            logger.error("❌ SCRAPER_API_KEY not set!")

    async def search_prices(self, product_name: str = None, image_bytes: bytes = None, image_mime: str = None) -> dict:
        try:
            if product_name and product_name.startswith("http"):
                page_info = await self._extract_from_url(product_name)
                if not page_info["success"]:
                    return page_info
                product_name = page_info["name"]

            if not product_name:
                return {"success": False, "error": "לא סופק מוצר לחיפוש"}

            logger.info(f"Searching Amazon for: '{product_name}'")
            prices = await self._search_amazon(product_name)
            prices = self._sort_by_price(prices)
            logger.info(f"Found {len(prices)} results")

            return {"success": True, "product_name": product_name, "prices": prices}
        except Exception as e:
            logger.error(f"search_prices error: {e}")
            return {"success": False, "error": f"שגיאה: {str(e)}"}

    async def _extract_from_url(self, url: str) -> dict:
        try:
            fetch_url = _proxied(url)
            logger.info(f"Fetching: {url[:80]}")
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url, allow_redirects=True) as response:
                    logger.info(f"Page status: {response.status}")
                    if response.status != 200:
                        return {"success": False, "error": f"לא ניתן לגשת לדף (קוד {response.status})"}
                    html = await response.text()

            name = self._extract_name_from_html(html, url)
            logger.info(f"Extracted name: '{name[:80]}'")
            if not name:
                return {"success": False, "error": "לא הצלחתי לחלץ את שם המוצר — נסה לשלוח את שם המוצר ישירות"}
            return {"success": True, "name": name}

        except asyncio.TimeoutError:
            return {"success": False, "error": "הדף לקח יותר מדי זמן לטעון"}
        except Exception as e:
            logger.error(f"_extract_from_url: {e}")
            return {"success": False, "error": "לא ניתן לגשת לדף."}

    def _extract_name_from_html(self, html: str, url: str) -> str:
        domain = urlparse(url).netloc.lower()

        if "amazon" in domain:
            for pattern in [
                r'id="productTitle"[^>]*>\s*([^<]{10,300})',
                r'"title"\s*:\s*"([^"]{10,300})"',
            ]:
                m = re.search(pattern, html)
                if m:
                    return m.group(1).strip()

        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>', html, re.I):
            try:
                data = json.loads(m.group(1))
                if isinstance(data, list):
                    data = next((d for d in data if isinstance(d, dict) and d.get("@type") == "Product"), {})
                if isinstance(data, dict) and data.get("@type") == "Product":
                    name = data.get("name", "")
                    if name and len(name) > 5:
                        return name[:200]
            except Exception:
                pass

        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']{10,300})["\']', html, re.I)
        if m:
            t = m.group(1).strip()
            if not any(s in t.lower() for s in ["amazon.com", "aliexpress"]):
                return self._clean(t)

        m = re.search(r'<title[^>]*>([^<]{10,300})</title>', html, re.I)
        if m:
            return self._clean(m.group(1).strip())

        return ""

    async def _search_amazon(self, product_name: str) -> list:
        if not SCRAPER_API_KEY:
            return self._fallback_links(product_name)

        query = quote_plus(product_name)
        amazon_url = f"https://www.amazon.com/s?k={query}&s=price-asc-rank"
        fetch_url = _proxied(amazon_url)

        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url) as response:
                    logger.info(f"Amazon search status: {response.status}")
                    if response.status != 200:
                        return self._fallback_links(product_name)
                    html = await response.text()

            logger.info(f"HTML length={len(html)}, has 'a-price-whole': {'a-price-whole' in html}, has 'data-asin': {'data-asin' in html}")
            results = self._parse_amazon_html(html)
            logger.info(f"Parsed {len(results)} results")

            if not results:
                keys = ["data-asin", "a-price-whole", "a-price-fraction", "a-offscreen", "priceblock"]
                found = {k: html.count(k) for k in keys if k in html}
                logger.warning(f"No results. Keywords: {found}")
                chunk = re.sub(r'\s+', ' ', html[3000:5000])
                logger.warning(f"HTML sample: {chunk[:600]}")
                return self._fallback_links(product_name)

            return results[:5]

        except Exception as e:
            logger.error(f"_search_amazon: {e}")
            return self._fallback_links(product_name)

    def _parse_amazon_html(self, html: str) -> list:
        results = []

        # Strategy 1: data-asin blocks with price
        asin_blocks = re.findall(
            r'data-asin="([A-Z0-9]{10})"[\s\S]{0,3000}?'
            r'<span class="a-price-whole">([\d,]+)</span>'
            r'[\s\S]{0,200}?<span class="a-price-fraction">(\d+)</span>',
            html
        )

        seen = set()
        for asin, whole, fraction in asin_blocks:
            if not asin or asin in seen:
                continue
            seen.add(asin)
            price = f"${whole.replace(',', '')}.{fraction}"
            link = f"https://www.amazon.com/dp/{asin}"

            title = ""
            asin_pos = html.find(f'data-asin="{asin}"')
            if asin_pos >= 0:
                block = html[asin_pos:asin_pos + 2000]
                t = re.search(r'<span[^>]*class="[^"]*a-text-normal[^"]*"[^>]*>([^<]{5,150})</span>', block)
                if not t:
                    t = re.search(r'<h2[^>]*>[\s\S]*?<span[^>]*>([^<]{5,150})</span>', block)
                if t:
                    title = t.group(1).strip()

            results.append({"store": "Amazon", "price": price, "link": link, "title": title[:80]})
            if len(results) >= 5:
                break

        if results:
            logger.info(f"Strategy 1 found {len(results)} items")
            return results

        # Strategy 2: any ASIN + nearby price
        asins = list(dict.fromkeys(re.findall(r'data-asin="([A-Z0-9]{10})"', html)))[:5]
        prices = re.findall(r'\$\s*(\d[\d,]*\.\d{2})', html)

        if asins:
            logger.info(f"Strategy 2: {len(asins)} ASINs, {len(prices)} prices")
            for i, asin in enumerate(asins):
                price = f"${prices[i]}" if i < len(prices) else "מחיר לא זמין"
                results.append({"store": "Amazon", "price": price, "link": f"https://www.amazon.com/dp/{asin}", "title": ""})

        return results

    def _clean(self, name: str) -> str:
        for p in [
            r"\s*[\|\-–]\s*(Amazon|eBay|AliExpress|Walmart).*$",
            r"\s*[\|\-–]\s*[A-Z][a-zA-Z\s]*\.com.*$",
        ]:
            name = re.sub(p, "", name, flags=re.IGNORECASE)
        return name.strip()[:200]

    def _fallback_links(self, product_name: str) -> list:
        query = quote_plus(product_name)
        return [{"store": "Amazon", "price": "לחץ לחיפוש",
                 "link": f"https://www.amazon.com/s?k={query}&s=price-asc-rank",
                 "title": f"חפש: {product_name[:60]}"}]

    def _sort_by_price(self, prices: list) -> list:
        def to_num(p: str) -> float:
            if not p or not re.search(r'\d', p):
                return float("inf")
            nums = re.findall(r'[\d]+\.?\d*', p.replace(",", ""))
            return float(nums[0]) if nums else float("inf")
        return sorted(prices, key=lambda x: to_num(x.get("price", "")))
