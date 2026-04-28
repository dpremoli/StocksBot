import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from app.cache import init_db, get_cached, set_cached
from app.models import BatchRequest, BatchResponse
from app.scraper import scrape_price, PriceNotFoundError, FTBlockedError


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
        return {"price": cached}

    try:
        price = await scrape_price(isin, currency)
    except PriceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FTBlockedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape error: {e}")

    await set_cached(isin, currency, price)
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
        except PriceNotFoundError:
            errors[isin] = "not_found"
        except FTBlockedError:
            errors[isin] = "blocked"
        except Exception as e:
            errors[isin] = f"error: {e}"
        # polite delay between requests to avoid FT rate limiting
        await asyncio.sleep(1.5)

    return BatchResponse(prices=prices, errors=errors)
