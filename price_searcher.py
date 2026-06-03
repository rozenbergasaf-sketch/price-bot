import asyncio
import aiohttp
import re
import logging
from urllib.parse import urlparse, quote_plus
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class PriceSearcher:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=20)

    async def search_prices(self, url: str) -> dict:
        """Main entry point: extract product name then search for prices."""
        try:
            product_info = await self._extract_product_info(url)
            if not product_info["success"]:
                return product_info
            
            product_name = product_info["name"]
            logger.info(f"Searching prices for: {product_name}")
            
            prices = await self._search_google_shopping(product_name)
            
            # Sort by price
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
        """Extract product name from the given URL by scraping the page."""
        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status != 200:
                        return {"success": False, "error": f"לא ניתן לגשת לדף (קוד {response.status})"}
                    
                    html = await response.text()
                    soup = BeautifulSoup(html, "html.parser")
                    
                    name = self._extract_name_from_soup(soup, url)
                    
                    if not name:
                        return {"success": False, "error": "לא הצלחתי לחלץ את שם המוצר מהדף"}
                    
                    return {"success": True, "name": name}
        except asyncio.TimeoutError:
            return {"success": False, "error": "הדף לקח יותר מדי זמן לטעון"}
        except Exception as e:
            logger.error(f"_extract_product_info error: {e}")
            return {"success": False, "error": "לא ניתן לגשת לדף. בדוק שהלינק תקין."}

    def _extract_name_from_soup(self, soup: BeautifulSoup, url: str) -> str:
        """Try multiple strategies to extract product name."""
        domain = urlparse(url).netloc.lower()
        
        # Strategy 1: Site-specific selectors
        site_selectors = {
            "amazon": ["#productTitle", "span#productTitle", ".product-title"],
            "ebay": ["h1.x-item-title__mainTitle", ".it-ttl", "h1[itemprop='name']"],
            "aliexpress": [".product-title", "h1.product-title-text", ".title--wrap--UUHae_g"],
            "walmart": ["h1[itemprop='name']", ".prod-ProductTitle"],
            "target": ["h1[data-test='product-title']"],
            "etsy": [".wt-text-body-03.wt-text-bold", "h1.wt-text-body-01"],
        }
        
        for site_key, selectors in site_selectors.items():
            if site_key in domain:
                for sel in selectors:
                    el = soup.select_one(sel)
                    if el and el.get_text(strip=True):
                        return el.get_text(strip=True)[:200]
        
        # Strategy 2: OpenGraph / meta tags
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
            if len(title) > 5:
                return self._clean_product_name(title)
        
        # Strategy 3: JSON-LD structured data
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    if data.get("@type") in ("Product", "ItemPage") and data.get("name"):
                        return data["name"][:200]
                    if isinstance(data.get("@graph"), list):
                        for item in data["@graph"]:
                            if isinstance(item, dict) and item.get("@type") == "Product":
                                return item.get("name", "")[:200]
            except Exception:
                pass
        
        # Strategy 4: Itemprop
        el = soup.find(attrs={"itemprop": "name"})
        if el:
            text = el.get("content") or el.get_text(strip=True)
            if text and len(text) > 5:
                return text[:200]
        
        # Strategy 5: h1 tag
        h1 = soup.find("h1")
        if h1:
            text = h1.get_text(strip=True)
            if len(text) > 5:
                return text[:200]
        
        # Strategy 6: Page title
        title_tag = soup.find("title")
        if title_tag:
            return self._clean_product_name(title_tag.get_text(strip=True))
        
        return ""

    def _clean_product_name(self, name: str) -> str:
        """Remove site names and clutter from product titles."""
        # Remove common suffixes like "| Amazon", "- eBay", etc.
        patterns = [
            r"\s*[\|\-–]\s*(Amazon|eBay|AliExpress|Walmart|Target|Etsy|Buy|Shop).*$",
            r"\s*[\|\-–]\s*[A-Z][a-zA-Z\s]*\.com.*$",
            r"\s*:.*$",  # Remove everything after colon if it's site-related
        ]
        for p in patterns:
            name = re.sub(p, "", name, flags=re.IGNORECASE)
        
        return name.strip()[:200]

    async def _search_google_shopping(self, product_name: str) -> list:
        """Scrape Google Shopping results for the product."""
        query = quote_plus(product_name)
        url = f"https://www.google.com/search?tbm=shop&q={query}&hl=en"
        
        results = []
        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.warning(f"Google Shopping returned {response.status}")
                        return []
                    
                    html = await response.text()
                    soup = BeautifulSoup(html, "html.parser")
                    
                    # Parse shopping cards
                    cards = soup.select("div.sh-dgr__grid-result, div.g-single, .sh-np__click-target")
                    
                    if not cards:
                        # Try alternative selectors
                        cards = soup.select("[data-docid], .sh-pr__product-results-grid > div")
                    
                    for card in cards[:10]:
                        item = self._parse_shopping_card(card)
                        if item:
                            results.append(item)
                    
                    # Fallback: try inline shopping results
                    if not results:
                        results = self._parse_inline_shopping(soup)
                    
        except Exception as e:
            logger.error(f"Google Shopping scrape error: {e}")
        
        return results

    def _parse_shopping_card(self, card) -> dict | None:
        """Parse a single shopping result card."""
        try:
            # Store name
            store = card.select_one(".sh-sp__seller-name, .aULzUe, .E5ocAb, [class*='seller'], [class*='merchant']")
            store_name = store.get_text(strip=True) if store else None
            
            # Price
            price_el = card.select_one(".a8Pemb, .T14wmb, [class*='price'], span[aria-label*='$'], span[aria-label*='£']")
            price_text = None
            if price_el:
                price_text = price_el.get_text(strip=True)
                if not price_text and price_el.get("aria-label"):
                    price_text = price_el["aria-label"]
            
            # Link
            link_el = card.select_one("a[href]")
            link = None
            if link_el:
                href = link_el.get("href", "")
                if href.startswith("/url?q="):
                    # Google redirect URL
                    match = re.search(r'/url\?q=([^&]+)', href)
                    if match:
                        from urllib.parse import unquote
                        link = unquote(match.group(1))
                elif href.startswith("http"):
                    link = href
            
            if not price_text:
                return None
            
            return {
                "store": store_name or "חנות",
                "price": price_text,
                "link": link or "",
            }
        except Exception:
            return None

    def _parse_inline_shopping(self, soup: BeautifulSoup) -> list:
        """Alternative parser for Google Shopping inline results."""
        results = []
        
        # Look for price patterns near merchant names
        price_pattern = re.compile(r'[\$£€₪]\s*[\d,]+\.?\d*|\d+[\.,]\d+\s*[\$£€₪]')
        
        for span in soup.find_all("span"):
            text = span.get_text(strip=True)
            if price_pattern.match(text) and len(text) < 20:
                parent = span.find_parent("div")
                if parent:
                    all_text = parent.get_text(separator="|", strip=True)
                    parts = all_text.split("|")
                    store = next((p for p in parts if len(p) > 3 and not price_pattern.match(p)), "חנות")
                    results.append({
                        "store": store[:50],
                        "price": text,
                        "link": ""
                    })
                
                if len(results) >= 8:
                    break
        
        return results

    def _sort_by_price(self, prices: list) -> list:
        """Sort prices from lowest to highest."""
        def extract_number(price_str: str) -> float:
            if not price_str:
                return float("inf")
            nums = re.findall(r'[\d,]+\.?\d*', price_str.replace(",", ""))
            return float(nums[0]) if nums else float("inf")
        
        return sorted(prices, key=lambda x: extract_number(x.get("price", "")))