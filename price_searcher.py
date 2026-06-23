import asyncio
import aiohttp
import re
import json
import logging
import os
from urllib.parse import urlparse, quote_plus

logger = logging.getLogger(__name__)

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()

AMAZON_SITES = [
    {"domain": "amazon.com",    "country": "us", "flag": "🇺🇸", "name": "USA",      "currency": "USD"},
    {"domain": "amazon.co.uk",  "country": "uk", "flag": "🇬🇧", "name": "UK",       "currency": "GBP"},
    {"domain": "amazon.de",     "country": "de", "flag": "🇩🇪", "name": "Germany",  "currency": "EUR"},
    {"domain": "amazon.fr",     "country": "fr", "flag": "🇫🇷", "name": "France",   "currency": "EUR"},
    {"domain": "amazon.it",     "country": "it", "flag": "🇮🇹", "name": "Italy",    "currency": "EUR"},
    {"domain": "amazon.es",     "country": "es", "flag": "🇪🇸", "name": "Spain",    "currency": "EUR"},
    {"domain": "amazon.ca",     "country": "ca", "flag": "🇨🇦", "name": "Canada",   "currency": "CAD"},
    {"domain": "amazon.co.jp",  "country": "jp", "flag": "🇯🇵", "name": "Japan",    "currency": "JPY"},
]

