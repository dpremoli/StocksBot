"""
Validation script — run before building the full service.
Usage: python test_scrape.py [ISIN] [CURRENCY]
  python test_scrape.py LU1022659046 EUR
"""
import asyncio
import re
import sys

ISIN = sys.argv[1] if len(sys.argv) > 1 else "LU1022659046"
CURRENCY = sys.argv[2] if len(sys.argv) > 2 else "EUR"


async def test():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        br = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await br.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        pg = await ctx.new_page()

        # FT auto-redirects /etfs/tearsheet/ → /funds/tearsheet/ for ISINs
        url = f"https://markets.ft.com/data/etfs/tearsheet/summary?s={ISIN}:{CURRENCY}"
        print(f"Fetching: {url}")
        resp = await pg.goto(url, wait_until="domcontentloaded")
        print(f"Final URL: {pg.url}")
        print(f"Status: {resp.status if resp else 'unknown'}")

        # Wait for JS to render price elements
        try:
            await pg.wait_for_selector(".mod-tearsheet-overview__quote", timeout=15000)
            print("Selector .mod-tearsheet-overview__quote found")
        except Exception as e:
            print(f"Timeout waiting for quote selector: {e}")

        # Primary selector (FT documented)
        els = await pg.query_selector_all(
            ".mod-tearsheet-overview__quote .mod-ui-data-list__value"
        )
        print(f"\nPrimary selector found {len(els)} element(s):")
        for el in els:
            txt = (await el.inner_text()).strip()
            cleaned = re.sub(r"[^\d.]", "", txt.replace(",", ""))
            try:
                marker = " PRICE" if cleaned and float(cleaned) > 0 else ""
            except ValueError:
                marker = " (unparseable)"
            print(f"  {txt!r} -> {cleaned!r}{marker}")

        # Fallback: all mod-ui-data-list__value spans
        all_els = await pg.query_selector_all("span.mod-ui-data-list__value")
        print(f"\nAll span.mod-ui-data-list__value elements ({len(all_els)}):")
        for el in all_els:
            print(f"  {(await el.inner_text()).strip()!r}")

        await br.close()


asyncio.run(test())
