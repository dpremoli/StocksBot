import re

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PwTimeout

# FT redirects /etfs/tearsheet/ -> /funds/tearsheet/ automatically for ISINs
FT_URL = "https://markets.ft.com/data/etfs/tearsheet/summary?s={isin}:{currency}"

_SELECTORS = [
    ".mod-tearsheet-overview__quote .mod-ui-data-list__value",
    "span.mod-ui-data-list__value",
    ".mod-ui-data-list__value",
]

_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class PriceNotFoundError(Exception):
    pass


class FTBlockedError(Exception):
    pass


def _parse_price(raw: str) -> float | None:
    """Return positive float from raw text, or None if unparseable/zero."""
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        value = float(cleaned)
        return value if value > 0 else None
    except ValueError:
        return None


async def scrape_price(isin: str, currency: str) -> float:
    url = FT_URL.format(isin=isin.upper(), currency=currency.upper())

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            if response and response.status == 403:
                raise FTBlockedError(f"FT returned 403 for {isin}")

            try:
                await page.wait_for_selector(
                    ".mod-tearsheet-overview__quote", timeout=15000
                )
            except PwTimeout:
                pass  # attempt extraction anyway

            body = await page.inner_text("body")
            if any(
                phrase in body.lower()
                for phrase in ("no data available", "instrument not found", "page not found")
            ):
                raise PriceNotFoundError(f"No data on FT for ISIN {isin}")

            for selector in _SELECTORS:
                elements = await page.query_selector_all(selector)
                # Always take the first valid positive price from the first selector hit
                for el in elements:
                    raw = (await el.inner_text()).strip()
                    price = _parse_price(raw)
                    if price is not None:
                        return price

            raise PriceNotFoundError(f"Could not extract price for {isin} from {url}")

        finally:
            await browser.close()
