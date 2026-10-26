import httpx

from app.scraper import FTBlockedError, PriceNotFoundError, _USER_AGENT

FT_SEARCH_URL = "https://markets.ft.com/data/searchapi/searchsecurities"


def check_response(response: httpx.Response, isin: str):
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
    check_response(response, isin)
    securities = response.json().get("data", {}).get("security", [])
    listing = pick_listing(securities, isin, currency)
    if listing is None:
        found = ", ".join(s.get("symbol", "?") for s in securities) or "nothing"
        raise PriceNotFoundError(f"FT has no {currency} listing for ISIN {isin} (found {found})")
    return str(listing["xid"]), listing["symbol"]


async def find_listing(isin: str, currency: str) -> tuple[str, str]:
    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT}, follow_redirects=True, timeout=30
    ) as client:
        return await resolve_listing(client, isin.upper(), currency.upper())
