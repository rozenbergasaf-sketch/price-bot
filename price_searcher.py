import asyncio
import aiohttp
import re
import json
import logging
import os
from urllib.parse import urlparse, quote_plus

logger = logging.getLogger(__name__)

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()

# כל אתרי Amazon בעולם עם המטבע והדגל שלהם
AMAZON_SITES = [
    {"domain": "amazon.com",    "country": "us",  "flag": "🇺🇸", "name": "USA",        "currency": "USD"},
    {"domain": "amazon.co.uk",  "country": "uk",  "flag": "🇬🇧", "name": "UK",         "currency": "GBP"},
    {"domain": "amazon.de",     "country": "de",  "flag": "🇩🇪", "name": "Germany",    "currency": "EUR"},
    {"domain": "amazon.fr",     "country": "fr",  "flag": "🇫🇷", "name": "France",     "currency": "EUR"},
    {"domain": "amazon.it",     "country": "it",  "flag": "🇮🇹", "name": "Italy",      "currency": "EUR"},
    {"domain": "amazon.es",     "country": "es",  "flag": "🇪🇸", "name": "Spain",      "currency": "EUR"},
    {"domain": "amazon.ca",     "country": "ca",  "flag": "🇨🇦", "name": "Canada",     "currency": "CAD"},
    {"domain": "amazon.com.au", "country": "au",  "flag": "🇦🇺", "name": "Australia",  "currency": "AUD"},
    {"domain": "amazon.co.jp",  "country": "jp",  "flag": "🇯🇵", "name": "Japan",      "currency": "JPY"},
    {"domain": "amazon.in",     "country": "in",  "flag": "🇮🇳", "name": "India",      "currency": "INR"},
    {"domain": "amazon.com.mx", "country": "mx",  "flag": "🇲🇽", "name": "Mexico",     "currency": "MXN"},
    {"domain": "amazon.nl",     "country": "nl",  "flag": "🇳🇱", "name": "Netherlands","currency": "EUR"},
    {"domain": "amazon.se",     "country": "se",  "flag": "🇸🇪", "name": "Sweden",     "currency": "SEK"},
    {"domain": "amazon.pl",     "country": "pl",  "flag": "🇵🇱", "name": "Poland",     "currency": "PLN"},
    {"domain": "amazon.com.br", "country": "br",  "flag": "🇧🇷", "name": "Brazil",     "currency": "BRL"},
    {"domain": "amazon.ae",     "country": "ae",  "flag": "🇦🇪", "name": "UAE",        "currency": "AED"},
    {"domain": "amazon.sa",     "country": "sa",  "flag": "🇸🇦", "name": "Saudi",      "currency": "SAR"},
    {"domain": "amazon.sg",     "country": "sg",  "flag": "🇸🇬", "name": "Singapore",  "currency": "SGD"},
]

