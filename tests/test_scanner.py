"""Scanner policy and shared-ledger integration tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from deal_pipeline.events import product_key
from deal_pipeline.catalog import Catalog, Page
from deal_pipeline.own_reference import reference_from_ledger
from deal_pipeline.price_ledger import PriceLedger
from proshop import NEW_SOURCE, OUTLET_SOURCE, SELLER
from proshop.scanner import (
    LISTING_STORE,
    condition_for,
    listing_page_urls,
    prune_listing_pages,
    should_refresh,
    should_refresh_catalog,
    source_for,
    sync_listing_page_range,
)


def test_listing_page_urls_are_canonical_and_bounded():
    assert listing_page_urls("https://www.proshop.pl/RAM", 3) == [
        "https://www.proshop.pl/RAM",
        "https://www.proshop.pl/RAM?pn=2",
        "https://www.proshop.pl/RAM?pn=3",
    ]
    assert listing_page_urls("https://www.proshop.pl/RAM?pn=9", 2) == [
        "https://www.proshop.pl/RAM",
        "https://www.proshop.pl/RAM?pn=2",
    ]
    assert listing_page_urls("https://www.proshop.pl/RAM", 999, cap=400)[-1].endswith("pn=400")


def test_prune_listing_pages_removes_only_pages_beyond_the_live_range(tmp_path):
    connection = sqlite3.connect(tmp_path / "products.db")
    connection.row_factory = sqlite3.Row
    catalog = Catalog(connection, LISTING_STORE)
    gpu = "https://www.proshop.pl/Karta-graficzna"
    ram = "https://www.proshop.pl/RAM"
    catalog.append([
        gpu,
        f"{gpu}?pn=2",
        f"{gpu}?pn=15",
        f"{gpu}?pn=16",
        f"{gpu}?pn=17",
        f"{ram}?pn=54",
    ])

    removed = prune_listing_pages(connection, gpu + "?pn=5", 15)
    remaining = {
        row[0]
        for row in connection.execute(
            "SELECT url FROM strony WHERE sklep = ?", (LISTING_STORE,)
        )
    }

    assert removed == 2
    assert gpu in remaining
    assert f"{gpu}?pn=15" in remaining
    assert f"{gpu}?pn=16" not in remaining
    assert f"{gpu}?pn=17" not in remaining
    assert f"{ram}?pn=54" in remaining, "pruning GPU touched RAM"


def test_sync_does_not_resurrect_the_stale_page_being_scanned(tmp_path):
    """Saving the current record must happen before the final stale-tail prune."""
    connection = sqlite3.connect(tmp_path / "products.db")
    connection.row_factory = sqlite3.Row
    catalog = Catalog(connection, LISTING_STORE)
    gpu = "https://www.proshop.pl/Karta-graficzna"
    stale = f"{gpu}?pn=16"
    catalog.append([gpu, stale, f"{gpu}?pn=17"])

    added, removed = sync_listing_page_range(
        catalog,
        connection,
        Page(url=stale, name="Karta graficzna", price=None),
        page_count=15,
        cap=400,
    )
    remaining = {
        row[0]
        for row in connection.execute(
            "SELECT url FROM strony WHERE sklep = ?", (LISTING_STORE,)
        )
    }

    assert removed == 2
    assert added == 14
    assert stale not in remaining, "the current stale page was saved back after pruning"


def test_catalog_refresh_policy_and_dry_run_guard():
    now = datetime(2026, 9, 2, 12, 0)
    assert should_refresh(None, now=now)
    assert should_refresh("broken", now=now)
    assert not should_refresh((now - timedelta(hours=1)).isoformat(), now=now)
    assert should_refresh((now - timedelta(hours=25)).isoformat(), now=now)
    assert not should_refresh_catalog(None, dry_run=True)
    assert should_refresh_catalog(None, dry_run=False)


def test_demo_and_retail_sources_never_share_reference_lanes(tmp_path):
    connection = sqlite3.connect(tmp_path / "products.db")
    connection.row_factory = sqlite3.Row
    ledger = PriceLedger(connection)
    name = "GIGABYTE GeForce RTX 5070 WindForce 3 OC 12GB"
    key = product_key(name, "GIGABYTE", "GV-N5070WF3OC-12GD")
    ledger.save_record(key, SELLER, NEW_SOURCE, 3299.0, name=name, url="https://www.proshop.pl/new")
    ledger.save_record(key, SELLER, OUTLET_SOURCE, 2499.0, name=name, url="https://www.proshop.pl/demo")

    new_ref = reference_from_ledger(connection, key=key, name=name, seller="x-kom", price=4000.0)
    used_ref = reference_from_ledger(connection, key=key, name=name, seller="olx", price=4000.0, condition="used")
    assert new_ref is not None and new_ref.price == 3299.0 and new_ref.condition == "new"
    assert used_ref is not None and used_ref.price == 2499.0 and used_ref.condition == "used"
    connection.close()


def test_source_and_condition_come_from_the_listing_not_a_global_default():
    assert source_for(False) == NEW_SOURCE
    assert condition_for(False) == "new"
    assert source_for(True) == OUTLET_SOURCE
    assert condition_for(True) == "used"
