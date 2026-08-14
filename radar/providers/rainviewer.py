"""RainViewer radar provider — full implementation.

All RainViewer URL building and HTTP lives here; views never see the upstream
shape. Tiles are fetched by the backend and re-served, so the browser never
hits tilecache.rainviewer.com.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from urllib.parse import urlparse

import httpx
from django.conf import settings

from radar import cache
from radar.providers.base import Frame
from radar.providers.base import FramesUnavailable
from radar.providers.base import RateLimited
from radar.providers.base import TileUpstreamError

logger = logging.getLogger(__name__)

# HTTP client timeouts to RainViewer: connect 3s, read 5s.
_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)

# Retry/backoff policy. A 429 is deliberately absent from it: retrying into a rate
# limit is what caused the limit, so 429 short-circuits to the cooldown below
# instead of spending an attempt budget (see _fetch_tile).
_FRAMES_BACKOFFS = (0.5, 1.0, 2.0)  # 3 retries
_TILE_BACKOFFS = (0.5, 1.0)  # 2 retries
_BACKOFF_JITTER = 0.25  # added to every retry sleep to de-synchronise a burst

# Process-wide gate on upstream tile fetches (see _UpstreamGate). Bound to the
# running loop and rebuilt when it changes, mirroring radar.cache.get_client —
# uvicorn keeps one loop, but WSGI runserver does not.
_gate: _UpstreamGate | None = None
_gate_loop: asyncio.AbstractEventLoop | None = None

# Monotonic deadline until which we refuse to call RainViewer at all, set when it
# answers 429. Module-level rather than part of _UpstreamGate on purpose: a
# monotonic deadline is loop-independent, so a loop rebuild must not forget that we
# are still being throttled.
_cooldown_until: float = 0.0


class _UpstreamGate:
    """Process-wide throttle in front of every upstream tile request.

    Two independent limits, because they bound different things:

    * ``_sem`` caps how many requests are *in flight* (``UPSTREAM_TILE_CONCURRENCY``);
    * ``_next_slot`` caps how fast requests *start* (``UPSTREAM_TILE_MIN_INTERVAL``).

    Concurrency alone is not a rate limit — four slots recycled over a keep-alive
    connection still emit hundreds of requests a minute, which is exactly how a cold
    start (~13 unarchived frames x 62 tiles, back-to-back) walks into a 429. Pacing
    happens *before* the semaphore so a waiting request never occupies a slot.
    """

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(settings.UPSTREAM_TILE_CONCURRENCY)
        self._pace = asyncio.Lock()
        self._next_slot = 0.0

    @contextlib.asynccontextmanager
    async def slot(self):
        """Wait for this request's turn, then hold a concurrency slot for it."""
        await self._await_turn()
        async with self._sem:
            yield

    async def _await_turn(self) -> None:
        """Sleep until this request's paced start time (claimed under the lock)."""
        interval = settings.UPSTREAM_TILE_MIN_INTERVAL
        if interval <= 0:
            return
        async with self._pace:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + interval
        delay = start - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


def _upstream_gate() -> _UpstreamGate:
    """Return the loop-bound gate throttling upstream tile fetches."""
    global _gate, _gate_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _gate is None or _gate_loop is not loop:
        _gate = _UpstreamGate()
        _gate_loop = loop
    return _gate


def cooldown_remaining() -> float:
    """Seconds left in the current 429 cooldown; 0.0 when not throttled."""
    return max(0.0, _cooldown_until - time.monotonic())


def _enter_cooldown(retry_after: float | None) -> float:
    """Start (or extend) the 429 cooldown; returns the window actually applied.

    Honours the server's ``Retry-After`` in full — unlike an in-request sleep, this
    gate *refuses* rather than waits, so a long window costs no held connection and
    hangs nothing. Still capped, so a hostile or buggy header can't park us
    indefinitely. Never shortens a cooldown already in force.
    """
    global _cooldown_until  # noqa: PLW0603
    window = retry_after if retry_after is not None else settings.UPSTREAM_RATE_LIMIT_COOLDOWN
    window = min(window, settings.UPSTREAM_RATE_LIMIT_COOLDOWN_MAX)
    _cooldown_until = max(_cooldown_until, time.monotonic() + window)
    return window


