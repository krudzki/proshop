"""Listing-page queue tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from deal_pipeline.catalog import Catalog
from proshop.queue import QueuedPage, due_for_pass, order_pages


def _page(path: str, checked_at: datetime | None) -> QueuedPage:
    return QueuedPage(
        url=f"https://www.proshop.pl/{path}",
        key="",
        name=path.split("?")[0],
        price=None,
        checked_at=checked_at,
    )


def test_focus_share_is_reserved_without_starving_general_pages():
    now = datetime(2026, 9, 2, 12, 0)
    pages = [
        *[_page(f"RAM?pn={i}", now) for i in range(1, 7)],
        *[_page(f"Karta-graficzna?pn={i}", now) for i in range(1, 7)],
        *[_page(f"Telefon-komorkowy?pn={i}", now - timedelta(days=2)) for i in range(1, 15)],
    ]
    chosen = order_pages(pages, limit=10, focus_share=0.5)
    assert len(chosen) == 10
    assert sum(page.focus for page in chosen) == 5
    assert any(not page.focus for page in chosen)


def test_first_lap_prefix_that_hides_focus_is_widened(tmp_path):
    connection = sqlite3.connect(tmp_path / "products.db")
    connection.row_factory = sqlite3.Row
    catalog = Catalog(connection, "proshop-listings")
    for index in range(900):
        url = f"https://www.proshop.pl/Kabel-USB?pn={index + 1}"
        catalog.append([url])
        connection.execute("UPDATE strony SET nazwa = ? WHERE sklep = ? AND url = ?", ("Kabel USB", "proshop-listings", url))
    for index in range(20):
        url = f"https://www.proshop.pl/RAM?pn={index + 1}"
        catalog.append([url])
        connection.execute("UPDATE strony SET nazwa = ? WHERE sklep = ? AND url = ?", ("RAM", "proshop-listings", url))
    connection.commit()

    chosen = due_for_pass(connection, "proshop-listings", 20, 0.5)
    assert sum(page.focus for page in chosen) == 10
    connection.close()


def test_zero_focus_share_restores_staleness_order():
    now = datetime(2026, 9, 2, 12, 0)
    pages = [_page("RAM", now), _page("Telefon-komorkowy", now - timedelta(days=2))]
    assert order_pages(pages, 1, 0.0)[0].focus is False
