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
    date = date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO prices(isin,currency,date,price,fetched_at) VALUES(?,?,?,?,?)",
            (isin, currency, date, price, datetime.datetime.now(datetime.timezone.utc).isoformat()),
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
    yesterday = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).date().isoformat()
    await _insert_price(use_tmp_db, "LU1022659046", "EUR", 9.99, date=yesterday)

    with patch("app.main.scrape_price", new_callable=AsyncMock, return_value=10.19) as mock_scrape:
        r = await client.get("/price?isin=LU1022659046&currency=EUR")
    assert r.status_code == 200
    assert r.json() == {"price": 10.19}
    mock_scrape.assert_awaited_once()


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

FT_ROWS = (
    '<tr><td class="mod-ui-table__cell--text"><span class="mod-ui-hide-small-below">Friday, October 23, 2026</span>'
    '<span class="mod-ui-hide-medium-above">Fri, Oct 23, 2026</span></td><td>24.31</td><td>24.31</td><td>24.31</td>'
    '<td>24.31</td><td><span class="mod-ui-hide-small-below">0</span><span class="mod-ui-hide-medium-above">0.00</span></td></tr>'
    '<tr><td class="mod-ui-table__cell--text"><span class="mod-ui-hide-small-below">Thursday, October 22, 2026</span>'
    '<span class="mod-ui-hide-medium-above">Thu, Oct 22, 2026</span></td><td>1,024.30</td><td>1,024.30</td><td>1,024.30</td>'
    '<td>1,024.30</td><td><span class="mod-ui-hide-small-below">0</span><span class="mod-ui-hide-medium-above">0.00</span></td></tr>'
)


def test_parse_history_html():
    from app.history import parse_history_html
    rows = parse_history_html(FT_ROWS)
    assert rows == [
        {"date": "2026-10-23", "open": 24.31, "high": 24.31, "low": 24.31, "close": 24.31},
        {"date": "2026-10-22", "open": 1024.3, "high": 1024.3, "low": 1024.3, "close": 1024.3},
    ]


def test_parse_history_html_skips_bad_rows():
    from app.history import parse_history_html
    assert parse_history_html("<tr><td>Date</td><td>Open</td></tr>") == []


@pytest.mark.asyncio
async def test_history_resolves_and_caches_listing(client):
    rows = [{"date": "2026-10-22", "open": None, "high": None, "low": None, "close": 24.31}]
    with patch("app.main.fetch_history", new_callable=AsyncMock, return_value=("6011", "LU0113257694:EUR", rows)) as mock_fetch:
        r1 = await client.get("/history?isin=lu0113257694&currency=eur&start=2026-10-01&end=2026-10-23")
        r2 = await client.get("/history?isin=LU0113257694&currency=EUR&start=2026-10-01&end=2026-10-23")
    assert r1.status_code == 200
    assert r1.json() == {"isin": "LU0113257694", "currency": "EUR", "xid": "6011", "symbol": "LU0113257694:EUR", "prices": rows}
    assert r2.status_code == 200
    first, second = mock_fetch.await_args_list
    assert first.kwargs["listing"] is None
    assert second.kwargs["listing"] == ("6011", "LU0113257694:EUR")
    assert first.args[2] == datetime.date(2026, 10, 1) and first.args[3] == datetime.date(2026, 10, 23)


@pytest.mark.asyncio
async def test_history_rejects_reversed_range(client):
    with patch("app.main.fetch_history", new_callable=AsyncMock) as mock_fetch:
        r = await client.get("/history?isin=LU0113257694&start=2026-10-23&end=2026-10-01")
    assert r.status_code == 400
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_not_found(client):
    from app.scraper import PriceNotFoundError
    with patch("app.main.fetch_history", new_callable=AsyncMock, side_effect=PriceNotFoundError("no page")):
        r = await client.get("/history?isin=XX0000000000")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_history_ft_blocked(client):
    from app.scraper import FTBlockedError
    with patch("app.main.fetch_history", new_callable=AsyncMock, side_effect=FTBlockedError("403")):
        r = await client.get("/history?isin=LU0113257694")
    assert r.status_code == 503


VANGUARD_LISTINGS = [
    {"symbol": "VUSA:LSE:GBP", "xid": "46487963"},
    {"symbol": "VUSD:LSE:USD", "xid": "46487965"},
    {"symbol": "VUSA:AEX:EUR", "xid": "58518299"},
]


def test_pick_listing_matches_requested_currency():
    from app.history import pick_listing
    assert pick_listing(VANGUARD_LISTINGS, "IE00B3XXRP09", "USD")["symbol"] == "VUSD:LSE:USD"
    assert pick_listing(VANGUARD_LISTINGS, "IE00B3XXRP09", "EUR")["symbol"] == "VUSA:AEX:EUR"


def test_pick_listing_prefers_fund_share_class():
    from app.history import pick_listing
    listings = [{"symbol": "ZPJS:FRA:EUR", "xid": "1"}, {"symbol": "LU0113257694:EUR", "xid": "6011"}]
    assert pick_listing(listings, "LU0113257694", "EUR")["xid"] == "6011"


def test_pick_listing_refuses_other_currencies():
    from app.history import pick_listing
    assert pick_listing(VANGUARD_LISTINGS, "IE00B3XXRP09", "CHF") is None
