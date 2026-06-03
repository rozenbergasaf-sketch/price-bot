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
    return f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}&url={quote_plus(url)}"


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class PriceSearcher:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=45)
        # Log at startup so Railway logs show the key status immediately
        if SCRAPER_API_KEY:
            logger.info(f"✅ SCRAPER_API_KEY loaded (starts with: {SCRAPER_API_KEY[:6]}...)")
        else:
            logger.error("❌ SCRAPER_API_KEY is NOT set — searches will return fallback links only!")

    async def search_prices(self, product_name: str = None, image_bytes: bytes = None, image_mime: str = None) -> dict:
        try:
            image_url = None

            if product_name and product_name.startswith("http"):
                page_info = await self._extract_from_url(product_name)
                if not page_info["success"]:
                    return page_info
                product_name = page_info["name"]
                image_url = page_info.get("image_url")

            if not product_name and not image_bytes and not image_url:
                return {"success": False, "error": "לא סופק מוצר לחיפוש"}

            logger.info(f"Searching AliExpress for: '{product_name}'")
            prices = await self._search_aliexpress(product_name or "product")
            prices = self._sort_by_price(prices)
            logger.info(f"Found {len(prices)} results")

            return {
                "success": True,
                "product_name": product_name or "מוצר",
                "prices": prices
            }
        except Exception as e:
            logger.error(f"search_prices error: {e}")
            return {"success": False, "error": f"שגיאה בחיפוש: {str(e)}"}

    async def _extract_from_url(self, url: str) -> dict:
        try:
            fetch_url = _proxied(url)
            logger.info(f"Fetching product page (proxied={bool(SCRAPER_API_KEY)}): {url[:80]}")
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url, allow_redirects=True) as response:
                    logger.info(f"Product page status: {response.status}")
                    if response.status != 200:
                        name = self._name_from_url(url)
                        if name:
                            return {"success": True, "name": name, "image_url": None}
                        return {"success": False, "error": f"לא ניתן לגשת לדף (קוד {response.status})"}
                    html = await response.text()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            name = self._name_from_soup(soup, url) or self._name_from_url(url)
            if not name:
                return {"success": False, "error": "לא הצלחתי לחלץ את שם המוצר"}
            image_url = self._image_from_soup(soup, url)
            logger.info(f"Extracted name='{name[:60]}' image={'yes' if image_url else 'no'}")
            return {"success": True, "name": name, "image_url": image_url}

        except asyncio.TimeoutError:
            name = self._name_from_url(url)
            if name:
                return {"success": True, "name": name, "image_url": None}
            return {"success": False, "error": "הדף לקח יותר מדי זמן לטעון"}
        except Exception as e:
            logger.error(f"_extract_from_url error: {e}")
            name = self._name_from_url(url)
            if name:
                return {"success": True, "name": name, "image_url": None}
            return {"success": False, "error": "לא ניתן לגשת לדף."}

    async def _search_aliexpress(self, product_name: str) -> list:
        if not SCRAPER_API_KEY:
            logger.error("No SCRAPER_API_KEY — cannot search")
            return self._fallback_links(product_name)

        from bs4 import BeautifulSoup
        query = quote_plus(product_name)
        ali_url = f"https://www.aliexpress.com/wholesale?SearchText={query}&SortType=total_tranpro_desc&page=1"
        fetch_url = _proxied(ali_url)

        logger.info(f"Fetching AliExpress search page...")
        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url) as response:
                    logger.info(f"AliExpress search status: {response.status}")
                    if response.status != 200:
                        body = await response.text()
                        logger.error(f"AliExpress search failed: {body[:200]}")
                        return self._fallback_links(product_name)
                    html = await response.text()

            logger.info(f"Got HTML, length={len(html)}, has 'price': {'price' in html.lower()}")
            soup = BeautifulSoup(html, "html.parser")
            results = []

            # Strategy 1: JSON embedded in page scripts
            for script in soup.find_all("script"):
                text = script.string or ""
                if "itemList" not in text and "mods" not in text:
                    continue
                # Try several JSON extraction patterns
                for pattern in [
                    r'"content":\s*(\[[\s\S]{50,5000}?\])\s*[,\}]',
                    r'window\._dida_config_.*?"items":\s*(\[[\s\S]{50,5000}?\])',
                    r'"productList":\s*(\[[\s\S]{50,5000}?\])',
                ]:
                    match = re.search(pattern, text)
                    if match:
                        try:
                            items = json.loads(match.group(1))
                            for item in items[:5]:
                                if not isinstance(item, dict):
                                    continue
                                # Try different price paths
                                price = (
                                    item.get("salePrice") or
                                    (item.get("prices") or {}).get("salePrice", {}).get("formattedPrice") or
                                    (item.get("prices") or {}).get("originalPrice", {}).get("formattedPrice") or
                                    ""
                                )
                                item_id = item.get("itemId") or item.get("productId") or ""
                                title = item.get("title") or item.get("name") or ""
                                if isinstance(title, dict):
                                    title = title.get("displayTitle") or title.get("seoTitle") or ""
                                link = f"https://www.aliexpress.com/item/{item_id}.html" if item_id else ""
                                if price and link:
                                    results.append({"store": "AliExpress", "price": str(price), "link": link, "title": str(title)[:80]})
                            if results:
                                logger.info(f"Found {len(results)} items via JSON in script")
                                break
                        except Exception as je:
                            logger.debug(f"JSON parse failed: {je}")
                if results:
                    break

            # Strategy 2: Parse HTML product cards
            if not results:
                logger.info("Trying HTML card parsing...")
                seen = set()
                for a in soup.select("a[href*='/item/']"):
                    href = a.get("href", "")
                    m = re.search(r'/item/(\d+)', href)
                    if not m or m.group(1) in seen:
                        continue
                    seen.add(m.group(1))
                    price_el = a.find(string=re.compile(r'[\$€£]\s*[\d,]+\.?\d*'))
                    if not price_el:
                        price_el = a.find(class_=re.compile(r'price', re.I))
                    price_text = price_el.get_text(strip=True) if hasattr(price_el, 'get_text') else str(price_el or "")
                    if not re.search(r'\d', price_text):
                        continue
                    title_el = a.find(class_=re.compile(r'title|name', re.I)) or a.find("h3") or a.find("h2")
                    title_text = title_el.get_text(strip=True) if title_el else ""
                    link = href if href.startswith("http") else "https://www.aliexpress.com" + href
                    results.append({"store": "AliExpress", "price": price_text[:30], "link": link, "title": title_text[:80]})
                    if len(results) >= 5:
                        break
                logger.info(f"HTML card parsing found {len(results)} items")

            if not results:
                logger.warning("No results found — returning fallback")
                return self._fallback_links(product_name)

            return results[:5]

        except Exception as e:
            logger.error(f"_search_aliexpress error: {e}")
            return self._fallback_links(product_name)

    # -------------------- helpers ------------------------------------ #
    def _name_from_url(self, url: str) -> str:
        try:
            path = urlparse(url).path
            segments = [s for s in path.split("/") if len(s) > 10 and not s.isdigit()]
            if segments:
                name = segments[-1].replace("-", " ").replace("_", " ")
                name = re.sub(r'\.(html?|php|aspx?)$', '', name)
                if len(name) > 10:
                    return name[:150]
        except Exception:
            pass
        return ""

    def _name_from_soup(self, soup, url: str) -> str:
        domain = urlparse(url).netloc.lower()
        site_selectors = {
            "amazon":     ["#productTitle", "span#productTitle"],
            "ebay":       ["h1.x-item-title__mainTitle", "h1[itemprop='name']"],
            "aliexpress": [".product-title", "h1.product-title-text", "[class*='title--wrap']", "h1"],
            "walmart":    ["h1[itemprop='name']", ".prod-ProductTitle"],
        }
        for site_key, selectors in site_selectors.items():
            if site_key in domain:
                for sel in selectors:
                    el = soup.select_one(sel)
                    if el and el.get_text(strip=True):
                        return el.get_text(strip=True)[:200]
        og = soup.find("meta", property="og:title")
        if og and og.get("content") and len(og["content"].strip()) > 5:
            return self._clean(og["content"])
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    if data.get("@type") in ("Product", "ItemPage") and data.get("name"):
                        return data["name"][:200]
                    for item in (data.get("@graph") or []):
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            return item.get("name", "")[:200]
            except Exception:
                pass
        h1 = soup.find("h1")
        if h1 and len(h1.get_text(strip=True)) > 5:
            return h1.get_text(strip=True)[:200]
        title = soup.find("title")
        if title:
            return self._clean(title.get_text(strip=True))
        return ""

    def _image_from_soup(self, soup, url: str) -> str:
        domain = urlparse(url).netloc.lower()
        site_selectors = {
            "amazon":     ["#landingImage", "#imgBlkFront"],
            "ebay":       [".ux-image-carousel-item img", "#icImg"],
            "aliexpress": [".product-image img", "[class*='product-image'] img"],
            "walmart":    ["[data-testid='hero-image'] img"],
        }
        for site_key, selectors in site_selectors.items():
            if site_key in domain:
                for sel in selectors:
                    el = soup.select_one(sel)
                    if el:
                        src = el.get("src") or el.get("data-src") or ""
                        if src.startswith("http"):
                            return src
        og = soup.find("meta", property="og:image")
        if og and og.get("content", "").startswith("http"):
            return og["content"]
        return ""

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
