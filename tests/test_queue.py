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


def test_never_checked_pages_are_not_starved_by_fresh_priority_pages(tmp_path):
    """A page nobody has ever opened must reach a pass eventually.

    Production shape, measured 2026-09-22 against the live catalogue: 122
    category roots (Activity Trackers through Bateria) sat at
    `sprawdzone IS NULL` for the whole 24-hour journal while every pass spent
    all 40 slots on already-checked pages.

    The mechanism needs all three of these together, which is why a smaller
    fixture passes while production starves:

      1. the staleness window (160 rows) is filled entirely by never-checked
         NON-focus pages, so it contains zero focus pages;
      2. that trips the widening guard, which re-reads the whole table;
      3. `order_pages` then fills the focus quota and sorts the remainder by
         PRIORITY before staleness, so every never-checked P2/P3 root falls
         below the limit.

    Measured on the live table: with the narrow window the pass would carry
    40 never-checked pages; after widening it carries 0.
    """
    connection = sqlite3.connect(tmp_path / "products.db")
    connection.row_factory = sqlite3.Row
    catalog = Catalog(connection, "proshop-listings")
    checked = datetime(2026, 9, 22, 12, 0).isoformat()

    # Enough never-checked non-focus roots to fill the 160-row window on
    # their own -- this is what makes the window focus-free. They are P2/P3
    # because that is what an unvisited category root scores.
    never_checked = []
    for index in range(200):
        url = f"https://www.proshop.pl/Antena-RTV-{index}"
        never_checked.append(url)
        catalog.append([url])
        connection.execute(
            "UPDATE strony SET nazwa = ? WHERE sklep = ? AND url = ?",
            ("Antena RTV", "proshop-listings", url),
        )
    # The live catalogue holds 1113 already-checked P1 pages that are NOT
    # focus. These are what actually consume every non-quota slot.
    for index in range(300):
        url = f"https://www.proshop.pl/Telefon-komorkowy?pn={index + 1}"
        catalog.append([url])
        connection.execute(
            "UPDATE strony SET nazwa = ?, sprawdzone = ? WHERE sklep = ? AND url = ?",
            ("Telefon komórkowy", checked, "proshop-listings", url),
        )
    # Fresh focus pages, enough to satisfy the quota after widening.
    for index in range(100):
        url = f"https://www.proshop.pl/Karta-graficzna?pn={index + 1}"
        catalog.append([url])
        connection.execute(
            "UPDATE strony SET nazwa = ?, sprawdzone = ? WHERE sklep = ? AND url = ?",
            ("Karta graficzna", checked, "proshop-listings", url),
        )
    connection.commit()

    chosen = due_for_pass(connection, "proshop-listings", 40, 0.5)
    picked = {page.url for page in chosen}

    assert picked & set(never_checked), (
        "no never-checked page was selected; they are starved indefinitely"
    )
    connection.close()
