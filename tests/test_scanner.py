"""Scanner policy and shared-ledger integration tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from deal_pipeline.events import product_key
from deal_pipeline.own_reference import reference_from_ledger
from deal_pipeline.price_ledger import PriceLedger
from proshop import NEW_SOURCE, OUTLET_SOURCE, SELLER
from proshop.scanner import (
    condition_for,
    listing_page_urls,
    should_refresh,
    should_refresh_catalog,
    source_for,
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
