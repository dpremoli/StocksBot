# StocksBot — FT NAV Price Bot

A self-hosted microservice that scrapes ETF and mutual-fund NAV prices from
[Financial Times Markets](https://markets.ft.com) by ISIN and exposes them
over a simple JSON REST API. Designed to integrate with
[Wealthfolio](https://wealthfolio.app) as a **Custom Market Data Provider**.

---

## Contents

- [How It Works](#how-it-works)
- [Deployment](#deployment)
  - [Docker Compose (recommended)](#docker-compose-recommended)
  - [Building from source](#building-from-source)
  - [Pre-built image (GHCR)](#pre-built-image-ghcr)
- [API Reference](#api-reference)
- [Connecting to Wealthfolio](#connecting-to-wealthfolio)
  - [Step 1 — Add a Custom Provider](#step-1--add-a-custom-provider)
  - [Step 2 — Configure the URL and price path](#step-2--configure-the-url-and-price-path)
  - [Step 3 — Assign the provider to an asset](#step-3--assign-the-provider-to-an-asset)
- [Syncing fund classifications into Wealthfolio](#syncing-fund-classifications-into-wealthfolio)
- [Caching](#caching)
- [Troubleshooting](#troubleshooting)

---

## How It Works

StocksBot launches a headless Chromium browser (via Playwright) and navigates
to the FT tearsheet page for the requested ISIN. It extracts the NAV/price,
converts pence-quoted instruments (GBp / GBX) to GBP automatically, and
returns the value as JSON.

Results are cached in a local SQLite database for the rest of the calendar day
so that repeated calls to Wealthfolio's sync cycle do not trigger additional
scrapes.

---

## Deployment

### Docker Compose (recommended)

```yaml
# docker-compose.yml
version: "3.9"

services:
  price-bot:
    container_name: price-bot
    image: ghcr.io/dpremoli/price-bot:latest
    restart: unless-stopped
    ports:
      - "8150:8000"       # host:container — change the host port if needed
    volumes:
      - /mnt/user/appdata/price-bot:/data   # SQLite cache lives here
    environment:
      - TZ=Europe/London
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 20s
    deploy:
      resources:
        limits:
          memory: 1G
```

```bash
docker compose up -d
```

The service is reachable at `http://localhost:8150` (or the host IP / hostname
if Wealthfolio runs on a different machine).

#### Volume

The container stores its SQLite cache in `/data`. Mount a persistent host path
so the cache survives container restarts.

| Host path | Container path | Purpose |
|---|---|---|
| `/mnt/user/appdata/price-bot` | `/data` | SQLite price cache |

### Building from source

```bash
git clone https://github.com/dpremoli/stocksbot.git
cd stocksbot
docker build -t price-bot .
docker run -d \
  --name price-bot \
  -p 8150:8000 \
  -v "$(pwd)/data:/data" \
  price-bot
```

### Pre-built image (GHCR)

A Docker image is published automatically on every push to `main` via GitHub
Actions:

```
ghcr.io/dpremoli/price-bot:latest
```

---

## API Reference

All endpoints accept and return JSON. The base URL in examples below uses
`http://localhost:8150`; replace with your actual host and port.

---

### `GET /health`

Liveness check.

**Response**
```json
{ "status": "ok" }
```

---

### `GET /price`

Fetch the current NAV/price for a single instrument.

**Query parameters**

| Parameter | Required | Default | Description |
|---|---|---|---|
| `isin` | Yes | — | 12-character ISIN |
| `currency` | No | `EUR` | ISO 4217 currency code |

**Example**
```bash
curl "http://localhost:8150/price?isin=LU1022659046&currency=EUR"
```

**Success response** `200 OK`
```json
{ "price": 11.42 }
```

**Error responses**

| Status | Meaning |
|---|---|
| `400` | Invalid ISIN length |
| `404` | ISIN not found on FT |
| `503` | FT returned 403 (rate-limited / blocked) |
| `500` | Unexpected scraping error |

---

### `POST /batch`

Fetch prices for multiple ISINs in one call. A 1.5 s polite delay is inserted
between requests to avoid FT rate-limiting.

**Request body**
```json
{
  "isins": ["LU1022659046", "IE00B4L5Y983"],
  "currency": "EUR"
}
```

**Success response** `200 OK`
```json
{
  "prices": {
    "LU1022659046": 11.42,
    "IE00B4L5Y983": 35.80
  },
  "errors": {
    "XX0000000000": "not_found"
  }
}
```

Error values: `"not_found"`, `"blocked"`, or `"error: <message>"`.

---

### `GET /info`

Scrape fund metadata: name, asset type, fund type, sector breakdown, regional
breakdown, and asset-class allocation from the FT holdings tab.

**Query parameters**

| Parameter | Required | Default | Description |
|---|---|---|---|
| `isin` | Yes | — | 12-character ISIN |
| `currency` | No | `EUR` | ISO 4217 currency code |

**Example**
```bash
curl "http://localhost:8150/info?isin=LU1022659046&currency=EUR"
```

**Success response** `200 OK`
```json
{
  "isin": "LU1022659046",
  "name": "Xtrackers MSCI World Swap UCITS ETF 1C",
  "asset_type": "Equity",
  "fund_type": "ETF",
  "assets": [
    { "name": "Equities", "weight": 99.8 }
  ],
  "sectors": [
    { "name": "Technology", "weight": 24.3 },
    { "name": "Financials", "weight": 15.1 }
  ],
  "regions": [
    { "name": "North America", "weight": 70.5 },
    { "name": "Europe", "weight": 15.2 }
  ]
}
```

---

### `POST /batch-info`

Fetch metadata for multiple ISINs. Same 1.5 s inter-request delay as `/batch`.

**Request body**
```json
{
  "isins": ["LU1022659046", "IE00B4L5Y983"],
  "currency": "EUR"
}
```

**Success response** `200 OK`
```json
{
  "info": {
    "LU1022659046": { ... },
    "IE00B4L5Y983": { ... }
  },
  "errors": {}
}
```

---

## Connecting to Wealthfolio

Wealthfolio (v3.1+, placeholders below need v3.9) supports **Custom Market Data Providers** — you supply a
URL template and a JSONPath expression; Wealthfolio substitutes the asset
symbol, fetches the URL, and extracts the price. StocksBot's `/price` endpoint
is designed to satisfy this contract directly.

> **Requirement:** Wealthfolio must be able to reach the StocksBot host over
> the network. If both run on the same machine, `http://localhost:8150` works.
> If Wealthfolio runs as a Docker container, use the host's LAN IP or Docker
> network alias instead.

---

### Step 1 — Add a Custom Provider

1. Open Wealthfolio and go to **Settings → Market Data**.
2. Scroll to **Custom Providers** and click **Add Provider**.
3. Give the provider a name, e.g. `FT Price Bot`.

---

### Step 2 — Configure the URL and price path

Fill in the provider form with the following values:

| Field | Value |
|---|---|
| **Source type** | `JSON` |
| **URL** | `http://<host>:8150/price?isin={ISIN}&currency={CURRENCY}` |
| **Price path** | `$.price` |

Replace `<host>` with the actual hostname or IP where StocksBot is running
(e.g. `192.168.1.50`, `price-bot`, or `localhost`).

**Notes**

- `{ISIN}` and `{CURRENCY}` are filled in by Wealthfolio from the asset at sync
  time (its ISIN identifier and quote currency), so the asset can keep its normal
  ticker.
- On older Wealthfolio versions, or for an asset without a stored ISIN, use
  `isin={SYMBOL}&currency=EUR` instead and set that asset's symbol to its ISIN
  (see [Step 3](#step-3--assign-the-provider-to-an-asset)).
- The response `{ "price": 11.42 }` is parsed by the JSONPath expression
  `$.price`, which selects the top-level `price` field.

**Test the provider**

Click **Test** (or **Fetch**) inside Wealthfolio's provider editor. Enter a
valid ISIN as the test symbol (e.g. `LU1022659046`). Wealthfolio will call the
URL and display the raw JSON response in the preview pane. You can click the
numeric price value in the preview to auto-populate the **Price path** field.

You can also verify manually:

```bash
curl "http://<host>:8150/price?isin=LU1022659046&currency=EUR"
# Expected: {"price": 11.42}
```

---

### Step 3 — Assign the provider to an asset

For each ETF or fund you want to price with StocksBot:

1. Open the asset in Wealthfolio (**Assets** or from a portfolio holding).
2. Under **Market Data**, set **Preferred Provider** to `FT Price Bot` (the
   name you gave in Step 1).
3. Make sure the asset has its ISIN recorded (it is substituted into `{ISIN}`).
   If you used the `{SYMBOL}` form of the URL, set the asset's **Symbol** to the
   ISIN instead (e.g. `LU1022659046`).
4. Save.

Wealthfolio will now use StocksBot to fetch the latest price for this asset
during each sync cycle.

---

## Syncing fund classifications into Wealthfolio

`sync_wealthfolio_taxonomies.py` pulls `/info` for each fund and writes its
sector, region, asset-class and instrument-type breakdown into Wealthfolio's
classification taxonomies, so allocation charts look *through* the fund.
Wealthfolio fills sectors/asset class for ETFs that Yahoo covers, but not
regions or funds Yahoo doesn't know, which is what this adds.

It talks to the Wealthfolio server's REST API (the self-hosted web/Docker
build), so Wealthfolio can keep running and every write goes through the app's
own validation. Earlier versions edited the SQLite file directly; don't use
those against Wealthfolio 3.8+.

```bash
pip install requests
WF_URL=http://<wealthfolio-host>:8088 \
WF_PASSWORD='<your Wealthfolio password>' \
PRICE_BOT_URL=http://<host>:8150 \
python3 sync_wealthfolio_taxonomies.py      # add DRY_RUN=1 to preview
```

| Variable | Default | Meaning |
|---|---|---|
| `WF_URL` | `http://localhost:8088` | Wealthfolio server base URL |
| `WF_PASSWORD` | — | Login password, when Wealthfolio auth is enabled |
| `WF_TOKEN` | — | Alternatively, a bearer token for the API |
| `PRICE_BOT_URL` | `http://localhost:8150` | StocksBot base URL |
| `DRY_RUN` | off | `1`/`true` fetches and prints without writing |

Behaviour:

- Assets are those with a custom market-data provider (falling back to every
  active asset with an ISIN).
- Instrument type and asset class are single-choice and are set outright.
- Sector and region weights replace the rows this script wrote before
  (`source = auto_scraper`). Classifications you added by hand are kept when
  they still fit within 100%.
- FT labels that map to the same category are summed, and totals that FT's
  rounding pushes over 100% are scaled down (Wealthfolio rejects them).
- Unmapped FT labels get their own `FT_…` category, using the same ids as
  earlier versions so nothing is duplicated.

Tests: `pytest tests/test_taxonomy_sync.py`.

---

## Caching

Prices are cached in SQLite keyed by `(ISIN, currency, date)`. The cache is
valid for the current UTC calendar day — the first request on any given day
triggers a live scrape; subsequent requests return the cached value.

Fund info (`/info`, `/batch-info`) is cached with the same daily key.

The cache file is stored at `/data/prices.db` inside the container. To clear
the cache, stop the container and delete the file (or the entire `/data`
directory), then restart.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `503` from `/price` | FT blocked the scraper | Wait a few minutes and retry; persistent blocks may require running behind a residential proxy |
| `404` from `/price` | ISIN not listed on FT Markets | Verify the ISIN on `markets.ft.com` manually |
| Wealthfolio shows "Provider error" | URL unreachable from Wealthfolio | Check network connectivity; use LAN IP instead of `localhost` if both are in Docker |
| Stale price in Wealthfolio | Cache not yet expired | Cache resets at midnight UTC; use `/price` directly to confirm |
| Container OOM | Chromium memory spike | Increase the `memory` limit in `docker-compose.yml` (default 1 GB) |
| `start_period` health-check fails | Slow Chromium init | Increase `start_period` to `40s` on low-spec hardware |
