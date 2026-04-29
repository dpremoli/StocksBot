import datetime
import json
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS fund_info (
                isin       TEXT NOT NULL,
                date       TEXT NOT NULL,
                data       TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (isin, date)
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


async def get_cached_info(isin: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT data FROM fund_info WHERE isin=? AND date=?",
            (isin.upper(), _today()),
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else None


async def set_cached_info(isin: str, data: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO fund_info(isin, date, data, fetched_at) VALUES(?,?,?,?)",
            (isin.upper(), _today(), json.dumps(data), datetime.datetime.utcnow().isoformat()),
        )
        await db.commit()
