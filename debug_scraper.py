"""
הרץ את הסקריפט הזה ב-Railway כדי לראות את ה-HTML האמיתי.
הוסף לקובץ bot.py שורה: import debug_scraper  (זמני)
או הרץ: python debug_scraper.py
"""
import asyncio, aiohttp, os, re, json
from urllib.parse import quote_plus

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()

async def debug():
    if not SCRAPER_API_KEY:
        print("❌ NO SCRAPER_API_KEY")
        return

    q = quote_plus("bike light")
    ali_url = f"https://www.aliexpress.com/wholesale?SearchText={q}&SortType=total_tranpro_desc"
    fetch_url = f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}&url={quote_plus(ali_url)}"

    print(f"Fetching: {ali_url}")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
        async with s.get(fetch_url) as r:
            print(f"Status: {r.status}")
            html = await r.text()

    print(f"HTML length: {len(html)}")

    # Check what keywords exist
    keywords = ["itemId", "productId", "salePrice", "formattedPrice", "runParams",
                "_dida_config_", "window.runParams", "US $", "subject", "itemList"]
    for kw in keywords:
        count = html.count(kw)
        if count:
            print(f"  '{kw}': {count} times")

    # Show sample around first itemId
    m = re.search(r'.{0,200}itemId.{0,200}', html)
    if m:
        print(f"\n--- itemId context ---\n{m.group()}\n")

    # Show sample around first price
    m2 = re.search(r'.{0,100}US \$.{0,100}', html)
    if m2:
        print(f"--- price context ---\n{m2.group()}\n")

    # Try to find any JSON blocks
    scripts = re.findall(r'window\.[a-zA-Z_]+\s*=\s*(\{.{20,200})', html)
    for s in scripts[:3]:
        print(f"window.X = {s[:120]}")

    # Save full HTML for manual inspection
    with open("/tmp/ali_search.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\n✅ Full HTML saved to /tmp/ali_search.html")
    print("First 2000 chars:")
    print(html[:2000])

asyncio.run(debug())
