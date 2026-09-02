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
    session = FakeSession([Response(429, "Too Many Requests"), Response(200, "unexpected")])
    fetcher = CurlCffiFetcher(0, session=session, sleeper=_no_sleep)
    status, _ = await fetcher("https://www.proshop.pl/RAM")
    assert status == 429
    assert fetcher.shop_refusal is True
    assert len(session.calls) == 1


async def test_short_challenge_shell_is_a_refusal_even_with_200():
    session = FakeSession([Response(200, "<title>Access Denied</title>")])
    fetcher = CurlCffiFetcher(0, session=session, sleeper=_no_sleep)
    await fetcher("https://www.proshop.pl/RAM")
    assert fetcher.shop_refusal is True


def test_close_releases_the_coherent_session():
    session = FakeSession([])
    fetcher = CurlCffiFetcher(0, session=session, sleeper=_no_sleep)
    fetcher.close()
    assert session.closed is True
