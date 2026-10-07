"""
Sync fund taxonomy data (sectors, regions, asset class, instrument type)
from the price bot's /info endpoint into Wealthfolio via its REST API.

Earlier versions wrote straight into Wealthfolio's SQLite file, which bypassed
the app's validation (weights could exceed 100%, duplicate categories) and
raced the running app. This version talks to the Wealthfolio server
(the self-hosted web/Docker build) instead, so it can run while Wealthfolio is
up and every write goes through the same checks as the UI.

Usage:
    WF_URL=http://localhost:8088 WF_PASSWORD=... python3 sync_wealthfolio_taxonomies.py

Environment:
    WF_URL         Wealthfolio server base URL        (default http://localhost:8088)
    WF_PASSWORD    Wealthfolio login password, if auth is enabled
    WF_TOKEN       Alternatively, a bearer token for the API
    PRICE_BOT_URL  StocksBot base URL                 (default http://localhost:8150)
    DRY_RUN        "1"/"true" to fetch and print without writing

Requirements: pip install requests
"""

import os
import re
import sys
from collections import OrderedDict

import requests

# ── Config ─────────────────────────────────────────────────────────────────
WF_URL          = os.environ.get("WF_URL", "http://localhost:8088").rstrip("/")
WF_PASSWORD     = os.environ.get("WF_PASSWORD", "")
WF_TOKEN        = os.environ.get("WF_TOKEN", "")
PRICE_BOT_URL   = os.environ.get("PRICE_BOT_URL", "http://localhost:8150").rstrip("/")
DRY_RUN         = os.environ.get("DRY_RUN", "").lower() in ("1", "true")

SOURCE = "auto_scraper"
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

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
# Pure helpers (unit-tested in tests/test_taxonomy_sync.py)
# ───────────────────────────────────────────────────────────────────────────

def fallback_category_id(name: str, strip_parens: bool = False) -> str:
    """Id for an FT label with no mapping. Kept byte-for-byte compatible with the
    ids earlier versions created ("Asia - Emerging" → FT_ASIA___EMERGING), so
    existing categories are reused rather than duplicated."""
    cat = name.upper().replace(" ", "_").replace("-", "_")
    if strip_parens:
        cat = cat.replace("(", "").replace(")", "")
    return "FT_" + cat


def filter_macro_regions(regions: list[dict]) -> list[dict]:
    """Drop macro rows (Americas, Greater Europe…) that merely restate a child
    row's weight, so weights don't double-count."""
    return [
        r for r in regions
        if r["name"] not in FT_MACRO_REGIONS
        or not any(
            r2["name"] not in FT_MACRO_REGIONS and abs(r2["weight"] - r["weight"]) < 1.0
            for r2 in regions
        )
    ]


def weighted_assignments(items: list[dict], mapping: dict[str, str],
                         strip_parens: bool = False) -> "OrderedDict[str, int]":
    """Turn FT `{name, weight%}` rows into `{category_id: basis_points}`.

    - Several FT labels can map to one category (Eurozone and Western Europe both
      → R1020); their weights are summed rather than overwritten.
    - Negative/zero weights (short positions, rounding) are dropped.
    - If FT's rounding pushes the total over 100%, weights are scaled down
      proportionally — Wealthfolio rejects totals above 10000 bp.
    """
    merged: "OrderedDict[str, float]" = OrderedDict()
    for item in items:
        weight = float(item.get("weight") or 0)
        if weight <= 0:
            continue
        cat = mapping.get(item["name"]) or fallback_category_id(item["name"], strip_parens)
        merged[cat] = merged.get(cat, 0.0) + weight * 100

    total = sum(merged.values())
    scale = 10000 / total if total > 10000 else 1.0
    out: "OrderedDict[str, int]" = OrderedDict()
    for cat, bp in merged.items():
        value = int(round(bp * scale))
        if value >= 1:
            out[cat] = value
    # Rounding after scaling can still overshoot by a basis point or two.
    overshoot = sum(out.values()) - 10000
    if overshoot > 0:
        biggest = max(out, key=out.get)
        out[biggest] -= overshoot
    return out


