"""
Sync fund taxonomy data (sectors, regions, asset class, instrument type)
from the price bot's /info endpoint into Wealthfolio's taxonomy tables.

Run on the NAS where both databases are accessible:
    python3 sync_wealthfolio_taxonomies.py

Requirements: pip install requests
"""

import os
import sqlite3
import uuid
import requests
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────────────────────────
WEALTHFOLIO_DB  = os.environ.get("WF_DB", "/mnt/user/appdata/wealthfolio/wealthfolio.db")
PRICE_BOT_URL   = os.environ.get("PRICE_BOT_URL", "http://localhost:8150")
DRY_RUN         = os.environ.get("DRY_RUN", "").lower() in ("1", "true")

# ── Sector name → industries_gics category ID ──────────────────────────────
# FT uses Morningstar sector names; map to closest GICS industry group.
# If a name isn't in this dict a new top-level category is created automatically.
FT_SECTOR_TO_GICS = {
    "Energy":                  "10",
    "Basic Materials":         "151010",
    "Industrials":             "2010",
    "Consumer Cyclical":       "2510",
    "Consumer Defensive":      "3010",
    "Healthcare":              "3510",
    "Financial Services":      "402010",
    "Technology":              "4510",
    "Communication Services":  "5010",
    "Utilities":               "551010",
    "Real Estate":             "6010",
}

# ── FT region name → regions category ID ───────────────────────────────────
# Country-level names map to country_XX codes; broader names to region codes.
FT_REGION_TO_ID = {
    # continents / macro
    "Americas":           "R20",
    "Greater Europe":     "R10",
    "Europe":             "R10",
    "Greater Asia":       "R30",
    "Asia":               "R30",
    "Africa":             "R40",
    "Oceania":            "R50",
    # sub-regions
    "Northern America":   "R2010",
    "Latin America":      "R2040",
    "Northern Europe":    "R1010",
    "Western Europe":     "R1020",
    "Eastern Europe":     "R1030",
    "Southern Europe":    "R1040",
    "Eurozone":           "R1020",   # closest match
    "Europe - ex Euro":   "R10",
    "Asia - Developed":   "R3030",
    "Asia - Emerging":    "R30",
    "Emerging Markets":   "R30",
    "Emerging Market":    "R30",
    "Japan":              "country_JP",
    "Australasia":        "R5010",
    # countries
    "United States":      "country_US",
    "Canada":             "country_CA",
    "United Kingdom":     "country_GB",
    "Germany":            "country_DE",
    "France":             "country_FR",
    "Switzerland":        "country_CH",
    "Netherlands":        "country_NL",
    "Sweden":             "country_SE",
    "Spain":              "country_ES",
    "Italy":              "country_IT",
    "Denmark":            "country_DK",
    "Norway":             "country_NO",
    "Finland":            "country_FI",
    "Poland":             "country_PL",
    "Ireland":            "country_IE",
    "Belgium":            "country_BE",
    "Austria":            "country_AT",
    "Portugal":           "country_PT",
    "Luxembourg":         "country_LU",
    "Australia":          "country_AU",
    "New Zealand":        "country_NZ",
    "China":              "country_CN",
    "Japan":              "country_JP",
    "South Korea":        "country_KR",
    "Korea":              "country_KR",
    "Taiwan":             "country_TW",
    "Hong Kong":          "country_HK",
    "Singapore":          "country_SG",
    "India":              "country_IN",
    "Brazil":             "country_BR",
    "Mexico":             "country_MX",
    "South Africa":       "country_ZA",
    "Russia":             "country_RU",
    "Saudi Arabia":       "country_SA",
    "Israel":             "country_IL",
    "Turkey":             "country_TR",
    "Indonesia":          "country_ID",
    "Malaysia":           "country_MY",
    "Thailand":           "country_TH",
    "Philippines":        "country_PH",
    "United Arab Emirates": "country_AE",
}

# ── Asset type → asset_classes category ID ─────────────────────────────────
# Use top-level IDs only — Wealthfolio auto-rolls up children and would
# double-count if we assigned both parent and child.
FT_ASSET_TYPE_TO_CLASS = {
    "Equity":         "EQUITY",
    "Fixed Income":   "FIXED_INCOME",
    "Mixed":          "EQUITY",
    "Cash":           "CASH",
    "Commodity":      "COMM_PRECIOUS",
    "Real Estate":    "RE_PUBLIC_REITS",
}

