"""Coherent Chrome-TLS transport for Proshop listings."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from curl_cffi import requests

REFUSAL_STATUS = {403, 429}
TRANSIENT_STATUS = {502, 503, 504}
BLOCK_MARKERS = ("access denied", "too many requests", "just a moment", "captcha")


class CurlCffiFetcher:
    """One cookie-preserving browser identity per scanner pass."""

    def __init__(
        self,
        delay_s: float,
        *,
        session=None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout_s: float = 30.0,
    ) -> None:
        self._delay = max(0.0, delay_s)
        self._sleep = sleeper
        self._timeout = timeout_s
        self._session = session or requests.Session(impersonate="chrome131")
        self._first = True
        self.shop_refusal = False

    async def __call__(self, url: str) -> tuple[int, str]:
        if self.shop_refusal:
            return 0, ""
        for attempt in range(3):
            if not self._first:
                await self._sleep(self._delay if attempt == 0 else max(self._delay, 2**attempt))
            self._first = False
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
                return 0, ""
            status = int(response.status_code)
            body = str(response.text or "")
            lowered = body[:10_000].lower()
            hidden_refusal = any(marker in lowered for marker in BLOCK_MARKERS)
            if status in REFUSAL_STATUS or hidden_refusal:
                self.shop_refusal = True
                return status, body
            if status in TRANSIENT_STATUS and attempt < 2:
                continue
            return status, body
        return 0, ""

    def close(self) -> None:
        self._session.close()