def asset_class_for(info: dict) -> str | None:
    asset_type = info.get("asset_type")
    if not asset_type:
        return None
    class_id = FT_ASSET_TYPE_TO_CLASS.get(asset_type)
    if class_id:
        return class_id
    assets = info.get("assets", [])
    stock_pct = sum(a["weight"] for a in assets if "stock" in a["name"].lower())
    bond_pct = sum(a["weight"] for a in assets if "bond" in a["name"].lower())
    if stock_pct >= 50:
        return "EQUITY_PUBLIC"
    if bond_pct >= 50:
        return "FIXED_INCOME"
    return "EQUITY"


def asset_isin(asset: dict) -> str | None:
    """ISIN for a Wealthfolio asset: the symbol when it is an ISIN (custom-provider
    assets are keyed by ISIN), else an `isin` identifier in the asset metadata."""
    symbol = (asset.get("instrumentSymbol") or "").upper()
    if ISIN_RE.match(symbol):
        return symbol
    meta = asset.get("metadata") or {}
    for container in (meta, meta.get("identifiers") or {}):
        value = container.get("isin") if isinstance(container, dict) else None
        if isinstance(value, str) and ISIN_RE.match(value.upper()):
            return value.upper()
    return None


def uses_custom_provider(asset: dict) -> bool:
    return "CUSTOM" in str(asset.get("providerConfig") or "").upper()


# ───────────────────────────────────────────────────────────────────────────
# Wealthfolio REST client
# ───────────────────────────────────────────────────────────────────────────

class Wealthfolio:
    def __init__(self, base_url: str):
        self.api = f"{base_url}/api/v1"
        self.http = requests.Session()
        self.categories: dict[str, set[str]] = {}

    def login(self, password: str = "", token: str = "") -> None:
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"
            return
        status = self.http.get(f"{self.api}/auth/status", timeout=15)
        status.raise_for_status()
        if not status.json().get("requiresPassword"):
            return
        if not password:
            sys.exit("Wealthfolio requires a password: set WF_PASSWORD (or WF_TOKEN).")
        # The session cookie set by /auth/login authenticates later calls.
        resp = self.http.post(f"{self.api}/auth/login", json={"password": password}, timeout=15)
        if resp.status_code != 200:
            sys.exit(f"Wealthfolio login failed: HTTP {resp.status_code} {resp.text[:200]}")

    def _check(self, resp: requests.Response) -> requests.Response:
        if not resp.ok:
            raise RuntimeError(f"{resp.request.method} {resp.url} → {resp.status_code}: {resp.text[:300]}")
        return resp

    def assets(self) -> list[dict]:
        return self._check(self.http.get(f"{self.api}/assets", timeout=30)).json()

    def category_ids(self, taxonomy_id: str) -> set[str]:
        if taxonomy_id not in self.categories:
            data = self._check(self.http.get(f"{self.api}/taxonomies/{taxonomy_id}", timeout=30)).json()
            if not data:
                raise RuntimeError(f"Taxonomy '{taxonomy_id}' not found in Wealthfolio")
            self.categories[taxonomy_id] = {c["id"] for c in data.get("categories", [])}
        return self.categories[taxonomy_id]

    def ensure_category(self, taxonomy_id: str, cat_id: str, name: str) -> None:
        if cat_id in self.category_ids(taxonomy_id):
            return
        self._check(self.http.post(
            f"{self.api}/taxonomies/categories",
            json={
                "id": cat_id,
                "taxonomyId": taxonomy_id,
                "parentId": None,
                "name": name,
                "key": cat_id.lower(),
                "color": "#808080",
                "description": "Created by StocksBot taxonomy sync",
                "sortOrder": 999,
            },
            timeout=30,
        ))
        self.categories[taxonomy_id].add(cat_id)
        print(f"  + Created category [{taxonomy_id}] {cat_id} = {name!r}")

    def assignments(self, asset_id: str) -> list[dict]:
        return self._check(self.http.get(
            f"{self.api}/taxonomies/assignments/asset/{asset_id}", timeout=30)).json()

    def replace_assignments(self, asset_id: str, taxonomy_id: str, weights: dict[str, int]) -> None:
        payload = [
            {"assetId": asset_id, "taxonomyId": taxonomy_id, "categoryId": cat,
             "weight": weight, "source": SOURCE}
            for cat, weight in weights.items()
        ]
        self._check(self.http.put(
            f"{self.api}/taxonomies/assignments/asset/{asset_id}/taxonomy/{taxonomy_id}",
            json=payload, timeout=30,
        ))


# ───────────────────────────────────────────────────────────────────────────
# Sync
# ───────────────────────────────────────────────────────────────────────────