# שערי המרה משוערים לדולר (לצורך מיון בלבד)
TO_USD = {
    "USD": 1.0, "GBP": 1.27, "EUR": 1.08, "CAD": 0.74, "AUD": 0.65,
    "JPY": 0.0067, "INR": 0.012, "MXN": 0.059, "SEK": 0.096, "PLN": 0.25,
    "BRL": 0.20, "AED": 0.27, "SAR": 0.27, "SGD": 0.74,
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

            logger.info(f"Searching all Amazon sites for: '{product_name}'")

            # חפש ב-5 האתרים הגדולים במקביל (לחסוך בבקשות ScraperAPI)
            sites_to_search = AMAZON_SITES[:8]
            tasks = [self._search_one_amazon(product_name, site) for site in sites_to_search]
            results_per_site = await asyncio.gather(*tasks, return_exceptions=True)

            all_prices = []
            for site, result in zip(sites_to_search, results_per_site):
                if isinstance(result, list):
                    all_prices.extend(result)

            # מיין לפי מחיר בדולר
            all_prices = self._sort_by_usd(all_prices)
            logger.info(f"Total results from all sites: {len(all_prices)}")

            if not all_prices:
                return {"success": True, "product_name": product_name, "prices": self._fallback_links(product_name)}

            return {"success": True, "product_name": product_name, "prices": all_prices[:5]}

        except Exception as e:
            logger.error(f"search_prices error: {e}")
            return {"success": False, "error": f"שגיאה: {str(e)}"}

    async def _search_one_amazon(self, product_name: str, site: dict) -> list:
        """חיפוש באתר Amazon אחד."""
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

            results = self._parse_amazon_html(html, site)
            logger.info(f"{site['flag']} {site['name']}: {len(results)} results")
            return results

        except Exception as e:
            logger.warning(f"{site['name']} error: {e}")
            return []

    def _parse_amazon_html(self, html: str, site: dict) -> list:
        results = []
        seen = set()

        # Strategy 1: data-asin + a-price-whole + a-price-fraction
        blocks = re.findall(
            r'data-asin="([A-Z0-9]{10})"[\s\S]{0,3000}?'
            r'<span class="a-price-whole">([\d\.,]+)</span>'
            r'[\s\S]{0,200}?<span class="a-price-fraction">(\d+)</span>',
            html
        )
        for asin, whole, fraction in blocks:
            if not asin or asin in seen:
                continue
            seen.add(asin)
            whole_clean = re.sub(r'[^\d]', '', whole)
            price_str = f"{site['currency']} {whole_clean}.{fraction}"
            price_usd = self._to_usd(float(f"{whole_clean}.{fraction}"), site["currency"])
            link = f"https://www.{site['domain']}/dp/{asin}"

            # נסה למצוא כותרת
            title = ""
            pos = html.find(f'data-asin="{asin}"')
            if pos >= 0:
                block = html[pos:pos + 2000]
                t = re.search(r'<span[^>]*class="[^"]*a-text-normal[^"]*"[^>]*>([^<]{5,150})</span>', block)
                if t:
                    title = t.group(1).strip()

            results.append({
                "store": f"Amazon {site['flag']} {site['name']}",
                "price": price_str,
                "price_usd": price_usd,
                "link": link,
                "title": title[:80],
            })
            if len(results) >= 3:
                break

        if results:
            return results

        # Strategy 2: כל ASIN + מחיר בטקסט
        asins = list(dict.fromkeys(re.findall(r'data-asin="([A-Z0-9]{10})"', html)))[:3]
        prices = re.findall(r'(\d[\d\.,]*)\s*(?:' + re.escape(site['currency']) + r'|[$£€¥₹])', html)

        for i, asin in enumerate(asins):
            if asin in seen:
                continue
            seen.add(asin)
            if i < len(prices):
                val_str = re.sub(r'[^\d.]', '', prices[i].replace(',', '.'))
                try:
                    val = float(val_str)
                    price_str = f"{site['currency']} {val:.2f}"
                    price_usd = self._to_usd(val, site["currency"])
                except Exception:
                    price_str = prices[i]
                    price_usd = 999999
            else:
                price_str = "מחיר לא זמין"
                price_usd = 999999

            results.append({
                "store": f"Amazon {site['flag']} {site['name']}",
                "price": price_str,
                "price_usd": price_usd,
                "link": f"https://www.{site['domain']}/dp/{asin}",
                "title": "",
            })

        return results

    def _to_usd(self, amount: float, currency: str) -> float:
        return amount * TO_USD.get(currency, 1.0)

    async def _extract_from_url(self, url: str) -> dict:
        try:
            domain = urlparse(url).netloc.lower()
            site = next((s for s in AMAZON_SITES if s["domain"] in domain), {"country": "us"})
            fetch_url = _proxied(url, site["country"])

            async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
                async with session.get(fetch_url, allow_redirects=True) as response:
                    if response.status != 200:
                        return {"success": False, "error": f"לא ניתן לגשת לדף (קוד {response.status})"}
                    html = await response.text()

            name = self._extract_name_from_html(html, url)
            if not name:
                return {"success": False, "error": "לא הצלחתי לחלץ את שם המוצר — נסה לשלוח שם ישירות"}
            logger.info(f"Extracted name: '{name[:80]}'")
            return {"success": True, "name": name}

        except asyncio.TimeoutError:
            return {"success": False, "error": "הדף לקח יותר מדי זמן"}
        except Exception as e:
            logger.error(f"_extract_from_url: {e}")
            return {"success": False, "error": "לא ניתן לגשת לדף."}

    def _extract_name_from_html(self, html: str, url: str) -> str:
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
                if isinstance(data, dict) and data.get("@type") == "Product" and data.get("name"):
                    return data["name"][:200]
            except Exception:
                pass

        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']{10,300})["\']', html, re.I)
        if m:
            t = m.group(1).strip()
            if "amazon" not in t.lower():
                return self._clean(t)

        m = re.search(r'<title[^>]*>([^<]{10,300})</title>', html, re.I)
        if m:
            return self._clean(m.group(1).strip())
        return ""

    def _clean(self, name: str) -> str:
        for p in [r"\s*[\|\-–]\s*(Amazon|eBay|AliExpress).*$", r"\s*[\|\-–]\s*[A-Z][a-zA-Z\s]*\.com.*$"]:
            name = re.sub(p, "", name, flags=re.IGNORECASE)
        return name.strip()[:200]

    def _fallback_links(self, product_name: str) -> list:
        query = quote_plus(product_name)
        return [{"store": "Amazon 🌍", "price": "לחץ לחיפוש",
                 "link": f"https://www.amazon.com/s?k={query}&s=price-asc-rank",
                 "title": f"חפש: {product_name[:60]}"}]

    def _sort_by_usd(self, prices: list) -> list:
        return sorted(prices, key=lambda x: x.get("price_usd", 999999))
