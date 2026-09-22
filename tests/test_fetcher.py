"""Transport retry and refusal tests."""

from __future__ import annotations

from dataclasses import dataclass

from proshop.fetcher import CurlCffiFetcher


@dataclass
class Response:
    status_code: int
    text: str


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


async def _no_sleep(_seconds):
    return None


async def test_transient_status_retries_at_fetch_boundary():
    session = FakeSession([Response(503, "temporary"), Response(200, "<ul id='products'>ok</ul>")])
    fetcher = CurlCffiFetcher(0, session=session, sleeper=_no_sleep)
    status, body = await fetcher("https://www.proshop.pl/RAM")
    assert status == 200
    assert "products" in body
    assert len(session.calls) == 2


async def test_429_aborts_without_retrying_or_changing_identity():
    """A WARM session treats a refusal as final, on the same identity.

    Rescaled 2026-09-22, not exempted. This test used to assert that the very
    first 429 aborts after one call, which encoded a model the live shop
    disproved: a brand-new session is challenged on first contact and admitted
    on a later one (fetcher module docstring; two-knock 2/2 vs single-knock
    0/2). Asserting the old count here would lock in the bug that cost 28
    consecutive cycles.

    What the test genuinely protects survives unchanged and is asserted below:
    once the session has been served, a refusal is believed immediately, and
    the identity is never swapped mid-pass.
    """
    session = FakeSession([
        Response(200, "<ul id='products'>ok</ul>"),   # session becomes warm
        Response(429, "Too Many Requests"),
        Response(200, "unexpected"),
    ])
    fetcher = CurlCffiFetcher(0, session=session, sleeper=_no_sleep)

    first, _ = await fetcher("https://www.proshop.pl/RAM")
    assert first == 200

    status, _ = await fetcher("https://www.proshop.pl/GPU")
    assert status == 429
    assert fetcher.shop_refusal is True
    # Two contacts total: the warm refusal was not retried.
    assert len(session.calls) == 2
    # And the identity is the one we started with - no session was rebuilt.
    assert fetcher._session is session


async def test_cold_429_is_retried_before_it_is_believed():
    """The counterpart: a cold session must knock again before latching."""
    session = FakeSession([
        Response(429, "Too Many Requests"),
        Response(200, "<ul id='products'>ok</ul>"),
    ])
    fetcher = CurlCffiFetcher(0, session=session, sleeper=_no_sleep,
                              cold_retry_wait_s=0)

    status, body = await fetcher("https://www.proshop.pl/RAM")

    assert status == 200
    assert "products" in body
    assert fetcher.shop_refusal is False
    assert len(session.calls) == 2


async def test_short_challenge_shell_is_a_refusal_even_with_200():
    """Marker detection is unchanged; only the number of contacts moved.

    A cold session knocks up to three times, so the fixture supplies three
    challenge shells - otherwise the test would measure the fixture running
    out of responses rather than the refusal rule.
    """
    session = FakeSession([
        Response(200, "<title>Access Denied</title>"),
        Response(200, "<title>Access Denied</title>"),
        Response(200, "<title>Access Denied</title>"),
    ])
    fetcher = CurlCffiFetcher(0, session=session, sleeper=_no_sleep,
                              cold_retry_wait_s=0)

    await fetcher("https://www.proshop.pl/RAM")

    assert fetcher.shop_refusal is True
    assert fetcher.refused_url == "https://www.proshop.pl/RAM"




def test_close_releases_the_coherent_session():
    session = FakeSession([])
    fetcher = CurlCffiFetcher(0, session=session, sleeper=_no_sleep)
    fetcher.close()
    assert session.closed is True
