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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


class PriceSearcher:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=30)

    async def search_prices(self, url: str) -> dict:
        try:
            product_info = await self._extract_product_info(url)
            if not product_info["success"]:
                return product_info

            product_name = product_info["name"]
            logger.info(f"Searching prices for: {product_name}")

            prices = await self._search_aliexpress_via_claude(product_name)
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
                return {"success": False, "error": "לא הצלחתי לחלץ את שם המוצר"}
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

    def _extract_name_from_soup(self, soup, url: str) -> str:
        domain = urlparse(url).netloc.lower()

        site_selectors = {
            "amazon": ["#productTitle", "span#productTitle"],
            "ebay": ["h1.x-item-title__mainTitle", "h1[itemprop='name']"],
            "aliexpress": [".product-title", "h1.product-title-text", "[class*='title--wrap']", "h1"],
            "walmart": ["h1[itemprop='name']", ".prod-ProductTitle"],
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
            r"\s*[\|\-–]\s*(Amazon|eBay|AliExpress|Walmart|Target|Etsy).*$",
            r"\s*[\|\-–]\s*[A-Z][a-zA-Z\s]*\.com.*$",
        ]
        for p in patterns:
            name = re.sub(p, "", name, flags=re.IGNORECASE)
        return name.strip()[:200]

    async def _search_aliexpress_via_claude(self, product_name: str) -> list:
        """Use Claude with web_search to find AliExpress prices."""
        if not ANTHROPIC_API_KEY:
            logger.warning("No ANTHROPIC_API_KEY set")
            return self._fallback_links(product_name)

        prompt = (
            f"Search AliExpress for: {product_name}\n\n"
            "Find the 5 cheapest listings on AliExpress for this product right now.\n"
            "Return ONLY a JSON array with exactly this format, no other text:\n"
            '[\n'
            '  {"title": "short product title", "price": "$X.XX", "url": "https://www.aliexpress.com/item/..."}\n'
            ']\n\n'
            "Rules:\n"
            "- Only AliExpress URLs\n"
            "- Real prices with currency symbol\n"
            "- Sort from cheapest to most expensive\n"
            "- Return maximum 5 items\n"
            "- If you cannot find real results, return empty array []"
        )

        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}]
        }

        headers = {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers=headers
                ) as response:
                    if response.status != 200:
                        logger.error(f"Anthropic API error: {response.status}")
                        return self._fallback_links(product_name)
                    data = await response.json()

            # Extract text from response
            full_text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    full_text += block.get("text", "")

            # Parse JSON from response
            json_match = re.search(r'\[.*?\]', full_text, re.DOTALL)
            if not json_match:
                logger.warning("No JSON array found in Claude response")
                return self._fallback_links(product_name)

            items = json.loads(json_match.group())
            results = []
            for item in items[:5]:
                if item.get("price") and item.get("url"):
                    results.append({
                        "store": "AliExpress",
                        "price": item["price"],
                        "link": item["url"],
                        "title": item.get("title", "")[:80],
                    })

            if not results:
                return self._fallback_links(product_name)

            return results

        except Exception as e:
            logger.error(f"Claude search error: {e}")
            return self._fallback_links(product_name)

    def _fallback_links(self, product_name: str) -> list:
        query = quote_plus(product_name)
        return [{
            "store": "AliExpress",
            "price": "לחץ לחיפוש",
            "link": f"https://www.aliexpress.com/wholesale?SearchText={query}&SortType=total_tranpro_desc",
            "title": f"חפש: {product_name[:60]}"
        }]

    def _sort_by_price(self, prices: list) -> list:
        def extract_number(price_str: str) -> float:
            if not price_str or not re.search(r'\d', price_str):
                return float("inf")
            nums = re.findall(r'[\d]+\.?\d*', price_str.replace(",", ""))
            return float(nums[0]) if nums else float("inf")
        return sorted(prices, key=lambda x: extract_number(x.get("price", "")))
