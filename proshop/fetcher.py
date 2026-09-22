"""Coherent Chrome-TLS transport for Proshop listings.

Two independent protections live at this boundary:

* A historical cold-session probe found that a challenge on a session's first
  contact could clear after a long pause, so an unserved session may retry a
  bounded number of times. Once any real page has been served, a refusal is
  final and is never retried.
* A controlled production-cadence probe on 2026-09-22, after 32 minutes of
  complete quiet, served contacts 1..12 and returned HTTP 429 on contact 13.
  The transport therefore counts every real contact, including retries, so a
  page-loop limit cannot cross that boundary invisibly.

Both controls preserve one cookie/TLS identity for the pass. The scanner sets
the per-cycle contact budget; standalone tests may leave it unbounded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from curl_cffi import requests

REFUSAL_STATUS = {403, 429}
TRANSIENT_STATUS = {502, 503, 504}
BLOCK_MARKERS = ("access denied", "too many requests", "just a moment", "captcha")

# The IDLE arm that succeeded waited ~180s between contacts; the BUSY arm,
# which re-asked every 10s, was refused 18/18. The pause is load-bearing, so
# it is sized from the measurement rather than from a retry convention.
COLD_RETRY_WAIT_S = 180.0
# Three contacts total. Enough for the measured warm-up, bounded so a shop
# that really is blocking is not hammered - and cheap, because this only ever
# runs while the session has parsed nothing.
COLD_RETRY_KNOCKS = 3


class CurlCffiFetcher:
    """One cookie-preserving browser identity per scanner pass."""

    def __init__(
        self,
        delay_s: float,
        *,
        session=None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout_s: float = 30.0,
        cold_retry_wait_s: float = COLD_RETRY_WAIT_S,
        cold_retry_knocks: int = COLD_RETRY_KNOCKS,
        max_requests: int | None = None,
    ) -> None:
        self._delay = max(0.0, delay_s)
        self._sleep = sleeper
        self._timeout = timeout_s
        self._session = session or self._new_session()
        self._first = True
        self.shop_refusal = False
        # Which request was actually refused. Once `shop_refusal` latches, every
        # later call short-circuits and returns status 0 without touching the
        # network, so the caller's own URL says nothing about the refusal. Live
        # journals blamed an innocent listing page for hours because of this.
        self.refused_url: str | None = None
        # A brand-new session is challenged on first contact and admitted on a
        # later one; see the module docstring. `_served` tracks whether this
        # session has ever been given a real page, because only a COLD session
        # earns the extra knocks.
        self._cold_retry_wait = max(0.0, cold_retry_wait_s)
        self._cold_retry_knocks = max(1, cold_retry_knocks)
        self._served = False
        self._max_requests = None if max_requests is None else max(0, int(max_requests))
        self.requests_made = 0
        self.budget_exhausted = False

    @property
    def remaining_requests(self) -> int | None:
        """Real network contacts left, or ``None`` for an unbounded helper."""
        if self._max_requests is None:
            return None
        return max(0, self._max_requests - self.requests_made)

    def _new_session(self):
        return requests.Session(impersonate="chrome131", trust_env=True)

    async def __call__(self, url: str) -> tuple[int, str]:
        if self.shop_refusal:
            return 0, ""
        # A cold session is allowed several contacts; a warm one gets the
        # ordinary single pass through the transient-retry loop.
        knocks = 1 if self._served else self._cold_retry_knocks
        for knock in range(knocks):
            if knock:
                await self._sleep(self._cold_retry_wait)
            status, body, refused = await self._attempt(url)
            if not refused:
                if status == 200:
                    self._served = True
                return status, body
            no_retry_budget = self.remaining_requests == 0
            if knock == knocks - 1 or no_retry_budget:
                if no_retry_budget:
                    self.budget_exhausted = True
                self.shop_refusal = True
                self.refused_url = url
                return status, body
        return 0, ""

    async def _attempt(self, url: str) -> tuple[int, str, bool]:
        """One contact, with the existing transient-status retries.

        Returns (status, body, refused). `refused` separates "the shop turned
        this request away" from every other outcome, so the caller decides
        whether that verdict is final - which depends on the session being
        cold or warm, something this method deliberately does not know.
        """
        for attempt in range(3):
            if self.remaining_requests == 0:
                self.budget_exhausted = True
                return 0, "", False
            if not self._first:
                await self._sleep(self._delay if attempt == 0 else max(self._delay, 2**attempt))
            self._first = False
            # Count attempted contacts, not successful responses. Cloudflare
            # sees a timed-out or broken request too, and retries must not
            # create the measured-refused thirteenth contact behind the page
            # loop's back.
            self.requests_made += 1
            try:
                response = await asyncio.to_thread(
                    self._session.get,
                    url,
                    timeout=self._timeout,
                    allow_redirects=True,
                )

            except Exception:
                if attempt < 2:
                    continue
                return 0, "", False
            status = int(response.status_code)
            body = str(response.text or "")
            lowered = body[:10_000].lower()
            hidden_refusal = any(marker in lowered for marker in BLOCK_MARKERS)
            if status in REFUSAL_STATUS or hidden_refusal:
                return status, body, True
            if status in TRANSIENT_STATUS and attempt < 2:
                continue
            return status, body, False
        return 0, "", False

    def close(self) -> None:
        self._session.close()
