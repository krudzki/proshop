"""A refused pass must not keep knocking, and must say where it was refused.

Two faults measured live on 2026-09-21, both invisible in the logs:

1. `refresh_categories` runs on EVERY cycle once the shop starts refusing.
   The refresh timestamp is only stamped when `complete=True`, and a refusal
   forces `complete=False`, so the 24h interval never starts. Each cycle then
   fires up to 12 root requests into an IP the shop is already refusing —
   the scanner sustains the very penalty it is waiting out.

2. The refusal is logged against the page the loop was ABOUT to read, not the
   request that was actually refused. Once `shop_refusal` is latched the
   fetcher short-circuits and returns `status=0` without a request, so the
   log names an innocent URL. Live journals blamed `Karta-graficzna?pn=12`
   for hours while the real refusal happened during the category refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from proshop import scanner
from proshop.fetcher import CurlCffiFetcher


@dataclass
class Response:
    status_code: int
    text: str


class RefusingSession:
    """Refuses every request, like a Cloudflare-penalised address."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return Response(429, "<title>Just a moment...</title>")

    def close(self) -> None:
        return None


async def _no_sleep(_seconds):
    return None


def test_a_refused_refresh_does_not_repeat_on_the_very_next_cycle():
    """Across CYCLES, a refused refresh must be spaced — not retried at once.

    Within one cycle `refresh_categories` already stops at the first refusal.
    The live fault is between cycles: the success timestamp is the only state,
    so a refused refresh leaves it untouched and `should_refresh_catalog`
    returns True again 17 minutes later, forever.

    This is what the journals showed: `proshop_categories_refreshed
    complete=False` on every single cycle for hours.
    """
    # No successful refresh has ever been recorded (the live state after a
    # long refusal streak), and the shop just refused us.
    last_success = None
    refused_at = datetime(2026, 9, 21, 17, 8)
    next_cycle = refused_at + timedelta(minutes=17)

    assert scanner.should_refresh_catalog(last_success, dry_run=False) is True

    # Knowing we were refused 17 minutes ago must suppress the retry.
    assert (
        scanner.refresh_backoff_until(refused_at.isoformat(), now=next_cycle)
        is True
    ), "a refusal 17 minutes ago did not hold off the next refresh attempt"


async def test_refusal_is_reported_against_the_request_that_was_refused():
    """`status=0` means the fetcher short-circuited: do not blame that URL.

    Reproduces the live shape — the refusal is latched during the category
    refresh, then the listing loop's first page reports the refusal while
    having sent no request at all.
    """
    session = RefusingSession()
    fetcher = CurlCffiFetcher(0, session=session, sleeper=_no_sleep)

    # Latch the refusal on a real request, as the category refresh does.
    refused_status, _ = await fetcher("https://www.proshop.pl/Komputer")
    assert refused_status == 429
    assert fetcher.shop_refusal is True
    assert fetcher.refused_url == "https://www.proshop.pl/Komputer", (
        "the fetcher does not record WHICH request was refused, so the "
        "scanner can only log the URL it was about to try next"
    )

    # A later call short-circuits without touching the network.
    before = len(session.calls)
    status, _ = await fetcher("https://www.proshop.pl/Karta-graficzna?pn=12")
    assert status == 0
    assert len(session.calls) == before, "short-circuit still issued a request"


@pytest.mark.parametrize("interval_hours", [24])
def test_refresh_backs_off_after_a_refusal(interval_hours):
    """A refusal must delay the next attempt, not leave the interval unstarted.

    `should_refresh_catalog` reads a single timestamp that is only written on
    success, so a refused refresh leaves it unchanged and the next cycle
    refreshes again immediately. The scanner needs a way to record "tried and
    was refused" so the retry is spaced.
    """
    assert hasattr(scanner, "refresh_backoff_until"), (
        "no back-off helper exists: a refused refresh repeats every cycle"
    )


def test_back_off_does_not_hold_when_nothing_was_ever_refused():
    """The guard must not be too WIDE.

    Backing off on absent state would suppress the very first category
    discovery on a fresh install, which is a silent coverage loss - the
    mirror-image failure of the bug being fixed.
    """
    assert scanner.refresh_backoff_until(None) is False
    assert scanner.refresh_backoff_until("") is False
    # Corrupt state must fail OPEN: at worst one extra refresh attempt.
    assert scanner.refresh_backoff_until("not-a-timestamp") is False


