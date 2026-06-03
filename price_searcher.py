import asyncio
import aiohttp
import re
import json
import logging
import os
from urllib.parse import urlparse, quote_plus

logger = logging.getLogger(__name__)

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _proxied(url: str) -> str:
    """Wrap a URL through ScraperAPI if key is set."""
    if not SCRAPER_API_KEY:
        return url
    return f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}&url={quote_plus(url)}&render=false"


class PriceSearcher:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=40)

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

            prices = await self._search_aliexpress(product_name or "product")
            prices = self._sort_by_price(prices)

            return {
                "success": True,
                "product_name": product_name or "מוצר מהתמונה",
                "prices": prices
            }
        except Exception as e:
            logger.error(f"search_prices error: {e}")
            return {"success": False, "error": f"שגיאה בחיפוש: {str(e)}"}

    # ------------------------------------------------------------------ #
    #  Extract product name + image URL from any product page             #
    # ------------------------------------------------------------------ #
    async def _extract_from_url(self, url: str) -> dict:
        try:
            fetch_url = _proxied(url)
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url, allow_redirects=True) as response:
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
            return {"success": True, "name": name, "image_url": image_url}

        except asyncio.TimeoutError:
            name = self._name_from_url(url)
            if name:
                return {"success": True, "name": name, "image_url": None}
            return {"success": False, "error": "הדף לקח יותר מדי זמן לטעון"}
        except Exception as e:
            logger.error(f"_extract_from_url: {e}")
            name = self._name_from_url(url)
            if name:
                return {"success": True, "name": name, "image_url": None}
            return {"success": False, "error": "לא ניתן לגשת לדף."}

    # ------------------------------------------------------------------ #
    #  Search AliExpress                                                  #
    # ------------------------------------------------------------------ #
    async def _search_aliexpress(self, product_name: str) -> list:
        from bs4 import BeautifulSoup
        query = quote_plus(product_name)
        ali_url = f"https://www.aliexpress.com/wholesale?SearchText={query}&SortType=total_tranpro_desc&page=1"
        fetch_url = _proxied(ali_url)

        if not SCRAPER_API_KEY:
            logger.warning("No SCRAPER_API_KEY — returning fallback links")
            return self._fallback_links(product_name)

        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url) as response:
                    if response.status != 200:
                        logger.warning(f"AliExpress search returned {response.status}")
                        return self._fallback_links(product_name)
                    html = await response.text()

            soup = BeautifulSoup(html, "html.parser")
            results = []

            # Try JSON embedded in page (most reliable)
            for script in soup.find_all("script"):
                text = script.string or ""
                # AliExpress embeds product data as window._dida_config_ or similar
                match = re.search(r'"mods":\s*\{[^}]*"itemList"[^}]*"content":\s*(\[[\s\S]*?\])\s*[,\}]', text)
                if match:
                    try:
                        items = json.loads(match.group(1))
                        for item in items[:5]:
                            price_info = item.get("prices", {}).get("salePrice", {})
                            price = price_info.get("formattedPrice", "")
                            item_id = item.get("itemId", "")
                            title = item.get("title", {})
                            if isinstance(title, dict):
                                title = title.get("displayTitle", "")
                            link = f"https://www.aliexpress.com/item/{item_id}.html" if item_id else ""
                            if price and link:
                                results.append({"store": "AliExpress", "price": price, "link": link, "title": str(title)[:80]})
                        if results:
                            break
                    except Exception:
                        pass

            # Fallback: parse HTML product cards
            if not results:
                cards = soup.select("a[href*='/item/']")
                seen_ids = set()
                for card in cards:
                    href = card.get("href", "")
                    id_match = re.search(r'/item/(\d+)', href)
                    if not id_match:
                        continue
                    item_id = id_match.group(1)
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)

                    # Find price near this card
                    price_el = card.find(class_=re.compile(r'price|Price'))
                    price_text = price_el.get_text(strip=True) if price_el else ""
                    title_el = card.find(class_=re.compile(r'title|Title|name|Name')) or card.find("h3") or card.find("h2")
                    title_text = title_el.get_text(strip=True) if title_el else ""

                    if re.search(r'[\d]', price_text):
                        link = href if href.startswith("http") else "https://www.aliexpress.com" + href
                        results.append({"store": "AliExpress", "price": price_text[:30], "link": link, "title": title_text[:80]})
                    if len(results) >= 5:
                        break

            if not results:
                return self._fallback_links(product_name)

            return results[:5]

        except Exception as e:
            logger.error(f"_search_aliexpress: {e}")
            return self._fallback_links(product_name)

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #
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
