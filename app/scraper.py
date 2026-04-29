import asyncio
import re

from playwright.async_api import async_playwright, Browser
from playwright.async_api import TimeoutError as PwTimeout

FT_URL = "https://markets.ft.com/data/etfs/tearsheet/summary?s={isin}:{currency}"
FT_URL_NO_CCY = "https://markets.ft.com/data/etfs/tearsheet/summary?s={isin}"

# Holdings tab — tried in order: ETF+ccy, Fund+ccy, ETF no-ccy, Fund no-ccy
_HOLDINGS_URLS = [
    "https://markets.ft.com/data/etfs/tearsheet/holdings?s={isin}:{currency}",
    "https://markets.ft.com/data/funds/tearsheet/holdings?s={isin}:{currency}",
    "https://markets.ft.com/data/etfs/tearsheet/holdings?s={isin}",
    "https://markets.ft.com/data/funds/tearsheet/holdings?s={isin}",
]

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

# Shared browser instance and concurrency limiter
_browser: Browser | None = None
_playwright_ctx = None
_semaphore = asyncio.Semaphore(3)  # max 3 concurrent pages


class PriceNotFoundError(Exception):
    pass


class FTBlockedError(Exception):
    pass


async def get_browser() -> Browser:
    global _browser, _playwright_ctx
    if _browser is None or not _browser.is_connected():
        if _playwright_ctx is not None:
            try:
                await _playwright_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        _playwright_ctx = async_playwright()
        pw = await _playwright_ctx.__aenter__()
        _browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
    return _browser


async def close_browser():
    global _browser, _playwright_ctx
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright_ctx is not None:
        try:
            await _playwright_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        _playwright_ctx = None