def reset_cooldown() -> None:
    """Clear the 429 cooldown. For tests — the cooldown is process-wide state."""
    global _cooldown_until  # noqa: PLW0603
    _cooldown_until = 0.0


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds form of a Retry-After header; None for absent or HTTP-date form."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date variant — fall back to the default cooldown


def _tile_backoff(attempt: int) -> float:
    """Seconds to wait before the next tile retry (5xx/transport failures only).

    Jitter de-synchronises a burst that all failed on the same tick.
    """
    return _TILE_BACKOFFS[attempt] + random.uniform(0.0, _BACKOFF_JITTER)  # noqa: S311 (jitter, not crypto)


# A RainViewer frame path is /v2/radar/<token>, where <token> is an opaque id —
# historically the epoch, now a hex string (e.g. /v2/radar/0f678d99e485). Restrict
# the token to [0-9a-zA-Z_-] (no "/" or "."), so a tampered/compromised upstream
# JSON still can't inject path traversal or steer our server-side fetch (SSRF).
_PATH_RE = re.compile(r"^/v2/radar/[0-9A-Za-z_-]+$")

_ATTRIBUTION = (
    'Weather data by <a href="https://www.rainviewer.com" '
    'target="_blank" rel="noopener">Rain Viewer</a>'
)


class RainViewerProvider:
    name = "rainviewer"

    def __init__(self) -> None:
        # Populated from the last get_frames(): host + {ts: path}.
        self._host: str = settings.RAINVIEWER_TILE_HOST
        self._paths: dict[int, str] = {}

    @property
    def frame_interval(self) -> int:
        """Seconds between RainViewer frames — unchanged 600."""
        return settings.FRAME_INTERVAL

    # -- frames ---------------------------------------------------------------

    async def get_frames(self) -> list[Frame]:
        """Return past frames oldest->newest, using the 60s Redis JSON cache.

        Cache hit -> parse and return without hitting upstream. Cache
        miss -> fetch with retry; on exhaustion raise FramesUnavailable.
        """
        key = cache.frames_key(self.name)
        cached = await cache.get_bytes(key)
        if cached is not None:
            return self._parse_frames(cached)

        body = await self._fetch_frames_json()
        if body is None:
            # Last-ditch: another worker may have populated the cache meanwhile.
            cached = await cache.get_bytes(key)
            if cached is not None:
                return self._parse_frames(cached)
            raise FramesUnavailable
        await cache.set_bytes(key, body, settings.FRAMES_CACHE_TTL)
        return self._parse_frames(body)

    async def _fetch_frames_json(self) -> bytes | None:
        url = settings.RAINVIEWER_API_URL
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for attempt in range(len(_FRAMES_BACKOFFS) + 1):
                try:
                    resp = await client.get(url)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    logger.warning("RainViewer frames fetch error %s: %s", url, exc)
                else:
                    # A 429 body is an error page, not frames JSON. Returning it here
                    # would hand _parse_frames unparseable bytes, so it is caught
                    # before the generic <500 success branch: enter the cooldown and
                    # report failure, which poll_radar counts and eventually gaps.
                    if resp.status_code == httpx.codes.TOO_MANY_REQUESTS:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        window = _enter_cooldown(retry_after)
                        logger.warning(
                            "RainViewer frames rate-limited; pausing upstream for %.0fs",
                            window,
                        )
                        return None
                    if resp.status_code < 500:  # noqa: PLR2004
                        return resp.content
                    logger.warning("RainViewer frames %s -> HTTP %s", url, resp.status_code)
                if attempt < len(_FRAMES_BACKOFFS):
                    await asyncio.sleep(_FRAMES_BACKOFFS[attempt])
        return None

    def _parse_frames(self, body: bytes) -> list[Frame]:
        data = json.loads(body)
        self._host = self._safe_host(data.get("host"))
        past = data.get("radar", {}).get("past", []) or []
        frames: list[Frame] = []
        self._paths = {}
        for item in past:
            path = item["path"]
            if not _PATH_RE.match(path):
                logger.warning("RainViewer: skipping frame with unexpected path %r", path)
                continue
            ts = int(item["time"])
            self._paths[ts] = path
            frames.append(Frame(timestamp=ts, ref=path))
        frames.sort(key=lambda f: f.timestamp)
        return frames

    @staticmethod
    def _safe_host(host: str | None) -> str:
        """Accept the upstream tile host only if https + an allowed domain.

        The host comes from the upstream JSON and drives a server-side fetch, so a
        tampered value (e.g. an internal IP) must not be honoured. Falls back to the
        configured ``RAINVIEWER_TILE_HOST`` otherwise (defense-in-depth, SSRF).
        """
        fallback = settings.RAINVIEWER_TILE_HOST
        if not host:
            return fallback
        parsed = urlparse(host)
        netloc = parsed.hostname or ""
        if parsed.scheme == "https" and netloc.endswith(settings.RAINVIEWER_ALLOWED_HOST_SUFFIX):
            return host
        logger.warning("RainViewer: rejecting upstream host %r; using %s", host, fallback)
        return fallback

    # -- tiles ----------------------------------------------------------------

    def tile_url(self, ts: int, z: int, x: int, y: int) -> str:
        """Compose the upstream tile URL from the configured fixed values."""
        path = self._paths.get(ts, f"/v2/radar/{ts}")
        return (
            f"{self._host}{path}/{settings.RADAR_TILE_SIZE}/{z}/{x}/{y}"
            f"/{settings.RADAR_COLOR}/{settings.RADAR_OPTIONS}.png"
        )

    def tile_client(self) -> httpx.AsyncClient:
        """A pooled client to reuse across one frame's tile fetches."""
        return httpx.AsyncClient(timeout=_TIMEOUT)

    async def get_tile(
        self,
        ts: int,
        z: int,
        x: int,
        y: int,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> bytes | None:
        """Fetch a tile PNG; None on a legitimate 404; raise on 5xx exhaustion.

        Reuses ``client`` when supplied (batch fetch); otherwise opens and closes a
        client for this single fetch.
        """
        url = self.tile_url(ts, z, x, y)
        if client is not None:
            return await self._fetch_tile(client, url)
        async with self.tile_client() as own:
            return await self._fetch_tile(own, url)

    async def _fetch_tile(self, client: httpx.AsyncClient, url: str) -> bytes | None:
        """Fetch one tile, or raise. ``RateLimited`` while upstream is throttling us.

        A 429 neither retries nor sleeps here: it opens the process-wide cooldown and
        raises immediately. Retrying into a rate limit is what produces the rate
        limit, and the caller has better options than waiting — the archiver abandons
        the rest of the batch, the on-demand view returns 502 at once instead of
        holding a request open for a fetch that is going to fail anyway.
        """
        for attempt in range(len(_TILE_BACKOFFS) + 1):
            remaining = cooldown_remaining()
            if remaining > 0:
                # Already throttled: refuse without touching upstream. Retrying
                # in-loop would only re-read the same deadline, so surface at once.
                raise RateLimited(remaining)
            try:
                # Pace + hold a concurrency slot for the request itself, never for
                # the backoff sleep, so a waiting retry occupies no slot.
                async with _upstream_gate().slot():
                    resp = await client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.warning("RainViewer tile fetch error %s: %s", url, exc)
            else:
                if resp.status_code == httpx.codes.OK:
                    return resp.content
                if resp.status_code == httpx.codes.NOT_FOUND:
                    return None  # legitimate empty region
                if resp.status_code == httpx.codes.TOO_MANY_REQUESTS:
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    window = _enter_cooldown(retry_after)
                    logger.warning(
                        "RainViewer tile rate-limited; pausing upstream for %.0fs",
                        window,
                    )
                    raise RateLimited(retry_after)
                logger.warning("RainViewer tile %s -> HTTP %s", url, resp.status_code)
            if attempt < len(_TILE_BACKOFFS):
                await asyncio.sleep(_tile_backoff(attempt))
        raise TileUpstreamError

    # -- attribution ----------------------------------------------------------

    def attribution(self) -> str:
        return _ATTRIBUTION
