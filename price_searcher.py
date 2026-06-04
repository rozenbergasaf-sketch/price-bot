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
        self.timeout = aiohttp.ClientTimeout(total=45)
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

            logger.info(f"Searching AliExpress for: '{product_name}'")
            prices = await self._search_aliexpress(product_name)
            prices = self._sort_by_price(prices)
            logger.info(f"Found {len(prices)} results")

            return {"success": True, "product_name": product_name, "prices": prices}
        except Exception as e:
            logger.error(f"search_prices error: {e}")
            return {"success": False, "error": f"שגיאה: {str(e)}"}

    # ------------------------------------------------------------------ #
    #  Extract product name from URL                                      #
    # ------------------------------------------------------------------ #
    async def _extract_from_url(self, url: str) -> dict:
        # Force English version of AliExpress for better parsing
        en_url = re.sub(r'https?://(he|fr|de|es|ru|pt)\.aliexpress', 'https://www.aliexpress', url)
        en_url = re.sub(r'\?.*', '', en_url)  # strip query params that cause redirects

        try:
            fetch_url = _proxied(en_url)
            logger.info(f"Fetching: {en_url[:80]}")
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url, allow_redirects=True) as response:
                    logger.info(f"Page status: {response.status}")
                    if response.status != 200:
                        return {"success": False, "error": f"לא ניתן לגשת לדף (קוד {response.status})"}
                    html = await response.text()

            name = self._extract_name_from_html(html, en_url)
            logger.info(f"Extracted name: '{name[:60]}'")
            if not name or name.lower() in ("aliexpress", "alibaba"):
                return {"success": False, "error": "לא הצלחתי לחלץ את שם המוצר — נסה לשלוח את שם המוצר ישירות"}
            return {"success": True, "name": name}

        except asyncio.TimeoutError:
            return {"success": False, "error": "הדף לקח יותר מדי זמן לטעון"}
        except Exception as e:
            logger.error(f"_extract_from_url: {e}")
            return {"success": False, "error": "לא ניתן לגשת לדף."}

    def _extract_name_from_html(self, html: str, url: str) -> str:
        """Extract product name — try JSON-LD and meta tags first, avoid og:title on AliExpress."""

        # 1. JSON-LD Product schema (most reliable)
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

        # 2. AliExpress specific: window.runParams or _dida_config_ JSON
        for pattern in [
            r'"subject"\s*:\s*"([^"]{10,200})"',
            r'"title"\s*:\s*"([^"]{10,200})"',
            r'"productTitle"\s*:\s*"([^"]{10,200})"',
            r'"name"\s*:\s*"([^"]{10,200})"',
        ]:
            m = re.search(pattern, html)
            if m:
                candidate = m.group(1)
                # Skip if it looks like a site name or URL
                if not any(skip in candidate.lower() for skip in ["aliexpress", "alibaba", "http", "{"]):
                    return candidate[:200]

        # 3. og:title — but skip AliExpress generic titles
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m:
            t = m.group(1).strip()
            if len(t) > 10 and "aliexpress" not in t.lower() and "alibaba" not in t.lower():
                return self._clean(t)

        # 4. <title> tag
        m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
        if m:
            t = m.group(1).strip()
            if "aliexpress" not in t.lower():
                return self._clean(t)

        return ""

    # ------------------------------------------------------------------ #
    #  Search AliExpress — parse embedded JSON from page                 #
    # ------------------------------------------------------------------ #
    async def _search_aliexpress(self, product_name: str) -> list:
        if not SCRAPER_API_KEY:
            return self._fallback_links(product_name)

        query = quote_plus(product_name)
        ali_url = f"https://www.aliexpress.com/wholesale?SearchText={query}&SortType=total_tranpro_desc&page=1"
        fetch_url = _proxied(ali_url)

        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url) as response:
                    logger.info(f"AliExpress search status: {response.status}")
                    if response.status != 200:
                        return self._fallback_links(product_name)
                    html = await response.text()

            logger.info(f"HTML length={len(html)}")
            results = self._parse_aliexpress_html(html)
            logger.info(f"Parsed {len(results)} results")

            if not results:
                # Dump key info to help debug
                keys = ["itemId","productId","salePrice","formattedPrice","runParams",
                        "window._dida","US $","subject","itemList","skuId"]
                found = {k: html.count(k) for k in keys if k in html}
                logger.warning(f"No results. Keywords in HTML: {found}")
                for i, start in enumerate([0, 3000, 8000]):
                    chunk = re.sub(r'\\s+', ' ', html[start:start+1500])
                    logger.warning(f"HTML chunk[{i}]: {chunk}")
                return self._fallback_links(product_name)

            return results[:5]

        except Exception as e:
            logger.error(f"_search_aliexpress: {e}")
            return self._fallback_links(product_name)

    def _parse_aliexpress_html(self, html: str) -> list:
        results = []

        # ---- Strategy 1: window.runParams JSON (AliExpress search page) ----
        for pattern in [
            r'window\.runParams\s*=\s*(\{[\s\S]*?\});\s*(?:var|window|//)',
            r'"mods"\s*:\s*\{[\s\S]*?"itemList"\s*:\s*\{[\s\S]*?"content"\s*:\s*(\[[\s\S]*?\])\s*[,\}]',
            r'"productList"\s*:\s*(\[[\s\S]*?\])\s*[,\}]',
            r'"items"\s*:\s*(\[[\s\S]{100,50000}?\])\s*[,\}]',
        ]:
            m = re.search(pattern, html)
            if not m:
                continue
            try:
                raw = m.group(1)
                # If it's a full object, dig into it
                data = json.loads(raw)
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    # Walk common paths
                    items = (data.get("mods", {}).get("itemList", {}).get("content") or
                             data.get("data", {}).get("itemList", {}).get("content") or
                             data.get("items") or [])

                for item in items[:5]:
                    if not isinstance(item, dict):
                        continue
                    item_id = str(item.get("itemId") or item.get("productId") or "")
                    title = item.get("title") or item.get("name") or item.get("subject") or ""
                    if isinstance(title, dict):
                        title = title.get("displayTitle") or title.get("seoTitle") or ""

                    # Price — try multiple paths
                    price = ""
                    for path in [
                        lambda x: x.get("salePrice"),
                        lambda x: x.get("prices", {}).get("salePrice", {}).get("formattedPrice"),
                        lambda x: x.get("prices", {}).get("originalPrice", {}).get("formattedPrice"),
                        lambda x: x.get("price", {}).get("formattedPrice") if isinstance(x.get("price"), dict) else x.get("price"),
                    ]:
                        try:
                            v = path(item)
                            if v and re.search(r'\d', str(v)):
                                price = str(v)
                                break
                        except Exception:
                            pass

                    if item_id and price:
                        results.append({
                            "store": "AliExpress",
                            "price": price,
                            "link": f"https://www.aliexpress.com/item/{item_id}.html",
                            "title": str(title)[:80],
                        })
                if results:
                    logger.info(f"Strategy 1 (JSON) found {len(results)} items")
                    return results
            except Exception as je:
                logger.debug(f"JSON parse attempt failed: {je}")

        # ---- Strategy 2: regex over raw HTML for item IDs + prices ----
        item_ids = re.findall(r'/item/(\d{10,20})\.html', html)
        prices_raw = re.findall(r'(?:US\s*\$|€|£)\s*([\d,]+\.?\d*)', html)

        seen = []
        for item_id in dict.fromkeys(item_ids):  # unique, preserve order
            if len(seen) >= 5:
                break
            seen.append(item_id)

        price_list = []
        for p in prices_raw:
            v = p.replace(",", "")
            try:
                if 0.5 < float(v) < 10000:
                    price_list.append(f"US ${v}")
            except Exception:
                pass

        if seen:
            logger.info(f"Strategy 2 (regex) found {len(seen)} item IDs, {len(price_list)} prices")
            for i, item_id in enumerate(seen):
                price = price_list[i] if i < len(price_list) else "מחיר לא זמין"
                results.append({
                    "store": "AliExpress",
                    "price": price,
                    "link": f"https://www.aliexpress.com/item/{item_id}.html",
                    "title": "",
                })
            return results

        return []

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _clean(self, name: str) -> str:
        for p in [
            r"\s*[\|\-–]\s*(Amazon|eBay|AliExpress|Walmart|Target|Etsy).*$",
            r"\s*[\|\-–]\s*[A-Z][a-zA-Z\s]*\.com.*$",
        ]:
            name = re.sub(p, "", name, flags=re.IGNORECASE)
        return name.strip()[:200]

    def _fallback_links(self, product_name: str) -> list:
        query = quote_plus(product_name)
        return [{
            "store": "AliExpress",
            "price": "לחץ לחיפוש",
            "link": f"https://www.aliexpress.com/wholesale?SearchText={query}&SortType=total_tranpro_desc",
            "title": f"חפש: {product_name[:60]}"
        }]

    def _sort_by_price(self, prices: list) -> list:
        def to_num(p: str) -> float:
            if not p or not re.search(r'\d', p):
                return float("inf")
            nums = re.findall(r'[\d]+\.?\d*', p.replace(",", ""))
            return float(nums[0]) if nums else float("inf")
        return sorted(prices, key=lambda x: to_num(x.get("price", "")))
