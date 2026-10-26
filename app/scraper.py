import re

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PwTimeout

# symbol is an FT listing such as LU0113257694:EUR or VUSD:LSE:USD (see app.listings)
FT_URL = "https://markets.ft.com/data/etfs/tearsheet/summary?s={symbol}"

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


async def scrape_price(symbol: str) -> float:
    """Scrape the latest price from the FT tearsheet of one specific listing."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            url = FT_URL.format(symbol=symbol)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            if response and response.status == 403:
                raise FTBlockedError(f"FT returned 403 for {symbol}")

            # No fallback to the bare ISIN: FT's default listing can be in another currency
            if _is_search_page(page.url):
                raise PriceNotFoundError(f"FT has no page for {symbol}")

            price = await _extract_price(page)
            if price is not None:
                return price

            raise PriceNotFoundError(f"Could not extract price for {symbol} from {page.url}")

        finally:
            await browser.close()
