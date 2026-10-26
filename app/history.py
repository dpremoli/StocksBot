import datetime
import html
import re

import httpx

from app.scraper import FTBlockedError, PriceNotFoundError, _USER_AGENT

FT_SEARCH_URL = "https://markets.ft.com/data/searchapi/searchsecurities"
FT_HISTORY_URL = "https://markets.ft.com/data/equities/ajax/get-historical-prices"

# FT returns at most about a year of rows per request, so longer ranges are fetched in chunks
_CHUNK_DAYS = 365

_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_LONG_DATE_RE = re.compile(r'mod-ui-hide-small-below">([^<]+)<')


def _parse_number(raw: str) -> float | None:
    cleaned = re.sub(r"<[^>]+>", "", raw).replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_history_html(fragment: str) -> list[dict]:
    """Parse FT's historical-prices table rows into [{date, open, high, low, close}]."""
    rows = []
    for row in _ROW_RE.findall(fragment):
        cells = _CELL_RE.findall(row)
        if len(cells) < 5:
            continue
        date_match = _LONG_DATE_RE.search(cells[0])
        if not date_match:
            continue
        try:
            date = datetime.datetime.strptime(html.unescape(date_match.group(1)).strip(), "%A, %B %d, %Y").date()
        except ValueError:
            continue
        close = _parse_number(cells[4])
        if close is None or close <= 0:
            continue
        rows.append({
            "date": date.isoformat(),
            "open": _parse_number(cells[1]),
            "high": _parse_number(cells[2]),
            "low": _parse_number(cells[3]),
            "close": close,
        })
    return rows


def _check_response(response: httpx.Response, isin: str):
    if response.status_code == 403:
        raise FTBlockedError(f"FT returned 403 for {isin}")
    response.raise_for_status()


def pick_listing(securities: list[dict], isin: str, currency: str) -> dict | None:
    """Choose the FT listing quoted in `currency`: the fund share class ISIN:CCY first, then any exchange listing ending :CCY."""
    exact = f"{isin}:{currency}"
    for security in securities:
        if security.get("symbol") == exact:
            return security
    for security in securities:
        if str(security.get("symbol", "")).endswith(f":{currency}"):
            return security
    return None


async def resolve_listing(client: httpx.AsyncClient, isin: str, currency: str) -> tuple[str, str]:
    """Return (xid, symbol) for the FT listing of an ISIN quoted in the requested currency.

    FT falls back to a default listing when ISIN:CCY has no page, which can be in another
    currency (e.g. VUSA in GBP for a USD request), so the listing is chosen from FT's search
    results by its currency suffix instead.
    """
    response = await client.get(FT_SEARCH_URL, params={"query": isin})
    _check_response(response, isin)
    securities = response.json().get("data", {}).get("security", [])
    listing = pick_listing(securities, isin, currency)
    if listing is None:
        found = ", ".join(s.get("symbol", "?") for s in securities) or "nothing"
        raise PriceNotFoundError(f"FT has no {currency} listing for ISIN {isin} (found {found})")
    return str(listing["xid"]), listing["symbol"]


async def fetch_history(
    isin: str,
    currency: str,
    start: datetime.date,
    end: datetime.date,
    listing: tuple[str, str] | None = None,
) -> tuple[str, str, list[dict]]:
    """Return (xid, symbol, rows) with FT daily prices between start and end inclusive, oldest first."""
    isin, currency = isin.upper(), currency.upper()
    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT}, follow_redirects=True, timeout=30
    ) as client:
        xid, symbol = listing or await resolve_listing(client, isin, currency)
        by_date: dict[str, dict] = {}
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + datetime.timedelta(days=_CHUNK_DAYS - 1), end)
            response = await client.get(FT_HISTORY_URL, params={
                "startDate": chunk_start.strftime("%Y/%m/%d"),
                "endDate": chunk_end.strftime("%Y/%m/%d"),
                "symbol": xid,
            })
            _check_response(response, isin)
            for row in parse_history_html(response.json().get("html", "")):
                by_date[row["date"]] = row
            chunk_start = chunk_end + datetime.timedelta(days=1)
    return xid, symbol, [by_date[d] for d in sorted(by_date)]
