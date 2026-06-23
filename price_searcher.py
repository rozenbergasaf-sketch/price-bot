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
    {"domain": "amazon.com",    "country": "us", "flag": "🇺🇸", "name": "USA",       "currency": "USD"},
    {"domain": "amazon.co.uk",  "country": "uk", "flag": "🇬🇧", "name": "UK",        "currency": "GBP"},
    {"domain": "amazon.de",     "country": "de", "flag": "🇩🇪", "name": "Germany",   "currency": "EUR"},
    {"domain": "amazon.fr",     "country": "fr", "flag": "🇫🇷", "name": "France",    "currency": "EUR"},
    {"domain": "amazon.it",     "country": "it", "flag": "🇮🇹", "name": "Italy",     "currency": "EUR"},
    {"domain": "amazon.es",     "country": "es", "flag": "🇪🇸", "name": "Spain",     "currency": "EUR"},
    {"domain": "amazon.ca",     "country": "ca", "flag": "🇨🇦", "name": "Canada",    "currency": "CAD"},
    {"domain": "amazon.co.jp",  "country": "jp", "flag": "🇯🇵", "name": "Japan",     "currency": "JPY"},
]

TO_USD = {
    "USD": 1.0, "GBP": 1.27, "EUR": 1.08, "CAD": 0.74,
    "AUD": 0.65, "JPY": 0.0067, "INR": 0.012,
}


