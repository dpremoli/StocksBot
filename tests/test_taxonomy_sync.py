"""Unit tests for the pure helpers in sync_wealthfolio_taxonomies (no network)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_wealthfolio_taxonomies as sync  # noqa: E402


def test_fallback_ids_match_previous_versions():
    assert sync.fallback_category_id("Asia - Emerging") == "FT_ASIA___EMERGING"
    assert sync.fallback_category_id("Other (Unclassified)", strip_parens=True) == "FT_OTHER_UNCLASSIFIED"


def test_weights_merge_when_labels_share_a_category():
    rows = [{"name": "Eurozone", "weight": 10.0}, {"name": "Western Europe", "weight": 5.5}]
    assert sync.weighted_assignments(rows, sync.FT_REGION_TO_ID) == {"R1020": 1550}


def test_weights_drop_non_positive_and_scale_overshoot():
    rows = [
        {"name": "Technology", "weight": 60.4},
        {"name": "Financial Services", "weight": 40.3},
        {"name": "Energy", "weight": -0.5},
    ]
    out = sync.weighted_assignments(rows, sync.FT_SECTOR_TO_GICS)
    assert set(out) == {"4510", "402010"}
    assert sum(out.values()) == 10000


def test_unmapped_labels_get_fallback_ids():
    out = sync.weighted_assignments([{"name": "Asia - Emerging Plus", "weight": 3}], {})
    assert out == {"FT_ASIA___EMERGING_PLUS": 300}


def test_macro_regions_dropped_when_they_restate_a_child():
    rows = [{"name": "Americas", "weight": 60.0}, {"name": "United States", "weight": 59.8}]
    assert [r["name"] for r in sync.filter_macro_regions(rows)] == ["United States"]


def test_asset_isin_from_symbol_or_metadata():
    assert sync.asset_isin({"instrumentSymbol": "lu1022659046"}) == "LU1022659046"
    assert sync.asset_isin({"instrumentSymbol": "VWRL", "metadata": {"identifiers": {"isin": "IE00B3RBWM25"}}}) == "IE00B3RBWM25"
    assert sync.asset_isin({"instrumentSymbol": "AAPL"}) is None


def test_asset_class_mapping_and_breakdown_fallback():
    assert sync.asset_class_for({"asset_type": "Equity"}) == "EQUITY"
    assert sync.asset_class_for({"asset_type": "Other", "assets": [{"name": "Bonds", "weight": 70}]}) == "FIXED_INCOME"
    assert sync.asset_class_for({}) is None


class FakeWealthfolio:
    def __init__(self, existing):
        self.existing = existing
        self.created = []
        self.replaced = None

    def ensure_category(self, taxonomy_id, cat_id, name):
        self.created.append((taxonomy_id, cat_id, name))

    def assignments(self, asset_id):
        return self.existing

    def replace_assignments(self, asset_id, taxonomy_id, weights):
        self.replaced = (taxonomy_id, dict(weights))


def test_sync_weighted_replaces_own_rows_and_keeps_manual_ones_within_budget():
    wf = FakeWealthfolio([
        {"taxonomyId": "regions", "categoryId": "R10", "weight": 2000, "source": "auto_scraper"},
        {"taxonomyId": "regions", "categoryId": "country_JP", "weight": 500, "source": "manual"},
        {"taxonomyId": "regions", "categoryId": "country_FR", "weight": 4000, "source": "manual"},
        {"taxonomyId": "industries_gics", "categoryId": "4510", "weight": 9000, "source": "manual"},
    ])
    weights = {"country_US": 7000, "FT_ATLANTIS": 1000}
    sync.sync_weighted(wf, "a1", "regions", weights, {"FT_ATLANTIS": "Atlantis"})
    assert wf.created == [("regions", "FT_ATLANTIS", "Atlantis")]
    # The stale auto row is dropped, JP fits in the remaining 20%, FR doesn't.
    assert wf.replaced == ("regions", {"country_US": 7000, "FT_ATLANTIS": 1000, "country_JP": 500})
