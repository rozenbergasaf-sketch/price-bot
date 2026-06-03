import asyncio
import aiohttp
import re
import json
import logging
import os
from urllib.parse import urlparse, quote_plus

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SERP_API_KEY = os.environ.get("SERPAPI_KEY", "")


class PriceSearcher:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=25)

    async def search_prices(self, url: str) -> dict:
        try:
            product_info = await self._extract_product_info(url)
            if not product_info["success"]:
                return product_info

            product_name = product_info["name"]
            logger.info(f"Searching prices for: {product_name}")

            if SERP_API_KEY:
                prices = await self._search_via_serpapi(product_name)
            else:
                prices = await self._search_fallback(product_name)

            prices = self._sort_by_price(prices)

            return {
                "success": True,
                "product_name": product_name,
                "prices": prices
            }
        except Exception as e:
            logger.error(f"search_prices error: {e}")
            return {"success": False, "error": f"שגיאה בחיפוש: {str(e)}"}

    async def _extract_product_info(self, url: str) -> dict:
        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status != 200:
                        # Try to extract product name from URL itself as fallback
                        name = self._extract_name_from_url(url)
                        if name:
                            return {"success": True, "name": name}
                        return {"success": False, "error": f"לא ניתן לגשת לדף (קוד {response.status})"}
                    html = await response.text()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            name = self._extract_name_from_soup(soup, url)
            if not name:
                name = self._extract_name_from_url(url)
            if not name:
                return {"success": False, "error": "לא הצלחתי לחלץ את שם המוצר מהדף"}
            return {"success": True, "name": name}
        except asyncio.TimeoutError:
            name = self._extract_name_from_url(url)
            if name:
                return {"success": True, "name": name}
            return {"success": False, "error": "הדף לקח יותר מדי זמן לטעון"}
        except Exception as e:
            logger.error(f"_extract_product_info error: {e}")
            name = self._extract_name_from_url(url)
            if name:
                return {"success": True, "name": name}
            return {"success": False, "error": "לא ניתן לגשת לדף. בדוק שהלינק תקין."}

    def _extract_name_from_url(self, url: str) -> str:
        """Try to get a product name from the URL path itself."""
        try:
            path = urlparse(url).path
            # AliExpress: /item/1005009865727415.html -> not useful
            # Amazon: /dp/B08XYZ/ref=... -> not useful
            # But some URLs have product names in them
            segments = [s for s in path.split("/") if len(s) > 10 and not s.isdigit()]
            if segments:
                name = segments[-1].replace("-", " ").replace("_", " ")
                name = re.sub(r'\.(html?|php|aspx?)$', '', name)
                if len(name) > 10:
                    return name[:150]
        except Exception:
            pass
        return ""

    def _extract_name_from_soup(self, soup, url: str) -> str:
        domain = urlparse(url).netloc.lower()

        site_selectors = {
            "amazon": ["#productTitle", "span#productTitle"],
            "ebay": ["h1.x-item-title__mainTitle", "h1[itemprop='name']"],
            "aliexpress": [
                ".product-title",
                "h1.product-title-text",
                "[class*='title--wrap']",
                "[class*='ProductTitle']",
                "h1"
            ],
            "walmart": ["h1[itemprop='name']", ".prod-ProductTitle"],
            "etsy": ["h1[data-buy-box-listing-title]", ".wt-text-body-03"],
        }

        for site_key, selectors in site_selectors.items():
            if site_key in domain:
                for sel in selectors:
                    el = soup.select_one(sel)
                    if el and el.get_text(strip=True):
                        return el.get_text(strip=True)[:200]

        og = soup.find("meta", property="og:title")
        if og and og.get("content") and len(og["content"].strip()) > 5:
            return self._clean_product_name(og["content"].strip())

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

        el = soup.find(attrs={"itemprop": "name"})
        if el:
            text = el.get("content") or el.get_text(strip=True)
            if text and len(text) > 5:
                return text[:200]

        h1 = soup.find("h1")
        if h1 and len(h1.get_text(strip=True)) > 5:
            return h1.get_text(strip=True)[:200]

        title_tag = soup.find("title")
        if title_tag:
            return self._clean_product_name(title_tag.get_text(strip=True))

        return ""

    def _clean_product_name(self, name: str) -> str:
        patterns = [
            r"\s*[\|\-–]\s*(Amazon|eBay|AliExpress|Walmart|Target|Etsy|Buy|Shop).*$",
            r"\s*[\|\-–]\s*[A-Z][a-zA-Z\s]*\.com.*$",
        ]
        for p in patterns:
            name = re.sub(p, "", name, flags=re.IGNORECASE)
        return name.strip()[:200]

    async def _search_via_serpapi(self, product_name: str) -> list:
        """Use SerpAPI to search Google Shopping — reliable, not blocked."""
        query = quote_plus(product_name)
        url = (
            f"https://serpapi.com/search.json"
            f"?engine=google_shopping"
            f"&q={query}"
            f"&api_key={SERP_API_KEY}"
            f"&hl=en&gl=us&num=10"
        )
        results = []
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.warning(f"SerpAPI returned {response.status}")
                        return await self._search_fallback(product_name)
                    data = await response.json()

            shopping_results = data.get("shopping_results", [])
            for item in shopping_results[:8]:
                results.append({
                    "store": item.get("source", "חנות"),
                    "price": item.get("price", ""),
                    "link": item.get("link", ""),
                    "title": item.get("title", "")[:80],
                })

        except Exception as e:
            logger.error(f"SerpAPI error: {e}")
            return await self._search_fallback(product_name)

        return results

    async def _search_fallback(self, product_name: str) -> list:
        """Fallback when no API key: return search links for the user to check manually."""
        query = quote_plus(product_name)
        # Return helpful search links instead of empty results
        return [
            {
                "store": "Google Shopping",
                "price": "לחץ לחיפוש",
                "link": f"https://www.google.com/search?tbm=shop&q={query}",
                "title": product_name[:60]
            },
            {
                "store": "AliExpress",
                "price": "לחץ לחיפוש",
                "link": f"https://www.aliexpress.com/wholesale?SearchText={query}",
                "title": product_name[:60]
            },
            {
                "store": "eBay",
                "price": "לחץ לחיפוש",
                "link": f"https://www.ebay.com/sch/i.html?_nkw={query}&_sop=15",
                "title": product_name[:60]
            },
            {
                "store": "Amazon",
                "price": "לחץ לחיפוש",
                "link": f"https://www.amazon.com/s?k={query}",
                "title": product_name[:60]
            },
        ]

    def _sort_by_price(self, prices: list) -> list:
        def extract_number(price_str: str) -> float:
            if not price_str or not re.search(r'\d', price_str):
                return float("inf")
            nums = re.findall(r'[\d]+\.?\d*', price_str.replace(",", ""))
            return float(nums[0]) if nums else float("inf")
        return sorted(prices, key=lambda x: extract_number(x.get("price", "")))