def test_back_off_expires_so_discovery_always_resumes():
    """A block must cost minutes of discovery, never a permanent stop."""
    long_ago = (datetime.now() - timedelta(minutes=scanner.REFRESH_BACKOFF_MINUTES + 1))
    assert scanner.refresh_backoff_until(long_ago.isoformat()) is False


class _RefusingFetcher:
    """Refuses everything, latching like the real transport."""

    def __init__(self, *_a, **_k):
        self.shop_refusal = False
        self.refused_url = None
        self.calls = []

    async def __call__(self, url: str):
        self.calls.append(url)
        if self.shop_refusal:
            return 0, ""
        self.shop_refusal = True
        self.refused_url = url
        return 429, "<title>Just a moment...</title>"

    def close(self) -> None:
        return None


def _settings(db_url: str, refresh_hours: int = 9999):
    return type("S", (), {
        "database_url": db_url,
        "request_delay_s": 0,
        "pages_per_pass": 1,
        "focus_share": 0,
        "max_listing_pages": 1,
        "category_refresh_hours": refresh_hours,
        "proshop_notify": False,
        "proshop_enabled": True,
        "max_alerts_per_cycle": 20,
        "webhook": lambda self, _c: None,
        "telegram_webhook_url": None,
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "telegram_discount_threshold": 80,
    })()


def _queue(*_a, **_k):
    return [type("Q", (), {
        "url": "https://www.proshop.pl/Karta-graficzna?pn=12",
        "name": "Karta graficzna",
        "key": "",
        "price": None,
        "checked_at": None,
    })()]


def test_a_refused_pass_records_the_refusal_so_the_next_cycle_backs_off(
    tmp_path, monkeypatch
):
    """The WRITE must happen, not merely the predicate exist.

    This asserts on the persisted state after a real `run()`, because a
    correct `refresh_backoff_until` is useless if nothing ever records that a
    refusal happened. Neutering the `set_state` call must turn this red.
    """
    import asyncio

    from deal_pipeline.database import Database

    monkeypatch.setattr(scanner, "CurlCffiFetcher", _RefusingFetcher)
    monkeypatch.setattr(scanner, "due_for_pass", _queue)

    db_url = f"sqlite:///{tmp_path}/products.db"
    outcome, _stats = asyncio.run(
        scanner.run(_settings(db_url), dry_run=False, notify=False)
    )

    assert outcome.shop_refusal is True

    database = Database(url=db_url)
    try:
        recorded = database.get_state(scanner.REFUSED_REFRESH_STATE_KEY)
    finally:
        database.close()

    assert recorded, (
        "the pass was refused and nothing recorded it: the next cycle will "
        "knock again in 17 minutes"
    )
    # And the recorded value must actually suppress the next attempt.
    assert scanner.refresh_backoff_until(recorded) is True


def test_a_refused_CATEGORY_REFRESH_is_recorded_too(tmp_path, monkeypatch):
    """The refresh branch has its OWN write, and needs its own test.

    There are two places a pass can meet a refusal - during category refresh
    and inside the listing loop - and each records it separately. A test that
    only exercises the listing loop leaves the refresh write unguarded:
    neutering it kept the suite green, which is how this test came to exist.

    Here the refresh is DUE (refresh_hours=0), so the refusal happens on the
    first root request and the listing loop is never reached.
    """
    import asyncio

    from deal_pipeline.database import Database

    fetchers = []

    class _Recording(_RefusingFetcher):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            fetchers.append(self)

    monkeypatch.setattr(scanner, "CurlCffiFetcher", _Recording)
    monkeypatch.setattr(scanner, "due_for_pass", lambda *_a, **_k: [])

    db_url = f"sqlite:///{tmp_path}/products.db"
    asyncio.run(
        scanner.run(_settings(db_url, refresh_hours=0), dry_run=False, notify=False)
    )

    # The refusal really happened during the refresh, not the listing loop.
    assert fetchers and fetchers[0].refused_url
    assert "?pn=" not in fetchers[0].refused_url, (
        f"expected a category root, got {fetchers[0].refused_url}"
    )

    database = Database(url=db_url)
    try:
        recorded = database.get_state(scanner.REFUSED_REFRESH_STATE_KEY)
    finally:
        database.close()

    assert recorded, (
        "a refused category refresh recorded nothing, so all 12 root requests "
        "are re-sent on the next cycle"
    )
    assert scanner.refresh_backoff_until(recorded) is True
