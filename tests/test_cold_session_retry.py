"""A cold session must knock twice before the refusal is believed.

Measured on the live shop 2026-09-21/22, timer stopped so the probe could not
re-arm the penalty:

    fresh session, ONE knock            challenged 12/12 across 110 min
    fresh session, knock + wait + knock served on the second contact,
                                        192 KB of real listing HTML

and a controlled two-arm trial with the arm order alternated:

    two-knock served 2/2      control (single knock) served 0/2

So the shop challenges a brand-new session and admits it on a later contact
within that same session. The scanner builds a fresh `CurlCffiFetcher` every
cycle, sent one request, saw 429 and latched `shop_refusal` - abandoning a
session that was one knock away from being served. Twenty-eight consecutive
cycles reported a store-wide block that a second request would have cleared.

This is Signature 9 from silent-queue-starvation: a session refused because it
never knocked twice.

The retry is deliberately asymmetric, and both halves are asserted here:

  * a COLD session (nothing parsed yet) re-contacts after a pause, because a
    challenge on first contact is the documented warm-up, not a verdict;
  * a WARM session latches immediately, because an episodic 403 mid-cycle IS
    the shop refusing, and retrying it would waste the budget and the
    request allowance on an address that has already answered.
"""

from __future__ import annotations

import asyncio

import pytest

from proshop.fetcher import CurlCffiFetcher


class _Response:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text


class _ScriptedSession:
    """Serves a scripted sequence and counts contacts."""

    def __init__(self, *responses: tuple[int, str]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        if self._responses:
            status, text = self._responses.pop(0)
        else:
            status, text = 200, "<html>last</html>"
        return _Response(status, text)

    def close(self) -> None:
        return None


CHALLENGE = (429, "<html><title>Just a moment...</title></html>")
LISTING = (200, "<html><div class='site-productlist-item'>ok</div></html>")


def _fetcher(session, **kwargs):
    waits: list[float] = []

    async def sleeper(seconds: float) -> None:
        waits.append(seconds)

    fetcher = CurlCffiFetcher(delay_s=0, session=session, sleeper=sleeper,
                              **kwargs)
    return fetcher, waits


def test_cold_session_knocks_again_after_a_challenge():
    """The exact shape measured live: second contact on the same session."""
    session = _ScriptedSession(CHALLENGE, LISTING)
    fetcher, _waits = _fetcher(session)

    status, body = asyncio.run(fetcher("https://www.proshop.pl/Komputer"))

    assert len(session.calls) == 2, (
        f"cold session gave up after {len(session.calls)} contact(s); "
        "the live measurement needed two"
    )
    assert status == 200, f"second knock not served: status {status}"
    assert "site-productlist-item" in body
    assert fetcher.shop_refusal is False, "refusal latched despite being served"


def test_cold_session_waits_between_the_two_knocks():
    """Re-asking instantly is not what was measured - the pause is load-bearing.

    The IDLE arm used ~180s between contacts; a tight retry loop is the BUSY
    arm, which was refused 18/18.
    """
    session = _ScriptedSession(CHALLENGE, LISTING)
    fetcher, waits = _fetcher(session, cold_retry_wait_s=45.0)

    asyncio.run(fetcher("https://www.proshop.pl/Komputer"))

    assert any(w >= 45.0 for w in waits), (
        f"no warm-up pause before the second knock; waits={waits}"
    )


def test_cold_session_gives_up_after_a_bounded_number_of_knocks():
    """A shop that really is blocking must not spin forever."""
    session = _ScriptedSession(CHALLENGE, CHALLENGE, CHALLENGE, CHALLENGE)
    fetcher, _waits = _fetcher(session, cold_retry_wait_s=0.0)

    status, _body = asyncio.run(fetcher("https://www.proshop.pl/Komputer"))

    assert fetcher.shop_refusal is True, "persistent challenge never latched"
    assert status == 429
    assert len(session.calls) <= 3, (
        f"knocked {len(session.calls)} times; the retry is unbounded"
    )


def test_warm_session_latches_immediately():
    """Once the session has parsed a page, a refusal is the shop's verdict.

    Retrying here would spend the pass, and the rate ceiling makes extra
    requests expensive: the penalty outlives the traffic.
    """
    session = _ScriptedSession(LISTING, CHALLENGE, LISTING)
    fetcher, _waits = _fetcher(session)

    first_status, _ = asyncio.run(fetcher("https://www.proshop.pl/Komputer"))
    assert first_status == 200

    contacts_before = len(session.calls)
    status, _ = asyncio.run(fetcher("https://www.proshop.pl/Laptop"))

    assert status == 429
    assert fetcher.shop_refusal is True, "warm refusal did not latch"
    assert len(session.calls) == contacts_before + 1, (
        "warm session retried a genuine refusal"
    )


def test_latched_refusal_still_short_circuits():
    """The existing contract must survive: no requests after a latch."""
    session = _ScriptedSession(CHALLENGE, CHALLENGE, CHALLENGE)
    fetcher, _waits = _fetcher(session, cold_retry_wait_s=0.0)

    asyncio.run(fetcher("https://www.proshop.pl/Komputer"))
    calls_after_latch = len(session.calls)
    status, body = asyncio.run(fetcher("https://www.proshop.pl/Laptop"))

    assert (status, body) == (0, "")
    assert len(session.calls) == calls_after_latch, "requested after latching"


def test_refused_url_names_the_request_that_was_refused():
    """Regression: the journal must not blame the next queue entry."""
    session = _ScriptedSession(CHALLENGE, CHALLENGE, CHALLENGE)
    fetcher, _waits = _fetcher(session, cold_retry_wait_s=0.0)

    asyncio.run(fetcher("https://www.proshop.pl/Komputer"))

    assert fetcher.refused_url == "https://www.proshop.pl/Komputer"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_transient_status_still_retries(status):
    """Unchanged behaviour for genuine transients."""
    session = _ScriptedSession((status, "boom"), LISTING)
    fetcher, _waits = _fetcher(session)

    result, _body = asyncio.run(fetcher("https://www.proshop.pl/Komputer"))

    assert result == 200
    assert fetcher.shop_refusal is False
