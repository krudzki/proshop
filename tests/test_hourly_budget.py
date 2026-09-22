"""The hour must be capped, not only the pass.

Measured live 2026-09-21. `pages_per_pass` was 80 and the lane was healthy all
morning at 47-65 pages/hour, because the queue only had 15-20 pages due per
cycle. When the queue filled up, the SAME configuration produced

    10:39  103 pages
    10:58  100 pages       -> 222 pages in the 10:00 hour
    11:31   95 pages
    11:45   71 pages

against a measured allowance of roughly 200/hour. The shop penalised the
address and production returned nothing for the next six hours: 28 consecutive
cycles refused on their first request.

So a per-pass budget cannot express the constraint the shop actually enforces.
A budget that is safe on average still breaches in a single hour whenever the
backlog happens to be deep - and the breach is not self-healing, which is what
makes it worth a guard rather than a tuning pass.

The pass must therefore ask: how many pages did I already fetch in the last
hour, and how many may I still take?
"""

from __future__ import annotations

from datetime import datetime, timedelta

from proshop import scanner


def _cycles(*pages_and_ages: tuple[int, int]) -> list[dict]:
    """Cycle records shaped like Database.recent_cycles() output."""
    now = datetime.now()
    return [
        {"ts": (now - timedelta(minutes=age)).isoformat(),
         "listing_pages": pages}
        for pages, age in pages_and_ages
    ]


def test_budget_shrinks_to_what_the_hour_still_allows():
    """The exact burst that earned the penalty must not be repeatable."""
    # 10:39 and 10:58 already spent 203 pages inside the hour.
    history = _cycles((103, 40), (100, 21))

    allowed = scanner.pages_allowed_now(
        history, pass_budget=80, hourly_cap=scanner.HOURLY_PAGE_CAP
    )

    assert allowed == 0, (
        f"hour already spent 203 pages, still offering {allowed} more"
    )


def test_a_quiet_hour_grants_the_full_pass_budget():
    """The guard must not tax a lane that is behaving.

    The healthy morning took 47-65 pages an hour; nothing about that should
    be throttled, or the fix costs coverage it was never meant to touch.
    """
    history = _cycles((19, 15), (17, 32), (20, 48))

    allowed = scanner.pages_allowed_now(
        history, pass_budget=40, hourly_cap=scanner.HOURLY_PAGE_CAP
    )

    assert allowed == 40, f"quiet hour throttled to {allowed}"


def test_pages_older_than_an_hour_do_not_count():
    """A rolling hour, not a running total - otherwise the lane never resumes."""
    history = _cycles((103, 61), (100, 75), (95, 90))

    allowed = scanner.pages_allowed_now(
        history, pass_budget=40, hourly_cap=scanner.HOURLY_PAGE_CAP
    )

    assert allowed == 40, (
        f"pages fetched over an hour ago still suppressing the pass ({allowed})"
    )


def test_partial_headroom_is_granted_not_rounded_away():
    """With 160 of 200 spent, the pass may take the remaining 40."""
    history = _cycles((80, 30), (80, 50))

    allowed = scanner.pages_allowed_now(
        history, pass_budget=80, hourly_cap=200
    )

    assert allowed == 40, f"expected the 40-page remainder, got {allowed}"


def test_unknown_history_does_not_block_the_lane():
    """Fail OPEN: no history means no evidence of a breach.

    A guard that silences the scanner when it cannot read the past is worse
    than the burst it prevents - that is the silent-starvation shape.
    """
    assert scanner.pages_allowed_now([], pass_budget=40, hourly_cap=200) == 40
    assert scanner.pages_allowed_now(
        [{"ts": "not-a-timestamp", "listing_pages": 999}],
        pass_budget=40, hourly_cap=200,
    ) == 40


def test_hourly_cap_leaves_margin_under_the_measured_band():
    """The cap is a measurement with margin, not the edge of the band.

    Refused passes were preceded by 193, 222, 222 and 313 pages in the hour;
    served passes reached 218. Sitting at the edge means the next deep
    backlog repeats today's outage.
    """
    assert scanner.HOURLY_PAGE_CAP <= 180, (
        f"cap {scanner.HOURLY_PAGE_CAP} is at or above the refusal band"
    )


