import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query

from app.cache import (
    init_db, get_cached, set_cached, get_cached_info, set_cached_info, get_cached_listing, set_cached_listing,
)
from app.history import fetch_history
from app.listings import find_listing
from app.models import BatchRequest, BatchResponse, FundInfo, BatchInfoRequest, BatchInfoResponse, HistoryResponse
from app.scraper import scrape_price, scrape_fund_info, get_browser, close_browser, PriceNotFoundError, FTBlockedError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
logging.Formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()
log = logging.getLogger(__name__)

MAX_HISTORY_DAYS = 365 * 15


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await get_browser()
    yield
    await close_browser()


app = FastAPI(title="FT NAV Price Bot", lifespan=lifespan)


async def resolve_listing(isin: str, currency: str) -> tuple[str, str]:
    """(xid, symbol) of the FT listing of isin quoted in currency, from cache or FT search."""
    listing = await get_cached_listing(isin, currency)
    if listing is None:
        listing = await find_listing(isin, currency)
        await set_cached_listing(isin, currency, *listing)
    return listing


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/price")
async def get_price(
    isin: str = Query(..., min_length=12, max_length=12),
    currency: str = Query("EUR", min_length=3, max_length=3),
):
    isin, currency = isin.upper(), currency.upper()
    cached = await get_cached(isin, currency)
    if cached is not None:
        log.info("CACHE HIT  %s/%s -> %s", isin, currency, cached)
        return {"price": cached}

    try:
        _, symbol = await resolve_listing(isin, currency)
        price = await scrape_price(symbol)
    except PriceNotFoundError as e:
        log.warning("NOT FOUND  %s/%s -> %s", isin.upper(), currency.upper(), e)
        raise HTTPException(status_code=404, detail=str(e))
    except FTBlockedError as e:
        log.warning("BLOCKED    %s/%s -> %s", isin.upper(), currency.upper(), e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        log.error("ERROR      %s/%s -> %s", isin.upper(), currency.upper(), e)
        raise HTTPException(status_code=500, detail=f"Scrape error: {e}")

    await set_cached(isin, currency, price)
    log.info("SCRAPED    %s/%s (%s) -> %s", isin, currency, symbol, price)
    return {"price": price}


@app.get("/history", response_model=HistoryResponse)
async def get_history(
    isin: str = Query(..., min_length=12, max_length=12),
    currency: str = Query("EUR", min_length=3, max_length=3),
    start: date | None = Query(None, description="First date (YYYY-MM-DD); defaults to a year before end"),
    end: date | None = Query(None, description="Last date (YYYY-MM-DD); defaults to today"),
):
    end = end or datetime.now(timezone.utc).date()
    start = start or end - timedelta(days=365)
    if start > end:
        raise HTTPException(status_code=400, detail="start must be on or before end")
    if (end - start).days > MAX_HISTORY_DAYS:
        raise HTTPException(status_code=400, detail=f"range is limited to {MAX_HISTORY_DAYS} days")

    isin, currency = isin.upper(), currency.upper()
    try:
        listing = await resolve_listing(isin, currency)
        xid, symbol, rows = await fetch_history(isin, currency, start, end, listing=listing)
    except PriceNotFoundError as e:
        log.warning("NOT FOUND  history %s/%s -> %s", isin, currency, e)
        raise HTTPException(status_code=404, detail=str(e))
    except FTBlockedError as e:
        log.warning("BLOCKED    history %s/%s -> %s", isin, currency, e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        log.error("ERROR      history %s/%s -> %s", isin, currency, e)
        raise HTTPException(status_code=500, detail=f"History error: {e}")

    log.info("HISTORY    %s/%s (%s) %s..%s -> %d rows", isin, currency, symbol, start, end, len(rows))
    return HistoryResponse(isin=isin, currency=currency, xid=xid, symbol=symbol, prices=rows)


@app.post("/batch", response_model=BatchResponse)
async def batch_prices(body: BatchRequest):
    prices: dict[str, float] = {}
    errors: dict[str, str] = {}

    for isin in body.isins:
        cached = await get_cached(isin, body.currency)
        if cached is not None:
            prices[isin] = cached
            continue
        try:
            _, symbol = await resolve_listing(isin.upper(), body.currency.upper())
            price = await scrape_price(symbol)
            await set_cached(isin, body.currency, price)
            prices[isin] = price
            log.info("SCRAPED    %s/%s (%s) -> %s", isin.upper(), body.currency.upper(), symbol, price)
        except PriceNotFoundError:
            errors[isin] = "not_found"
            log.warning("NOT FOUND  %s/%s", isin.upper(), body.currency.upper())
        except FTBlockedError:
            errors[isin] = "blocked"
            log.warning("BLOCKED    %s/%s", isin.upper(), body.currency.upper())
        except Exception as e:
            errors[isin] = f"error: {e}"
            log.error("ERROR      %s/%s -> %s", isin.upper(), body.currency.upper(), e)
        # polite delay between requests to avoid FT rate limiting
        await asyncio.sleep(1.5)

    return BatchResponse(prices=prices, errors=errors)


@app.get("/info", response_model=FundInfo)
async def get_fund_info(
    isin: str = Query(..., min_length=12, max_length=12),
    currency: str = Query("EUR", min_length=3, max_length=3),
):
    cached = await get_cached_info(isin)
    if cached is not None:
        log.info("INFO CACHE HIT  %s", isin.upper())
        return FundInfo(**cached)

    try:
        data = await scrape_fund_info(isin, currency)
    except PriceNotFoundError as e:
        log.warning("INFO NOT FOUND  %s -> %s", isin.upper(), e)
        raise HTTPException(status_code=404, detail=str(e))
    except FTBlockedError as e:
        log.warning("INFO BLOCKED    %s -> %s", isin.upper(), e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        log.error("INFO ERROR      %s -> %s", isin.upper(), e)
        raise HTTPException(status_code=500, detail=f"Scrape error: {e}")

    await set_cached_info(isin, data)
    log.info("INFO SCRAPED    %s -> %s sectors, %s regions", isin.upper(), len(data["sectors"]), len(data["regions"]))
    return FundInfo(**data)


@app.post("/batch-info", response_model=BatchInfoResponse)
async def batch_fund_info(body: BatchInfoRequest):
    info: dict[str, FundInfo] = {}
    errors: dict[str, str] = {}

    for isin in body.isins:
        cached = await get_cached_info(isin)
        if cached is not None:
            info[isin] = FundInfo(**cached)
            continue
        try:
            data = await scrape_fund_info(isin, body.currency)
            await set_cached_info(isin, data)
            info[isin] = FundInfo(**data)
            log.info("INFO SCRAPED    %s", isin.upper())
        except PriceNotFoundError:
            errors[isin] = "not_found"
        except FTBlockedError:
            errors[isin] = "blocked"
        except Exception as e:
            errors[isin] = f"error: {e}"
            log.error("INFO ERROR      %s -> %s", isin.upper(), e)
        await asyncio.sleep(1.5)

    return BatchInfoResponse(info=info, errors=errors)
