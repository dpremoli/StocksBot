import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from app.cache import init_db, get_cached, set_cached
from app.models import BatchRequest, BatchResponse
from app.scraper import scrape_price, PriceNotFoundError, FTBlockedError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
logging.Formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="FT NAV Price Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/price")
async def get_price(
    isin: str = Query(..., min_length=12, max_length=12),
    currency: str = Query("EUR", min_length=3, max_length=3),
):
    cached = await get_cached(isin, currency)
    if cached is not None:
        log.info("CACHE HIT  %s/%s -> %s", isin.upper(), currency.upper(), cached)
        return {"price": cached}

    try:
        price = await scrape_price(isin, currency)
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
    log.info("SCRAPED    %s/%s -> %s", isin.upper(), currency.upper(), price)
    return {"price": price}


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
            price = await scrape_price(isin, body.currency)
            await set_cached(isin, body.currency, price)
            prices[isin] = price
            log.info("SCRAPED    %s/%s -> %s", isin.upper(), body.currency.upper(), price)
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