def sync_weighted(wf: Wealthfolio, asset_id: str, taxonomy_id: str,
                  weights: "OrderedDict[str, int]", labels: dict[str, str]) -> None:
    """Replace this tool's assignments for a weighted taxonomy, keeping any the user
    added by hand (other sources) for categories we don't set, as long as the total
    stays within 100%."""
    for cat in weights:
        if cat.startswith("FT_"):
            wf.ensure_category(taxonomy_id, cat, labels.get(cat, cat))
    keep = OrderedDict()
    budget = 10000 - sum(weights.values())
    for a in wf.assignments(asset_id):
        if a["taxonomyId"] != taxonomy_id or a.get("source") == SOURCE or a["categoryId"] in weights:
            continue
        if a["weight"] <= budget:
            keep[a["categoryId"]] = a["weight"]
            budget -= a["weight"]
    wf.replace_assignments(asset_id, taxonomy_id, OrderedDict(list(weights.items()) + list(keep.items())))


def process_asset(wf: Wealthfolio, asset: dict, isin: str) -> None:
    currency = asset.get("quoteCcy") or "EUR"
    name = asset.get("name") or isin
    print(f"\n{'─'*60}")
    print(f"  {name} ({isin}) [{currency}]")

    try:
        resp = requests.get(f"{PRICE_BOT_URL}/info", params={"isin": isin, "currency": currency}, timeout=120)
    except requests.RequestException as e:
        print(f"  ✗ Request failed: {e}")
        return
    if resp.status_code == 404:
        print("  ✗ Not found on FT")
        return
    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:120]}")
        return

    info = resp.json()
    print(f"  asset_type={info.get('asset_type')}  fund_type={info.get('fund_type')}")
    print(f"  sectors={len(info.get('sectors', []))}  regions={len(info.get('regions', []))}")

    instr_id = FT_FUND_TYPE_TO_INSTRUMENT.get(info.get("fund_type"))
    class_id = asset_class_for(info)
    sectors = weighted_assignments(info.get("sectors", []), FT_SECTOR_TO_GICS)
    regions = weighted_assignments(filter_macro_regions(info.get("regions", [])), FT_REGION_TO_ID,
                                   strip_parens=True)
    for cat, bp in sectors.items():
        print(f"    sector  {cat}  ({bp / 100:.2f}%)")
    for cat, bp in regions.items():
        print(f"    region  {cat}  ({bp / 100:.2f}%)")

    if DRY_RUN:
        return

    asset_id = asset["id"]
    # Single-select taxonomies: exactly one category at 100%.
    for taxonomy_id, cat in (("instrument_type", instr_id), ("asset_classes", class_id)):
        if not cat:
            continue
        if cat in wf.category_ids(taxonomy_id):
            wf.replace_assignments(asset_id, taxonomy_id, {cat: 10000})
        else:
            print(f"  ! [{taxonomy_id}] has no category {cat}; skipped")

    if sectors:
        labels = {fallback_category_id(s["name"]): s["name"] for s in info.get("sectors", [])}
        sync_weighted(wf, asset_id, "industries_gics", sectors, labels)
    if regions:
        labels = {fallback_category_id(r["name"], True): r["name"] for r in info.get("regions", [])}
        sync_weighted(wf, asset_id, "regions", regions, labels)


def main() -> None:
    wf = Wealthfolio(WF_URL)
    wf.login(WF_PASSWORD, WF_TOKEN)

    candidates = [(a, asset_isin(a)) for a in wf.assets() if a.get("isActive", True)]
    candidates = [(a, isin) for a, isin in candidates if isin]
    custom = [(a, isin) for a, isin in candidates if uses_custom_provider(a)]
    if custom:
        print(f"Found {len(custom)} active assets with a custom provider\n")
        selected = custom
    else:
        print(f"No custom-provider assets; falling back to all {len(candidates)} active assets with an ISIN\n")
        selected = candidates
    selected.sort(key=lambda pair: (pair[0].get("name") or ""))

    ok = fail = 0
    for asset, isin in selected:
        try:
            process_asset(wf, asset, isin)
            ok += 1
        except Exception as e:  # keep going; report at the end
            print(f"  ✗ Error processing {isin}: {e}")
            fail += 1

    print(f"\n{'='*60}")
    print(f"Done — {ok} assets synced, {fail} failed")
    if DRY_RUN:
        print("(DRY RUN — no changes written)")


if __name__ == "__main__":
    main()