TO_USD = {
    "USD": 1.0, "GBP": 1.27, "EUR": 1.08, "CAD": 0.74,
    "AUD": 0.65, "JPY": 0.0067, "INR": 0.012,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


class PriceSearcher:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=60)
        logger.warning("PRICE_SEARCHER_VERSION=v6-structured-api")
        if SCRAPER_API_KEY:
            logger.info(f"SCRAPER_API_KEY starts: {SCRAPER_API_KEY[:6]}...")
        else:
            logger.error("SCRAPER_API_KEY not set!")

    async def search_prices(self, product_name=None, image_bytes=None, image_mime=None):
        try:
            if product_name and product_name.startswith("http"):
                page_info = await self._extract_from_url(product_name)
                if not page_info["success"]:
                    return page_info
                product_name = page_info["name"]

            if not product_name:
                return {"success": False, "error": "לא סופק מוצר לחיפוש"}

            logger.info(f"Searching Amazon for: '{product_name}'")
            tasks = [self._search_one_amazon(product_name, site) for site in AMAZON_SITES]
            results_per_site = await asyncio.gather(*tasks, return_exceptions=True)

            all_prices = []
            for result in results_per_site:
                if isinstance(result, list):
                    all_prices.extend(result)

            all_prices = sorted(all_prices, key=lambda x: x.get("price_usd", 999999))
            logger.info(f"Total results: {len(all_prices)}")

            if not all_prices:
                return {"success": True, "product_name": product_name, "prices": self._fallback(product_name)}

            # Keep up to 5 from USA + up to 5 cheapest from other countries
            usa_prices = [p for p in all_prices if "USA" in p.get("store", "")][:5]
            other_prices = [p for p in all_prices if "USA" not in p.get("store", "")][:5]
            final = usa_prices + other_prices
            final = sorted(final, key=lambda x: x.get("price_usd", 999999))
            return {"success": True, "product_name": product_name, "prices": final}

        except Exception as e:
            logger.error(f"search_prices error: {e}")
            return {"success": False, "error": str(e)}

    async def _search_one_amazon(self, product_name, site):
        """Use ScraperAPI structured Amazon search — returns clean JSON."""
        if not SCRAPER_API_KEY:
            return []
        query = quote_plus(product_name)
        # ScraperAPI structured endpoint for Amazon search
        url = (
            f"https://api.scraperapi.com/structured/amazon/search"
            f"?api_key={SCRAPER_API_KEY}"
            f"&query={query}"
            f"&country={site['country']}"
        )
        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(url) as response:
                    logger.info(f"{site['flag']} {site['name']}: status {response.status}")
                    if response.status != 200:
                        body = await response.text()
                        logger.warning(f"{site['name']} error body: {body[:200]}")
                        return []
                    data = await response.json()

            results = []
            products = data.get("results", [])
            logger.info(f"{site['flag']} {site['name']}: {len(products)} products in JSON")

            max_results = 5 if site["domain"] == "amazon.com" else 2
            for item in products[:max_results]:
                price_str = item.get("price", "")
                if not price_str:
                    continue
                # Extract numeric value
                nums = re.findall(r'[\d]+\.?\d*', str(price_str).replace(",", ""))
                if not nums:
                    continue
                try:
                    price_usd = float(nums[0]) * TO_USD.get(site["currency"], 1.0)
                except Exception:
                    continue

                asin = item.get("asin", "")
                link = f"https://www.{site['domain']}/dp/{asin}" if asin else item.get("url", "")
                title = item.get("name", item.get("title", ""))[:80]

                results.append({
                    "store": f"Amazon {site['flag']} {site['name']}",
                    "price": f"{site['currency']} {price_str}",
                    "price_usd": price_usd,
                    "link": link,
                    "title": title,
                })

            return results

        except Exception as e:
            logger.warning(f"{site['name']} error: {e}")
            return []

    async def _extract_from_url(self, url):
        try:
            domain = urlparse(url).netloc.lower()
            site = next((s for s in AMAZON_SITES if s["domain"] in domain), {"country": "us"})
            # Use ScraperAPI structured product endpoint
            api_url = (
                f"https://api.scraperapi.com/structured/amazon/product"
                f"?api_key={SCRAPER_API_KEY}"
                f"&asin={self._extract_asin(url)}"
                f"&country={site['country']}"
            )
            asin = self._extract_asin(url)
            if asin and SCRAPER_API_KEY:
                async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                    async with session.get(api_url) as response:
                        if response.status == 200:
                            data = await response.json()
                            name = data.get("name", data.get("title", ""))
                            if name:
                                return {"success": True, "name": name}

            # Fallback: scrape page directly
            proxied = (f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}"
                      f"&render=true&country_code={site.get('country','us')}&url={quote_plus(url)}")
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(proxied, allow_redirects=True) as response:
                    if response.status != 200:
                        return {"success": False, "error": f"שגיאה {response.status}"}
                    html = await response.text()

            name = self._extract_name(html)
            if not name:
                return {"success": False, "error": "לא נמצא שם מוצר — שלח שם ישירות"}
            return {"success": True, "name": name}

        except Exception as e:
            logger.error(f"_extract_from_url: {e}")
            return {"success": False, "error": "לא ניתן לגשת לדף."}

    def _extract_asin(self, url):
        m = re.search(r'/dp/([A-Z0-9]{10})', url)
        return m.group(1) if m else ""

    def _extract_name(self, html):
        for pat in [
            r'id="productTitle"[^>]*>\s*([^<]{10,300})',
            r'"title"\s*:\s*"([^"]{10,300})"',
        ]:
            m = re.search(pat, html)
            if m:
                return m.group(1).strip()
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']{10,200})["\']', html, re.I)
        if m and "amazon" not in m.group(1).lower():
            return self._clean(m.group(1))
        m = re.search(r'<title[^>]*>([^<]{10,200})</title>', html, re.I)
        if m:
            return self._clean(m.group(1))
        return ""

    def _clean(self, name):
        for p in [r"\s*[\|\-–]\s*Amazon.*$", r"\s*[\|\-–]\s*[A-Z][a-zA-Z\s]*\.com.*$"]:
            name = re.sub(p, "", name, flags=re.IGNORECASE)
        return name.strip()[:200]

    def _fallback(self, product_name):
        query = quote_plus(product_name)
        return [{"store": "Amazon 🌍", "price": "לחץ לחיפוש",
                 "link": f"https://www.amazon.com/s?k={query}",
                 "title": product_name[:60], "price_usd": 999999}]