class _CountingFetcher:
    """Serves an empty listing so the pass runs but parses nothing."""

    def __init__(self, *_a, **_k):
        self.shop_refusal = False
        self.refused_url = None
        self.calls: list[str] = []

    async def __call__(self, url: str):
        self.calls.append(url)
        return 200, "<html><body>no products</body></html>"

    def close(self) -> None:
        return None


def _settings(db_url: str):
    return type("S", (), {
        "database_url": db_url,
        "request_delay_s": 0,
        "pages_per_pass": 40,
        "focus_share": 0,
        "max_listing_pages": 1,
        "category_refresh_hours": 9999,
        "proshop_notify": False,
        "proshop_enabled": True,
        "max_alerts_per_cycle": 20,
        "webhook": lambda self, _c: None,
        "telegram_webhook_url": None,
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "telegram_discount_threshold": 80,
    })()


def test_run_actually_applies_the_cap_at_the_call_site(tmp_path, monkeypatch):
    """The predicate existing is not enough - run() must consult it.

    Twice today a guard passed its own unit test while the call site that
    feeds it was neutered and the suite stayed green. So assert on the limit
    that reaches the queue during a real run(), with an hour's worth of pages
    already spent.
    """
    import asyncio

    from deal_pipeline.database import Database

    db_url = f"sqlite:///{tmp_path}/products.db"

    # Record two cycles that already spent the whole hourly allowance.
    database = Database(url=db_url)
    try:
        for pages in (103, 100):
            database.record_cycle(
                scanner_name=scanner.STORE,
                checked_count=pages * 10,
                available_count=pages * 10,
                report_count=0,
                extra={"listing_pages": pages},
            )
    finally:
        database.close()

    seen_limits: list[int] = []

    def _spy(_conn, _store, limit, _share):
        seen_limits.append(limit)
        return []

    monkeypatch.setattr(scanner, "CurlCffiFetcher", _CountingFetcher)
    monkeypatch.setattr(scanner, "due_for_pass", _spy)

    asyncio.run(scanner.run(_settings(db_url), dry_run=False, notify=False))

    assert seen_limits, "due_for_pass was never called"
    assert seen_limits[0] == 0, (
        f"203 pages already spent this hour, yet the pass asked for "
        f"{seen_limits[0]} more"
    )


def test_category_refresh_contacts_reduce_the_listing_budget(tmp_path, monkeypatch):
    """All contacts share the measured 12-request burst budget.

    A normal category refresh contacts nine roots before the listing loop.
    Limiting only `pages_per_pass` to 12 would still send 21 contacts and
    reproduce the measured 429 on contact 13.
    """
    import asyncio

    budgets = []

    class _BudgetedFetcher:
        def __init__(self, *_a, max_requests=None, **_k):
            budgets.append(max_requests)
            self.remaining_requests = max_requests if max_requests is not None else 999
            self.requests_made = 0
            self.budget_exhausted = False
            self.shop_refusal = False
            self.refused_url = None

        async def __call__(self, _url):
            self.requests_made += 1
            self.remaining_requests -= 1
            return 200, (
                '<div id="subCategoryList">'
                '<a href="/RAM">RAM</a>'
                '</div>'
            )

        def close(self):
            return None

    seen_limits = []

    def _spy(_conn, _store, limit, _share):
        seen_limits.append(limit)
        return []

    settings = _settings(f"sqlite:///{tmp_path}/products.db")
    settings.pages_per_pass = 12
    settings.category_refresh_hours = 24
    monkeypatch.setattr(scanner, "CurlCffiFetcher", _BudgetedFetcher)
    monkeypatch.setattr(scanner, "due_for_pass", _spy)

    asyncio.run(scanner.run(settings, dry_run=False, notify=False))

    assert budgets == [scanner.MAX_CONTACTS_PER_CYCLE]
    assert seen_limits == [3], (
        f"nine refresh contacts should leave three listing contacts, got {seen_limits}"
    )
