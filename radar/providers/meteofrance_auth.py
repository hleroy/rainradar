"""Météo-France OAuth2 client-credentials token cache.

Caches a bearer token minted from the portal "application ID" (a base64 blob =
``base64(consumer_key:consumer_secret)``) and refreshes it before expiry.
Refresh is single-flight — N concurrent callers produce exactly one token POST —
via a loop-bound ``asyncio.Lock`` rebuilt when the running loop changes, mirroring
:func:`radar.cache.get_client` (uvicorn keeps one loop; WSGI runserver does not).

Never logs the token or the application ID.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# Timeouts + fixed-backoff retry mirror rainviewer.py's frames policy.
_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
_BACKOFFS = (0.5, 1.0, 2.0)  # 3 retries
_BACKOFF_JITTER = 0.25  # de-synchronise a burst that all refreshed on the same tick
_REFRESH_MARGIN_S = 60  # refresh this long before the real expiry (safety margin)
_DEFAULT_TTL_S = 3600  # fallback if the token response omits expires_in


class AuthError(Exception):
    """Token could not be obtained after retries. Provider chains it onward."""


class MeteoFranceAuth:
    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0  # monotonic-clock deadline
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_lock(self) -> asyncio.Lock:
        """Return an ``asyncio.Lock`` bound to the running loop (rebuilt on change)."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _valid(self) -> bool:
        return self._token is not None and time.monotonic() < self._expires_at - _REFRESH_MARGIN_S

    async def get_token(self) -> str:
        """Return a valid bearer token, refreshing (single-flight) when needed."""
        if self._valid():
            return self._token  # type: ignore[return-value]  # _valid ⇒ not None
        async with self._get_lock():
            # Re-check under the lock: another caller may have refreshed while we waited.
            if self._valid():
                return self._token  # type: ignore[return-value]
            self._token, self._expires_at = await self._refresh()
            return self._token

    async def invalidate(self) -> None:
        """Drop the cached token so the next ``get_token`` refreshes (401 recovery)."""
        async with self._get_lock():
            self._token = None
            self._expires_at = 0.0

    async def _refresh(self) -> tuple[str, float]:
        headers = {
            "Authorization": f"Basic {settings.METEOFRANCE_APPLICATION_ID}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "client_credentials"}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for attempt in range(len(_BACKOFFS) + 1):
                try:
                    resp = await client.post(
                        settings.METEOFRANCE_TOKEN_URL,
                        data=data,
                        headers=headers,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    logger.warning("Météo-France token fetch error: %s", exc)
                else:
                    if resp.status_code == httpx.codes.OK:
                        return self._parse_token(resp)
                    # A 4xx (bad/expired credentials) will not fix on retry — fail fast.
                    logger.warning("Météo-France token endpoint -> HTTP %s", resp.status_code)
                    if resp.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
                        break
                if attempt < len(_BACKOFFS):
                    await asyncio.sleep(_BACKOFFS[attempt] + random.uniform(0.0, _BACKOFF_JITTER))  # noqa: S311
        raise AuthError

    @staticmethod
    def _parse_token(resp: httpx.Response) -> tuple[str, float]:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise AuthError from exc
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthError
        ttl = payload.get("expires_in")
        ttl = int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else _DEFAULT_TTL_S
        return token, time.monotonic() + ttl
