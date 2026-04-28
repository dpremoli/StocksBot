"""
API tests — all scraper calls are mocked; no network or Playwright needed.
Uses an in-memory SQLite DB (via tmp_path fixture) to keep tests isolated.
"""
import datetime
import os
import pytest
import pytest_asyncio
import aiosqlite
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_tmp_db(tmp_path, monkeypatch):
    """Point the cache module at a temp DB so tests don't share state."""
    db_file = str(tmp_path / "prices.db")
    monkeypatch.setattr("app.cache.DB_PATH", db_file)
    return db_file


@pytest_asyncio.fixture
async def client(use_tmp_db):
    # ASGITransport does not trigger FastAPI lifespan, so init the DB explicitly
    from app.cache import init_db
    await init_db()
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _insert_price(db_path, isin, currency, price, date=None):
    date = date or datetime.datetime.now(datetime.UTC).date().isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO prices(isin,currency,date,price,fetched_at) VALUES(?,?,?,?,?)",
            (isin, currency, date, price, datetime.datetime.now(datetime.UTC).isoformat()),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_price_cache_miss_then_scrape(client):
    with patch("app.main.scrape_price", new_callable=AsyncMock, return_value=11.5) as mock_scrape:
        r = await client.get("/price?isin=LU1022659046&currency=EUR")
    assert r.status_code == 200
    assert r.json() == {"price": 11.5}
    mock_scrape.assert_awaited_once_with("LU1022659046", "EUR")


@pytest.mark.asyncio
async def test_price_cache_hit(client, use_tmp_db):
    await _insert_price(use_tmp_db, "LU1022659046", "EUR", 10.19)
    with patch("app.main.scrape_price", new_callable=AsyncMock) as mock_scrape:
        r = await client.get("/price?isin=LU1022659046&currency=EUR")
    assert r.status_code == 200
    assert r.json() == {"price": 10.19}
    mock_scrape.assert_not_awaited()


@pytest.mark.asyncio
async def test_price_scraper_called_only_once_per_day(client):
    with patch("app.main.scrape_price", new_callable=AsyncMock, return_value=10.19) as mock_scrape:
        r1 = await client.get("/price?isin=LU1022659046&currency=EUR")
        r2 = await client.get("/price?isin=LU1022659046&currency=EUR")
    assert r1.status_code == 200
    assert r2.status_code == 200
    mock_scrape.assert_awaited_once()


@pytest.mark.asyncio
async def test_price_not_found(client):
    from app.scraper import PriceNotFoundError
    with patch("app.main.scrape_price", new_callable=AsyncMock, side_effect=PriceNotFoundError("no data")):
        r = await client.get("/price?isin=LU1022659046&currency=EUR")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_price_ft_blocked(client):
    from app.scraper import FTBlockedError
    with patch("app.main.scrape_price", new_callable=AsyncMock, side_effect=FTBlockedError("403")):
        r = await client.get("/price?isin=LU1022659046&currency=EUR")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_batch_mixed_results(client):
    from app.scraper import PriceNotFoundError

    async def fake_scrape(isin, currency):
        if isin == "LU1022659046":
            return 10.19
        raise PriceNotFoundError("not found")

    with patch("app.main.scrape_price", new_callable=AsyncMock, side_effect=fake_scrape):
        with patch("asyncio.sleep", new_callable=AsyncMock):  # skip delay in tests
            r = await client.post(
                "/batch",
                json={"isins": ["LU1022659046", "IE0000000000"], "currency": "EUR"},
            )
    assert r.status_code == 200
    data = r.json()
    assert data["prices"]["LU1022659046"] == 10.19
    assert data["errors"]["IE0000000000"] == "not_found"


@pytest.mark.asyncio
async def test_cache_expiry_triggers_rescrape(client, use_tmp_db):
    yesterday = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)).date().isoformat()
    await _insert_price(use_tmp_db, "LU1022659046", "EUR", 9.99, date=yesterday)

    with patch("app.main.scrape_price", new_callable=AsyncMock, return_value=10.19) as mock_scrape:
        r = await client.get("/price?isin=LU1022659046&currency=EUR")
    assert r.status_code == 200
    assert r.json() == {"price": 10.19}
    mock_scrape.assert_awaited_once()
