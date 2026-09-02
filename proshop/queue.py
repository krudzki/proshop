"""Focus-reserved ordering for Proshop listing pages."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from proshop.categories import canonical_category, is_focus, priority_of_slug

CANDIDATE_MULTIPLIER = 4
FULL_SCAN_CAP = 50_000
_PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2}


@dataclass(frozen=True)
class QueuedPage:
    url: str
    key: str
    name: str
    price: float | None
    checked_at: datetime | None

    @property
    def focus(self) -> bool:
        return is_focus(self.url)

    @property
    def priority(self) -> str:
        return priority_of_slug(canonical_category(self.name, self.name, self.url))


def _rows(conn: sqlite3.Connection, store: str, limit: int) -> list[QueuedPage]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT url, klucz, nazwa, cena, sprawdzone
             FROM strony
            WHERE sklep = ?
            ORDER BY sprawdzone IS NOT NULL, sprawdzone ASC
            LIMIT ?""",
        (store, limit),
    ).fetchall()
    pages = []
    for row in rows:
        checked = row["sprawdzone"]
        try:
            checked_at = datetime.fromisoformat(checked) if checked else None
        except ValueError:
            checked_at = None
        pages.append(
            QueuedPage(
                url=row["url"],
                key=row["klucz"] or "",
                name=row["nazwa"] or "",
                price=row["cena"],
                checked_at=checked_at,
            )
        )
    return pages


def _staleness(page: QueuedPage) -> tuple[int, datetime]:
    return (0 if page.checked_at is None else 1, page.checked_at or datetime.min)


def order_pages(pages: list[QueuedPage], limit: int, focus_share: float) -> list[QueuedPage]:
    """Reserve a bounded share for RAM/GPU and fill the rest fairly."""
    if limit <= 0 or not pages:
        return []
    share = min(max(focus_share, 0.0), 1.0)
    quota = int(round(limit * share))
    focus = sorted((page for page in pages if page.focus), key=_staleness)
    general = sorted(
        (page for page in pages if not page.focus),
        key=lambda page: (_PRIORITY_RANK.get(page.priority, 2), *_staleness(page)),
    )
    chosen = focus[:quota]
    taken = {page.url for page in chosen}
    for page in [*general, *focus[quota:]]:
        if len(chosen) >= limit:
            break
        if page.url not in taken:
            chosen.append(page)
            taken.add(page.url)
    return chosen[:limit]


def due_for_pass(
    conn: sqlite3.Connection,
    store: str,
    limit: int,
    focus_share: float,
) -> list[QueuedPage]:
    """Read a wide prefix, widening once if a tied first lap hides focus."""
    candidates = _rows(conn, store, max(limit, limit * CANDIDATE_MULTIPLIER))
    quota = int(round(limit * min(max(focus_share, 0.0), 1.0)))
    if quota and sum(page.focus for page in candidates) < quota:
        candidates = _rows(conn, store, FULL_SCAN_CAP)
    return order_pages(candidates, limit, focus_share)