def _proxied(url: str, country: str = "us") -> str:
    if not SCRAPER_API_KEY:
        return url
    return (f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}"
            f"&render=true&country_code={country}&url={quote_plus(url)}")


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class PriceSearcher:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=60)
        logger.warning("PRICE_SEARCHER_VERSION=v5-debug")
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
            for site, result in zip(AMAZON_SITES, results_per_site):
                if isinstance(result, list):
                    all_prices.extend(result)

            all_prices = self._sort_by_usd(all_prices)
            logger.info(f"Total results: {len(all_prices)}")

            if not all_prices:
                return {"success": True, "product_name": product_name, "prices": self._fallback(product_name)}

            return {"success": True, "product_name": product_name, "prices": all_prices[:5]}

        except Exception as e:
            logger.error(f"search_prices error: {e}")
            return {"success": False, "error": str(e)}

    async def _search_one_amazon(self, product_name, site):
        if not SCRAPER_API_KEY:
            return []
        query = quote_plus(product_name)
        url = f"https://www.{site['domain']}/s?k={query}&s=price-asc-rank"
        fetch_url = _proxied(url, site["country"])
        try:
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url) as response:
                    if response.status != 200:
                        logger.warning(f"{site['name']}: status {response.status}")
                        return []
                    html = await response.text()

            # DEBUG: always log for USA
            if site["domain"] == "amazon.com":
                keys = ["data-asin", "a-price-whole", "a-price-fraction",
                        "a-offscreen", "s-item-image", "puis-price"]
                found = {k: html.count(k) for k in keys}
                logger.warning(f"USA_KEYWORDS={found} html_len={len(html)}")
                clean = re.sub(r'\s+', ' ', html[2000:4000])
                logger.warning(f"USA_HTML_CHUNK={clean[:800]}")

            results = self._parse_amazon_html(html, site)
            logger.info(f"{site['flag']} {site['name']}: {len(results)} results")
            return results

        except Exception as e:
            logger.warning(f"{site['name']} error: {e}")
            return []

    def _parse_amazon_html(self, html, site):
        results = []
        seen = set()

        # Strategy 1: data-asin + a-price-whole
        blocks = re.findall(
            r'data-asin="([A-Z0-9]{10})"[\s\S]{0,3000}?'
            r'<span class="a-price-whole">([\d\.,]+)</span>'
            r'[\s\S]{0,300}?<span class="a-price-fraction">(\d+)</span>',
            html
        )
        for asin, whole, frac in blocks:
            if not asin or asin in seen:
                continue
            seen.add(asin)
            whole_clean = re.sub(r'[^\d]', '', whole)
            if not whole_clean:
                continue
            price_str = f"{site['currency']} {whole_clean}.{frac}"
            price_usd = int(whole_clean) * TO_USD.get(site["currency"], 1.0)
            title = self._find_title(html, asin)
            results.append({
                "store": f"Amazon {site['flag']} {site['name']}",
                "price": price_str,
                "price_usd": price_usd,
                "link": f"https://www.{site['domain']}/dp/{asin}",
                "title": title,
            })
            if len(results) >= 3:
                break

        if results:
            return results

        # Strategy 2: a-offscreen price (Amazon sometimes uses this)
        offscreen = re.findall(r'class="a-offscreen">([^<]{2,20})</span>', html)
        asins = list(dict.fromkeys(re.findall(r'data-asin="([A-Z0-9]{10})"', html)))

        if asins and offscreen:
            logger.info(f"{site['name']} Strategy2: {len(asins)} asins, {len(offscreen)} prices")
            for i, asin in enumerate(asins[:3]):
                if asin in seen:
                    continue
                seen.add(asin)
                price_str = offscreen[i] if i < len(offscreen) else "N/A"
                # Extract numeric value
                nums = re.findall(r'[\d,]+\.?\d*', price_str)
                price_usd = float(nums[0].replace(',', '')) * TO_USD.get(site["currency"], 1.0) if nums else 999999
                results.append({
                    "store": f"Amazon {site['flag']} {site['name']}",
                    "price": f"{site['currency']} {price_str}",
                    "price_usd": price_usd,
                    "link": f"https://www.{site['domain']}/dp/{asin}",
                    "title": self._find_title(html, asin),
                })
            return results

        # Strategy 3: pure regex price + asin
        prices_raw = re.findall(r'(?:\$|£|€|¥)\s*([\d,]+\.?\d*)', html)
        if asins and prices_raw:
            logger.info(f"{site['name']} Strategy3: {len(asins)} asins, {len(prices_raw)} prices")
            for i, asin in enumerate(asins[:3]):
                if asin in seen:
                    continue
                seen.add(asin)
                p = prices_raw[i] if i < len(prices_raw) else "?"
                nums = re.findall(r'[\d]+\.?\d*', p.replace(',', ''))
                price_usd = float(nums[0]) * TO_USD.get(site["currency"], 1.0) if nums else 999999
                results.append({
                    "store": f"Amazon {site['flag']} {site['name']}",
                    "price": f"{site['currency']} {p}",
                    "price_usd": price_usd,
                    "link": f"https://www.{site['domain']}/dp/{asin}",
                    "title": "",
                })
            return results

        return []

    def _find_title(self, html, asin):
        pos = html.find(f'data-asin="{asin}"')
        if pos < 0:
            return ""
        block = html[pos:pos + 2000]
        for pat in [
            r'<span[^>]*class="[^"]*a-text-normal[^"]*"[^>]*>([^<]{5,150})</span>',
            r'<h2[^>]*>[\s\S]*?<span[^>]*>([^<]{5,150})</span>',
        ]:
            m = re.search(pat, block)
            if m:
                return m.group(1).strip()[:80]
        return ""

    async def _extract_from_url(self, url):
        try:
            domain = urlparse(url).netloc.lower()
            site = next((s for s in AMAZON_SITES if s["domain"] in domain), {"country": "us"})
            fetch_url = _proxied(url, site["country"])
            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url, allow_redirects=True) as response:
                    if response.status != 200:
                        return {"success": False, "error": f"שגיאה {response.status}"}
                    html = await response.text()
            name = self._extract_name(html, url)
            if not name:
                return {"success": False, "error": "לא נמצא שם מוצר — שלח שם ישירות"}
            return {"success": True, "name": name}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _extract_name(self, html, url):
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
                 "link": f"https://www.amazon.com/s?k={query}&s=price-asc-rank",
                 "title": product_name[:60]}]

    def _sort_by_usd(self, prices):
        return sorted(prices, key=lambda x: x.get("price_usd", 999999))
