"""Proshop must not leave an unbuyable listing as a current price.

Measured 2026-09-06: HP 669324-B21 was published at 1,015 PLN while not
purchasable, and that amount was reused 8 times as a cross-shop reference.
The scanner wrote the current price before checking purchasability.

Outlet and retail are separate lanes for the same product, so retiring one
must never disturb the other.
"""
from __future__ import annotations

import sqlite3

import pytest

from proshop import NEW_SOURCE, OUTLET_SOURCE, SELLER
from proshop.scanner import source_for


LEDGER_KEY = "code:669324-B21"


def current_rows(db_url: str) -> list[tuple]:
    conn = sqlite3.connect(db_url)
    try:
        return conn.execute(
            "SELECT klucz, sprzedawca, zrodlo, cena FROM ceny_biezace ORDER BY zrodlo"
        ).fetchall()
    finally:
        conn.close()


def history_rows(db_url: str) -> list[tuple]:
    conn = sqlite3.connect(db_url)
    try:
        return conn.execute("SELECT klucz, cena FROM historia_cen").fetchall()
    finally:
        conn.close()


@pytest.fixture()
def seeded(tmp_path):
    """Both lanes hold a current price for the same product."""
    from deal_pipeline.database import Database

    db_url = str(tmp_path / "products.db")
    database = Database(url=db_url)
    database.prices.save_record(
        LEDGER_KEY, SELLER, NEW_SOURCE, 1015.0,
        name="HP 669324-B21", url="https://www.proshop.pl/new",
    )
    database.prices.save_record(
        LEDGER_KEY, SELLER, OUTLET_SOURCE, 700.0,
        name="HP 669324-B21", url="https://www.proshop.pl/outlet",
    )
    database.connection.commit()
    database.close()
    return db_url


def test_retiring_the_retail_lane_leaves_the_outlet_lane(seeded):
    """The exact contract the scanner relies on: retire only this source."""
    from deal_pipeline.database import Database

    database = Database(url=seeded)
    try:
        assert database.prices.retire_current(
            LEDGER_KEY, SELLER, source_for(False),
            name="HP 669324-B21", url="https://www.proshop.pl/new",
        ) is True
    finally:
        database.close()

    survivors = current_rows(seeded)
    assert [row[2] for row in survivors] == [OUTLET_SOURCE]
    assert history_rows(seeded), "history is evidence and must survive"


def test_retiring_the_outlet_lane_leaves_the_retail_lane(seeded):
    from deal_pipeline.database import Database

    database = Database(url=seeded)
    try:
        assert database.prices.retire_current(
            LEDGER_KEY, SELLER, source_for(True),
            name="HP 669324-B21", url="https://www.proshop.pl/outlet",
        ) is True
    finally:
        database.close()

    survivors = current_rows(seeded)
    assert [row[2] for row in survivors] == [NEW_SOURCE]


def test_source_for_separates_the_two_lanes():
    assert source_for(True) == OUTLET_SOURCE
    assert source_for(False) == NEW_SOURCE
    assert source_for(True) != source_for(False)