# FT macro region names that aggregate child rows — skip these when the
# response also contains their children (to avoid double-counting weights).
FT_MACRO_REGIONS = {
    "Americas", "Greater Europe", "Greater Asia",
    "Europe", "Asia", "Africa", "Oceania",
    "Northern America", "Latin America",
}

# ── Fund type → instrument_type category ID ────────────────────────────────
FT_FUND_TYPE_TO_INSTRUMENT = {
    "ETF":  "ETF",
    "Fund": "FUND_MUTUAL",
}

# ───────────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ensure_category(db: sqlite3.Connection, taxonomy_id: str, cat_id: str,
                    name: str, parent_id: str | None = None) -> str:
    """Return cat_id after creating the category row if it doesn't exist."""
    row = db.execute(
        "SELECT id FROM taxonomy_categories WHERE taxonomy_id=? AND id=?",
        (taxonomy_id, cat_id),
    ).fetchone()
    if row:
        return cat_id

    ts = now_iso()
    db.execute(
        "INSERT INTO taxonomy_categories(id, taxonomy_id, parent_id, name, key, color, sort_order, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (cat_id, taxonomy_id, parent_id, name,
         cat_id.lower().replace(" ", "_"), "#808080", 999, ts, ts),
    )
    print(f"  + Created category [{taxonomy_id}] {cat_id} = {name!r}")
    return cat_id


def upsert_assignment(db: sqlite3.Connection, asset_id: str, taxonomy_id: str,
                      category_id: str, weight: int):
    """Insert or replace a taxonomy assignment (weight 0-10000)."""
    existing = db.execute(
        "SELECT id FROM asset_taxonomy_assignments WHERE asset_id=? AND taxonomy_id=? AND category_id=?",
        (asset_id, taxonomy_id, category_id),
    ).fetchone()

    ts = now_iso()
    if existing:
        db.execute(
            "UPDATE asset_taxonomy_assignments SET weight=?, source=?, updated_at=? WHERE id=?",
            (weight, "auto_scraper", ts, existing[0]),
        )
    else:
        db.execute(
            "INSERT INTO asset_taxonomy_assignments(id, asset_id, taxonomy_id, category_id, weight, source, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), asset_id, taxonomy_id, category_id, weight, "auto_scraper", ts, ts),
        )


def delete_auto_assignments(db: sqlite3.Connection, asset_id: str, taxonomy_id: str):
    db.execute(
        "DELETE FROM asset_taxonomy_assignments WHERE asset_id=? AND taxonomy_id=? AND source='auto_scraper'",
        (asset_id, taxonomy_id),
    )


def delete_all_assignments(db: sqlite3.Connection, asset_id: str, taxonomy_id: str):
    """Delete all assignments regardless of source — used for single-value
    taxonomies where Wealthfolio auto-adds parent entries with source='AUTO'."""
    db.execute(
        "DELETE FROM asset_taxonomy_assignments WHERE asset_id=? AND taxonomy_id=?",
        (asset_id, taxonomy_id),
    )