def _parse_price(raw: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        value = float(cleaned)
        return value if value > 0 else None
    except ValueError:
        return None


def _is_search_page(url: str) -> bool:
    return "/data/search" in url or "query=" in url


async def _extract_price(page) -> float | None:
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

    # Detect pence-quoted instruments (GBp/GBX) — divide price by 100 to get GBP
    is_pence = False
    currency_els = await page.query_selector_all(".mod-tearsheet-overview__quote .mod-ui-data-list__label")
    for el in currency_els:
        label = (await el.inner_text()).strip()
        if label in ("GBp", "GBX", "pence"):
            is_pence = True
            break
    if not is_pence:
        # Fallback: check the quote block text for GBp
        quote_el = await page.query_selector(".mod-tearsheet-overview__quote")
        if quote_el:
            quote_text = await quote_el.inner_text()
            if "GBp" in quote_text or "GBX" in quote_text:
                is_pence = True

    for selector in _SELECTORS:
        elements = await page.query_selector_all(selector)
        for el in elements:
            raw = (await el.inner_text()).strip()
            price = _parse_price(raw)
            if price is not None:
                return round(price / 100, 6) if is_pence else price

    return None


def _parse_pct(raw: str) -> float | None:
    cleaned = raw.strip().replace("%", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


async def _extract_allocation_table(page) -> list[dict]:
    """Extract rows from the currently-visible allocation/weightings table."""
    rows = await page.evaluate("""() => {
        const results = [];
        // Asset allocation table: rows under section with 'Asset allocation' heading
        const tables = document.querySelectorAll('table');
        for (const table of tables) {
            const rows_els = table.querySelectorAll('tbody tr');
            if (rows_els.length === 0) continue;
            for (const tr of rows_els) {
                const cells = tr.querySelectorAll('td');
                if (cells.length >= 2) {
                    const name = cells[0].innerText.trim();
                    const pct = cells[1].innerText.trim();
                    if (name && pct) results.push({name, pct});
                }
            }
            if (results.length > 0) break;
        }
        return results;
    }""")
    out = []
    for row in rows:
        pct = _parse_pct(row["pct"])
        if pct is not None and pct > 0:
            out.append({"name": row["name"], "weight": pct})
    return out


async def _extract_weightings_table(page) -> list[dict]:
    """Extract rows from the currently-visible weightings table (sectors or regions).

    FT uses the same 'Sector' column header for both views; visibility (offsetParent)
    distinguishes which tab is active.
    """
    rows = await page.evaluate("""() => {
        const results = [];
        const tables = document.querySelectorAll('table');
        for (const table of tables) {
            // Skip hidden tables
            if (table.offsetParent === null) continue;
            const header = table.querySelector('thead th, thead td');
            if (!header) continue;
            const headerText = header.innerText.trim().toLowerCase();
            if (headerText !== 'sector' && headerText !== 'region') continue;
            const rows_els = table.querySelectorAll('tbody tr');
            for (const tr of rows_els) {
                const cells = tr.querySelectorAll('td');
                if (cells.length >= 2) {
                    const name = cells[0].innerText.trim();
                    const pct = cells[1].innerText.trim();
                    if (name) results.push({name, pct});
                }
            }
            if (results.length > 0) break;
        }
        return results;
    }""")
    out = []
    for row in rows:
        pct = _parse_pct(row["pct"])
        if pct is not None and pct > 0 and row["name"].lower() != "other":
            out.append({"name": row["name"], "weight": pct})
    return out


async def _extract_fund_name(page) -> str | None:
    try:
        el = await page.query_selector("h1.mod-tearsheet-overview__name, h1.mod-ui-tearsheet__title, h1")
        if el:
            return (await el.inner_text()).strip() or None
    except Exception:
        pass
    return None


_SUMMARY_URLS = [
    "https://markets.ft.com/data/etfs/tearsheet/summary?s={isin}:{currency}",
    "https://markets.ft.com/data/funds/tearsheet/summary?s={isin}:{currency}",
    "https://markets.ft.com/data/etfs/tearsheet/summary?s={isin}",
    "https://markets.ft.com/data/funds/tearsheet/summary?s={isin}",
]


async def _scrape_summary_regions(page, isin: str, currency: str) -> list[dict]:
    """Scrape the top-5 regions from the summary page Diversification section."""
    for url_tpl in _SUMMARY_URLS:
        url = url_tpl.format(isin=isin.upper(), currency=currency.upper())
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if response and response.status == 403:
                return []
            if _is_search_page(page.url):
                continue
        except Exception:
            continue

        try:
            await page.wait_for_selector(".mod-tearsheet-overview__diversification", timeout=8000)
        except PwTimeout:
            pass

        rows = await page.evaluate("""() => {
            const results = [];
            // Find the TOP 5 REGIONS heading then collect its sibling rows
            const headings = [...document.querySelectorAll('h3, h4, strong, b, .mod-tearsheet-overview__diversification-title')];
            let regionSection = null;
            for (const h of headings) {
                if (h.innerText && h.innerText.trim().toUpperCase().includes('REGION')) {
                    regionSection = h.closest('section, div, td') || h.parentElement;
                    break;
                }
            }
            if (!regionSection) return results;

            const rows = regionSection.querySelectorAll('li, tr, .mod-tearsheet-overview__diversification-item');
            for (const row of rows) {
                const text = row.innerText.trim();
                const match = text.match(/^(.+?)\\s+(\\d+\\.?\\d*)%/);
                if (match) results.push({name: match[1].trim(), pct: match[2]});
            }
            return results;
        }""")

        out = []
        for row in rows:
            pct = _parse_pct(row["pct"])
            if pct is not None and pct > 0 and row["name"].lower() != "other":
                out.append({"name": row["name"], "weight": pct})
        if out:
            return out

    return []


async def scrape_fund_info(isin: str, currency: str) -> dict:
    """Scrape asset type, sectors, and regions from the FT holdings tab."""
    async with _semaphore:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            loaded_url = None
            for url_tpl in _HOLDINGS_URLS:
                url = url_tpl.format(isin=isin.upper(), currency=currency.upper())
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response and response.status == 403:
                    raise FTBlockedError(f"FT returned 403 for {isin}")
                if not _is_search_page(page.url):
                    loaded_url = page.url
                    break

            if loaded_url is None:
                raise PriceNotFoundError(f"FT has no holdings page for ISIN {isin}")

            # Wait for allocation section to render
            try:
                await page.wait_for_selector("table", timeout=15000)
            except PwTimeout:
                pass

            name = await _extract_fund_name(page)
            assets = await _extract_allocation_table(page)
            sectors = await _extract_weightings_table(page)

            # Click the "Regions" tab and wait for the table header to switch
            regions = []
            try:
                regions_btn = await page.query_selector(
                    "li:has-text('Regions'), button:has-text('Regions'), a:has-text('Regions')"
                )
                if regions_btn:
                    await regions_btn.click()
                    await page.wait_for_timeout(500)  # CSS show/hide is synchronous
                    regions = await _extract_weightings_table(page)
            except Exception:
                pass

            # Fallback: scrape regions from the summary page Diversification section
            # when the holdings tab Regions tab click didn't yield data
            if not regions:
                regions = await _scrape_summary_regions(page, isin, currency)

            # Derive a simple asset_type label from the assets breakdown
            asset_type = None
            if assets:
                stock_pct = sum(
                    a["weight"] for a in assets
                    if "stock" in a["name"].lower() or "equity" in a["name"].lower()
                )
                bond_pct = sum(
                    a["weight"] for a in assets
                    if "bond" in a["name"].lower() or "fixed" in a["name"].lower()
                )
                if stock_pct >= 70:
                    asset_type = "Equity"
                elif bond_pct >= 70:
                    asset_type = "Fixed Income"
                elif stock_pct > 0 and bond_pct > 0:
                    asset_type = "Mixed"
                elif assets:
                    asset_type = assets[0]["name"]

            return {
                "isin": isin.upper(),
                "name": name,
                "asset_type": asset_type,
                "fund_type": "ETF" if "etfs" in loaded_url else "Fund",
                "assets": assets,
                "sectors": sectors,
                "regions": regions,
            }
        finally:
            await context.close()


async def scrape_price(isin: str, currency: str) -> float:
    async with _semaphore:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            url = FT_URL.format(isin=isin.upper(), currency=currency.upper())
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            if response and response.status == 403:
                raise FTBlockedError(f"FT returned 403 for {isin}")

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
            await context.close()
