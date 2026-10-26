import datetime
import aiosqlite

DB_PATH = "/data/prices.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prices (
                isin       TEXT NOT NULL,
                currency   TEXT NOT NULL,
                date       TEXT NOT NULL,
                price      REAL NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (isin, currency, date)
            )
        """)
        # xids (id only, could hold a listing in the wrong currency) is replaced by listings
        await db.execute("DROP TABLE IF EXISTS xids")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                isin       TEXT NOT NULL,
                currency   TEXT NOT NULL,
                xid        TEXT NOT NULL,
                symbol     TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (isin, currency)
            )
        """)
        await db.commit()


def _today() -> str:
    return datetime.datetime.utcnow().date().isoformat()


async def get_cached(isin: str, currency: str) -> float | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT price FROM prices WHERE isin=? AND currency=? AND date=?",
            (isin.upper(), currency.upper(), _today()),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_cached(isin: str, currency: str, price: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO prices(isin, currency, date, price, fetched_at) "
            "VALUES(?,?,?,?,?)",
            (
                isin.upper(),
                currency.upper(),
                _today(),
                price,
                datetime.datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()


async def get_cached_listing(isin: str, currency: str) -> tuple[str, str] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT xid, symbol FROM listings WHERE isin=? AND currency=?",
            (isin.upper(), currency.upper()),
        )
        row = await cursor.fetchone()
        return (row[0], row[1]) if row else None


async def set_cached_listing(isin: str, currency: str, xid: str, symbol: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO listings(isin, currency, xid, symbol, fetched_at) VALUES(?,?,?,?,?)",
            (isin.upper(), currency.upper(), xid, symbol, datetime.datetime.utcnow().isoformat()),
        )
        await db.commit()
