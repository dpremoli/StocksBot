import re

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PwTimeout

FT_URL = "https://markets.ft.com/data/etfs/tearsheet/summary?s={isin}:{currency}"
FT_URL_NO_CCY = "https://markets.ft.com/data/etfs/tearsheet/summary?s={isin}"

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


def _is_search_page(url: str) -> bool:
    """Returns True if FT redirected to a search/no-result page."""
    return "/data/search" in url or "query=" in url


async def _extract_price(page) -> float | None:
    """Try all selectors and return first valid price, or None."""
    try:
        await page.wait_for_selector(".mod-tearsheet-overview__quote", timeout=15000)
    except PwTimeout:
        pass

    body = await page.inner_text("body")
    if any(
        phrase in body.lower()
        for phrase in ("no data available", "instrument not found", "page not found")
    ):
        return None

    for selector in _SELECTORS:
        elements = await page.query_selector_all(selector)
        for el in elements:
            raw = (await el.inner_text()).strip()
            price = _parse_price(raw)
            if price is not None:
                return price

    return None


async def scrape_price(isin: str, currency: str) -> float:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            # First attempt: with currency
            url = FT_URL.format(isin=isin.upper(), currency=currency.upper())
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            if response and response.status == 403:
                raise FTBlockedError(f"FT returned 403 for {isin}")

            # If FT redirected to a search page, retry without currency suffix
            if _is_search_page(page.url):
                url_no_ccy = FT_URL_NO_CCY.format(isin=isin.upper())
                response = await page.goto(url_no_ccy, wait_until="domcontentloaded", timeout=30000)

            if _is_search_page(page.url):
                raise PriceNotFoundError(f"FT has no page for ISIN {isin}")

            price = await _extract_price(page)
            if price is not None:
                return price

            raise PriceNotFoundError(f"Could not extract price for {isin} from {page.url}")

        finally:
            await browser.close()