def process_asset(db: sqlite3.Connection, asset_id: str, isin: str, currency: str, name: str):
    print(f"\n{'─'*60}")
    print(f"  {name} ({isin}) [{currency}]")

    try:
        resp = requests.get(f"{PRICE_BOT_URL}/info", params={"isin": isin, "currency": currency}, timeout=60)
    except requests.RequestException as e:
        print(f"  ✗ Request failed: {e}")
        return

    if resp.status_code == 404:
        print(f"  ✗ Not found on FT")
        return
    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:120]}")
        return

    info = resp.json()
    print(f"  asset_type={info.get('asset_type')}  fund_type={info.get('fund_type')}")
    print(f"  sectors={len(info.get('sectors', []))}  regions={len(info.get('regions', []))}")

    if DRY_RUN:
        return

    # ── instrument_type (single-select) ────────────────────────────────────
    fund_type = info.get("fund_type")
    instr_id = FT_FUND_TYPE_TO_INSTRUMENT.get(fund_type)
    if instr_id:
        ensure_category(db, "instrument_type", instr_id, fund_type)
        delete_all_assignments(db, asset_id, "instrument_type")
        upsert_assignment(db, asset_id, "instrument_type", instr_id, 10000)

    # ── asset_classes ───────────────────────────────────────────────────────
    asset_type = info.get("asset_type")
    if asset_type:
        class_id = FT_ASSET_TYPE_TO_CLASS.get(asset_type)
        if not class_id:
            # Use asset breakdown if available for a better mapping
            assets = info.get("assets", [])
            stock_pct = sum(a["weight"] for a in assets if "stock" in a["name"].lower())
            bond_pct  = sum(a["weight"] for a in assets if "bond" in a["name"].lower())
            if stock_pct >= 50:
                class_id = "EQUITY_PUBLIC"
            elif bond_pct >= 50:
                class_id = "FIXED_INCOME"
            else:
                class_id = "EQUITY"

        ensure_category(db, "asset_classes", class_id, asset_type)
        delete_all_assignments(db, asset_id, "asset_classes")
        upsert_assignment(db, asset_id, "asset_classes", class_id, 10000)

    # ── industries_gics (with weights) ─────────────────────────────────────
    sectors = info.get("sectors", [])
    if sectors:
        delete_auto_assignments(db, asset_id, "industries_gics")
        for s in sectors:
            sname  = s["name"]
            weight = int(round(s["weight"] * 100))  # pct → 0-10000
            cat_id = FT_SECTOR_TO_GICS.get(sname)
            if cat_id is None:
                # Auto-create a top-level category using a safe key
                cat_id = "FT_" + sname.upper().replace(" ", "_").replace("-", "_")
                ensure_category(db, "industries_gics", cat_id, sname)
            upsert_assignment(db, asset_id, "industries_gics", cat_id, weight)
            print(f"    sector  {sname} → {cat_id}  ({s['weight']}%)")

    # ── regions (with weights) ──────────────────────────────────────────────
    regions = info.get("regions", [])
    if regions:
        # Drop macro rows (Americas, Greater Europe…) when the response also
        # contains their children — otherwise weights double-count.
        region_names = {r["name"] for r in regions}
        filtered_regions = [
            r for r in regions
            if r["name"] not in FT_MACRO_REGIONS
            or not any(
                r2["name"] not in FT_MACRO_REGIONS
                and abs(r2["weight"] - r["weight"]) < 1.0
                for r2 in regions
            )
        ]
        delete_auto_assignments(db, asset_id, "regions")
        for r in filtered_regions:
            rname  = r["name"]
            weight = int(round(r["weight"] * 100))
            cat_id = FT_REGION_TO_ID.get(rname)
            if cat_id is None:
                cat_id = "FT_" + rname.upper().replace(" ", "_").replace("-", "_").replace("(", "").replace(")", "")
                ensure_category(db, "regions", cat_id, rname)
            upsert_assignment(db, asset_id, "regions", cat_id, weight)
            print(f"    region  {rname} → {cat_id}  ({r['weight']}%)")


def main():
    db = sqlite3.connect(WEALTHFOLIO_DB)
    db.row_factory = sqlite3.Row

    # All active assets that have the custom scraper configured
    # ISIN is stored in instrument_symbol; currency in quote_ccy
    assets = db.execute("""
        SELECT id, instrument_symbol AS isin, quote_ccy AS currency, name
        FROM assets
        WHERE is_active = 1
          AND instrument_symbol IS NOT NULL
          AND instrument_symbol != ''
          AND provider_config LIKE '%CUSTOM_SCRAPER%'
        ORDER BY name
    """).fetchall()

    print(f"Found {len(assets)} active assets with custom scraper\n")

    if not assets:
        assets = db.execute("""
            SELECT id, instrument_symbol AS isin, quote_ccy AS currency, name
            FROM assets
            WHERE is_active = 1
              AND instrument_symbol IS NOT NULL AND instrument_symbol != ''
            ORDER BY name
        """).fetchall()
        print(f"Falling back to all {len(assets)} active assets with ISIN\n")

    ok = 0
    fail = 0
    for row in assets:
        try:
            process_asset(db, row["id"], row["isin"], row["currency"] or "EUR", row["name"])
            if not DRY_RUN:
                db.commit()
            ok += 1
        except Exception as e:
            print(f"  ✗ Error processing {row['isin']}: {e}")
            db.rollback()
            fail += 1

    db.close()
    print(f"\n{'='*60}")
    print(f"Done — {ok} assets synced, {fail} failed")
    if DRY_RUN:
        print("(DRY RUN — no changes written)")


if __name__ == "__main__":
    main()
