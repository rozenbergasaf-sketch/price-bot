import asyncio
import aiohttp
import re
import json
import logging
import os
import base64
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

    async def search_prices(self, product_name: str = None, image_bytes: bytes = None, image_mime: str = None) -> dict:
        try:
            # If given a URL as product_name, extract name + image from the page
            if product_name and product_name.startswith("http"):
                page_info = await self._extract_from_url(product_name)
                if not page_info["success"]:
                    return page_info
                product_name = page_info["name"]
                # Use page image if no image was passed in
                if not image_bytes and page_info.get("image_bytes"):
                    image_bytes = page_info["image_bytes"]
                    image_mime = page_info.get("image_mime", "image/jpeg")

            if not product_name and not image_bytes:
                return {"success": False, "error": "לא סופק מוצר לחיפוש"}

            prices = await self._search_aliexpress_via_claude(
                product_name=product_name,
                image_bytes=image_bytes,
                image_mime=image_mime
            )
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
    #  Extract product name + main image from any product page URL        #
    # ------------------------------------------------------------------ #
    async def _extract_from_url(self, url: str) -> dict:
        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status != 200:
                        name = self._extract_name_from_url(url)
                        if name:
                            return {"success": True, "name": name, "image_bytes": None}
                        return {"success": False, "error": f"לא ניתן לגשת לדף (קוד {response.status})"}
                    html = await response.text()
                    final_url = str(response.url)

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            name = self._extract_name_from_soup(soup, final_url) or self._extract_name_from_url(url)
            if not name:
                return {"success": False, "error": "לא הצלחתי לחלץ את שם המוצר"}

            # Extract main product image URL
            image_url = self._extract_image_url(soup, final_url)
            image_bytes = None
            image_mime = "image/jpeg"

            if image_url:
                try:
                    async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as session:
                        async with session.get(image_url) as img_resp:
                            if img_resp.status == 200:
                                image_bytes = await img_resp.read()
                                ct = img_resp.headers.get("Content-Type", "image/jpeg")
                                image_mime = ct.split(";")[0].strip()
                                # Limit to 4MB
                                if len(image_bytes) > 4 * 1024 * 1024:
                                    image_bytes = None
                except Exception as e:
                    logger.warning(f"Could not download product image: {e}")

            return {"success": True, "name": name, "image_bytes": image_bytes, "image_mime": image_mime}

        except asyncio.TimeoutError:
            name = self._extract_name_from_url(url)
            if name:
                return {"success": True, "name": name, "image_bytes": None}
            return {"success": False, "error": "הדף לקח יותר מדי זמן לטעון"}
        except Exception as e:
            logger.error(f"_extract_from_url error: {e}")
            name = self._extract_name_from_url(url)
            if name:
                return {"success": True, "name": name, "image_bytes": None}
            return {"success": False, "error": "לא ניתן לגשת לדף. בדוק שהלינק תקין."}

    def _extract_image_url(self, soup, url: str) -> str:
        """Extract the main product image URL from the page."""
        domain = urlparse(url).netloc.lower()

        # Site-specific selectors
        site_selectors = {
            "amazon":      ["#landingImage", "#imgBlkFront", "#main-image"],
            "ebay":        [".ux-image-carousel-item img", "#icImg"],
            "aliexpress":  [".product-image img", ".images-view-item img", "[class*='product-image'] img"],
            "walmart":     ["[data-testid='hero-image'] img", ".prod-hero-image img"],
        }
        for site_key, selectors in site_selectors.items():
            if site_key in domain:
                for sel in selectors:
                    el = soup.select_one(sel)
                    if el:
                        src = el.get("src") or el.get("data-src") or el.get("data-old-hires") or el.get("data-a-dynamic-image")
                        if src and src.startswith("http"):
                            return src

        # OpenGraph image (works on most sites)
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]

        # JSON-LD image
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    img = data.get("image")
                    if isinstance(img, str) and img.startswith("http"):
                        return img
                    if isinstance(img, list) and img:
                        return img[0] if isinstance(img[0], str) else img[0].get("url", "")
            except Exception:
                pass

        # First large img on page
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src.startswith("http") and any(x in src for x in ["product", "item", "main", "primary", "large"]):
                return src

        return ""

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
            return self._clean_name(og["content"].strip())

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

        title = soup.find("title")
        if title:
            return self._clean_name(title.get_text(strip=True))
        return ""

    def _clean_name(self, name: str) -> str:
        patterns = [
            r"\s*[\|\-–]\s*(Amazon|eBay|AliExpress|Walmart|Target|Etsy).*$",
            r"\s*[\|\-–]\s*[A-Z][a-zA-Z\s]*\.com.*$",
        ]
        for p in patterns:
            name = re.sub(p, "", name, flags=re.IGNORECASE)
        return name.strip()[:200]

    # ------------------------------------------------------------------ #
    #  Claude search — sends both name + image for best results           #
    # ------------------------------------------------------------------ #
    async def _search_aliexpress_via_claude(self, product_name: str = None, image_bytes: bytes = None, image_mime: str = None) -> list:
        if not ANTHROPIC_API_KEY:
            return self._fallback_links(product_name or "מוצר")

        # Build the user message content
        content = []

        # Add image if available
        if image_bytes:
            mime = image_mime or "image/jpeg"
            if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                mime = "image/jpeg"
            b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64}
            })

        # Build text prompt
        if product_name and image_bytes:
            search_instruction = (
                f"Product name: {product_name}\n"
                "I'm also attaching the product image above.\n\n"
                "Use BOTH the product name AND the image to search AliExpress for this exact product or the closest match."
            )
        elif image_bytes:
            search_instruction = (
                "Look at the product in this image.\n"
                "Identify what this product is, then search AliExpress for it."
            )
        else:
            search_instruction = f"Search AliExpress for: {product_name}"

        prompt = (
            f"{search_instruction}\n\n"
            "Find the 5 cheapest listings on AliExpress right now.\n"
            "Return ONLY a JSON array, no other text:\n"
            "[\n"
            '  {"title": "short product title", "price": "$X.XX", "url": "https://www.aliexpress.com/item/..."}\n'
            "]\n\n"
            "Rules:\n"
            "- Only real AliExpress URLs (aliexpress.com/item/...)\n"
            "- Real prices with currency symbol\n"
            "- Sort cheapest first\n"
            "- Maximum 5 items\n"
            "- If no results found, return []"
        )
        content.append({"type": "text", "text": prompt})

        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": content}]
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
                        logger.error(f"Anthropic API error: {response.status} — {await response.text()}")
                        return self._fallback_links(product_name or "מוצר")
                    data = await response.json()

            full_text = "".join(
                block.get("text", "") for block in data.get("content", [])
                if block.get("type") == "text"
            )

            json_match = re.search(r'\[.*?\]', full_text, re.DOTALL)
            if not json_match:
                logger.warning("No JSON in Claude response")
                return self._fallback_links(product_name or "מוצר")

            items = json.loads(json_match.group())
            results = [
                {
                    "store": "AliExpress",
                    "price": item["price"],
                    "link": item["url"],
                    "title": item.get("title", "")[:80],
                }
                for item in items[:5]
                if item.get("price") and item.get("url")
            ]
            return results if results else self._fallback_links(product_name or "מוצר")

        except Exception as e:
            logger.error(f"Claude search error: {e}")
            return self._fallback_links(product_name or "מוצר")

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
