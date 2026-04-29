"""
Merge multiple Trading 212 CSV exports into a single normalised file.

- Normalises all files to the newest column format
- Converts GBX prices to GBP (divides Price / share by 100, sets currency to GBP)
- Removes duplicate rows (same ID)
- Sorts by date ascending

Output: trading212_merged.csv
"""

import csv
import os
from pathlib import Path

INPUT_FILES = [
    "ss_ISA.csv",
    "from_2024-04-01_to_2025-04-01_MTc3NzM5NjkxMDE0Nw.csv",
    "from_2025-04-01_to_2026-04-01_MTc3NzM5Njk0OTM4OQ.csv",
    "from_2026-04-01_to_2026-04-28_MTc3NzM5NzAzNzUwNA.csv",
]

OUTPUT_FILE = "trading212_merged.csv"

# Canonical output columns (newest format)
OUT_COLS = [
    "Action",
    "Time",
    "ISIN",
    "Ticker",
    "Name",
    "Notes",
    "ID",
    "No. of shares",
    "Price / share",
    "Currency (Price / share)",
    "Exchange rate",
    "Result",
    "Currency (Result)",
    "Total",
    "Currency (Total)",
    "Withholding tax",
    "Currency (Withholding tax)",
    "Stamp duty reserve tax",
    "Currency (Stamp duty reserve tax)",
    "Currency conversion fee",
    "Currency (Currency conversion fee)",
]

# Map old column names to canonical names
COL_ALIASES = {
    # old format had no Result columns — they just don't exist
}


def normalise_row(row: dict) -> dict:
    out = {col: "" for col in OUT_COLS}
    for col in OUT_COLS:
        if col in row:
            out[col] = row[col]
    return out


def fix_gbx(row: dict) -> dict:
    if row.get("Currency (Price / share)", "").strip() in ("GBX", "GBp"):
        price_raw = row.get("Price / share", "").strip()
        try:
            price = float(price_raw) / 100
            row["Price / share"] = f"{price:.10f}".rstrip("0").rstrip(".")
        except ValueError:
            pass
        row["Currency (Price / share)"] = "GBP"
        # Exchange rate was 100 to convert pence→pounds; now price is in GBP
        row["Exchange rate"] = "1.00000000"
    return row


def main():
    base = Path(__file__).parent
    seen_ids = set()
    all_rows = []

    for fname in INPUT_FILES:
        fpath = base / fname
        if not fpath.exists():
            print(f"  SKIP (not found): {fname}")
            continue

        with open(fpath, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            count = 0
            dupes = 0
            for row in reader:
                row_id = row.get("ID", "").strip()
                if row_id and row_id in seen_ids:
                    dupes += 1
                    continue
                if row_id:
                    seen_ids.add(row_id)
                row = fix_gbx(row)
                row = normalise_row(row)
                all_rows.append(row)
                count += 1
            print(f"  {fname}: {count} rows ({dupes} dupes skipped)")

    # Sort by Time ascending
    all_rows.sort(key=lambda r: r.get("Time", ""))

    out_path = base / OUTPUT_FILE
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLS)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nDone — {len(all_rows)} rows written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
