"""Météo-France radar provider — real implementation.

Fetches the DPRadar LAME_D_EAU 500 m ODIM composite (latest-only upstream),
renders it to the same 62-tile Web-Mercator matrix, and re-serves those tiles — the
browser never touches Météo-France (all tiles come from our ``/tiles/…`` paths).

When ``METEOFRANCE_REFLECTIVITY_ENABLED`` is on, a frame fetches a *second* product —
the REFLECTIVITE mosaic — concurrently with the rain one, and renders it as a wash
*under* the rain, flattened into the same single tile. Both arms sit inside the one
per-``ts`` single-flight memo, so it stays one pair of downloads per frame no matter
how many tiles are asked for at once.

Isolation: this is a fourth failure domain. Auth, catalog, download, HDF5 parse and
render failures all surface as the Protocol's ``FramesUnavailable`` /
``TileUpstreamError`` (RenderError/AuthError are chained into them), so
``archiver.py`` / ``views.py`` need zero exception-handling changes and a
Météo-France failure never touches RainViewer, lightning, or alerts. The reflectivity
arm is weaker still: it is best-effort throughout, so *any* failure in it — catalog,
download, BUFR decode, a deadline overrun, or a mosaic too far from the rain frame's
instant — degrades the frame to rain-only, byte-for-byte, instead of raising
at all. The one thing the two arms genuinely share is the ``MeteoFranceAuth`` token
cache, which is why the wash arm never invalidates it (see ``_fetch_reflectivity``).

Never logs the access token or the application ID.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import UTC
from datetime import datetime

import httpx
from django.conf import settings

from radar import cache
from radar.logging_json import emit
from radar.providers import meteofrance_render
from radar.providers.base import Frame
from radar.providers.base import FramesUnavailable
from radar.providers.base import TileUpstreamError
from radar.providers.meteofrance_auth import AuthError
from radar.providers.meteofrance_auth import MeteoFranceAuth
from radar.providers.meteofrance_render import RenderError

logger = logging.getLogger(__name__)

# Catalog fetch mirrors rainviewer's frames policy; the product download gets a
# longer read timeout (it is ~1.7 MB).
_CATALOG_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
_PRODUCT_TIMEOUT = httpx.Timeout(connect=3.0, read=30.0, write=5.0, pool=5.0)
_CATALOG_BACKOFFS = (0.5, 1.0, 2.0)  # 3 retries
_PRODUCT_BACKOFFS = (0.5, 1.0)  # 2 retries
_BACKOFF_JITTER = 0.25
_MAX_MEMOIZED = 2  # keep only the 2 most-recent rendered frames

_ATTRIBUTION = (
    'Données <a href="https://meteofrance.fr" target="_blank" '
    'rel="noopener">Météo-France</a> (Licence Ouverte 2.0)'
)


def _ms(seconds: float) -> float:
    """Seconds -> milliseconds, one decimal (phases below 1 ms are still readable)."""
    return round(seconds * 1000.0, 1)


def _decode_render(h5_bytes: bytes, bufr_bytes: bytes | None, ts: int) -> dict:
    """Parse + render one frame (CPU-bound; the provider runs it in a thread).

    ``bufr_bytes`` is the optional REFLECTIVITE product. It is best-effort by design:
    a decode failure degrades to the byte-identical rain-only render rather
    than losing the frame, because LAME_D_EAU is the accurate rain and the wash is
    only an atmospheric hint. See :func:`_decode_wash`.

    This is the only place that sees the render's stages, hence the ``frame_rendered``
    event: the archiver's ``poll_complete.duration_ms`` bundles the catalog GET, both
    product downloads, this whole call and the disk writes of every frame that poll
    archived, so it can never say what the wash costs. That answer is
    ``wash_decode_ms + wash_pyramid_ms + wash_px_ms`` (plus whatever share of
    ``encode_ms`` the extra non-empty tiles account for).
    """
    t0 = time.perf_counter()
    grid = meteofrance_render.parse_grid(h5_bytes)
    t1 = time.perf_counter()
    wash = _decode_wash(bufr_bytes, ts)
    t2 = time.perf_counter()
    phases: dict[str, float] = {}
    rendered = meteofrance_render.render_composite_frame(grid, wash, phases=phases)
    emit(
        logger,
        logging.INFO,
        "frame_rendered",
        provider=MeteoFranceProvider.name,
        ts=ts,
        wash=wash is not None,
        parse_ms=_ms(t1 - t0),
        wash_decode_ms=_ms(t2 - t1),
        rain_pyramid_ms=_ms(phases["rain_pyramid"]),
        wash_pyramid_ms=_ms(phases["wash_pyramid"]),
        tiles_ms=_ms(phases["tiles"]),
        wash_px_ms=_ms(phases["wash_px"]),
        encode_ms=_ms(phases["encode"]),
        total_ms=_ms(time.perf_counter() - t0),
        tiles=sum(png is not None for png in rendered.values()),
    )
    return rendered


def _decode_wash(bufr_bytes: bytes | None, ts: int):
    """Decode the reflectivity mosaic, or ``None`` if anything at all goes wrong."""
    if not bufr_bytes:
        return None
    # Imported lazily so a deployment with the wash disabled never loads it.
    from radar.providers import bufr_decode  # noqa: PLC0415

    try:
        grid = bufr_decode.decode(bufr_bytes)
    except bufr_decode.BufrDecodeError as exc:
        logger.warning("Météo-France reflectivity decode failed, rendering rain only: %s", exc)
    except Exception:
        logger.exception("Météo-France reflectivity decode raised, rendering rain only")
    else:
        # Second, independent staleness check: the catalog's validity_time is upstream's
        # *claim*, this is the message's own section-1 nominal time. A catalog entry that
        # advertises a fresh validity while serving a stale product would pass the first
        # check and fail here. Only the one live frame behind the pinned fixture confirms
        # the two agree, hence the full-frame tolerance rather than an exact match.
        if _wash_skew_ok(grid.timestamp, ts, "payload"):
            return grid
    return None


def _wash_skew_ok(wash_ts: int | None, ts: int | None, source: str) -> bool:
    """Whether a reflectivity timestamp is close enough to the rain frame to composite.

    Neither side is a hard error — an unknown timestamp is accepted (we have nothing to
    compare against) and a skewed one only drops the wash, never the frame.
    """
    if wash_ts is None or ts is None:
        return True
    skew = abs(wash_ts - ts)
    if skew > settings.METEOFRANCE_REFLECTIVITY_MAX_SKEW:
        logger.warning(
            "Météo-France reflectivity %s is %ss from frame %s, rendering rain only",
            source,
            skew,
            ts,
        )
        return False
    return True


class MeteoFranceProvider:
    name = "meteofrance"

    def __init__(self) -> None:
        self._auth = MeteoFranceAuth()
        # The single latest frame the last get_frames() saw (upstream is latest-only).
        self._latest_ts: int | None = None
        self._product_ref: str | None = None
        # Single-flight rendered-frame memo, keyed by ts; loop-bound like the lock.
        self._tasks: dict[int, asyncio.Task] = {}
        # Negative memo: ts -> (monotonic deadline, the failure). Deliberately NOT
        # loop-bound — a monotonic deadline outlives a loop rebuild, and forgetting a
        # very recent failure is exactly what re-triggers the download storm.
        self._failures: dict[int, tuple[float, BaseException]] = {}
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    @property
    def frame_interval(self) -> int:
        return settings.METEOFRANCE_FRAME_INTERVAL

    def attribution(self) -> str:
        return _ATTRIBUTION

    # -- frames (latest-only description) -------------------------------------

    async def get_frames(self) -> list[Frame]:
        """Return the single latest frame from the observation description.

        60 s Redis-cached like RainViewer; a cache miss fetches with a Bearer token
        (401 ⇒ invalidate + one retry). Both a fetch exhaustion *and* a 200 catalog
        that yields no usable product raise ``FramesUnavailable`` — see the empty-parse
        branch below for why the latter must not be a silent success.
        """
        key = cache.frames_key(self.name)
        cached = await cache.get_bytes(key)
        if cached is not None:
            frames = self._parse_frames(cached)
            if frames:
                return frames  # a cached body that no longer parses ⇒ refetch below

        body = await self._fetch_description()
        if body is None:
            cached = await cache.get_bytes(key)  # another worker may have filled it
            if cached is not None:
                frames = self._parse_frames(cached)
                if frames:
                    return frames
            raise FramesUnavailable
        frames = self._parse_frames(body)
        if not frames:
            # A 200 catalog that parses to no frame — unparseable JSON, no maille=
            # product link, or no validity_time — is not a valid latest-only state
            # (upstream schema drift or a transient bad body). Surfacing it as
            # FramesUnavailable makes poll_radar count a failure and open a gap after
            # GAP_OPEN_AFTER_FAILURES, instead of resetting the failure counter and
            # reporting a healthy poll while archiving nothing indefinitely.
            raise FramesUnavailable
        # Cache only a body we could actually use, so an unusable catalog is retried
        # on the next poll rather than pinned (and re-served empty) for the TTL.
        await cache.set_bytes(key, body, settings.FRAMES_CACHE_TTL)
        return frames

    def _description_url(self, observation: str | None = None) -> str:
        return (
            f"{settings.METEOFRANCE_API_BASE_URL}/mosaiques/"
            f"{settings.METEOFRANCE_ZONE}/observations/"
            f"{observation or settings.METEOFRANCE_OBSERVATION}"
        )

    async def _fetch_description(self, observation: str | None = None) -> bytes | None:
        url = self._description_url(observation)
        async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT) as client:
            forced_refresh = False
            for attempt in range(len(_CATALOG_BACKOFFS) + 1):
                try:
                    token = await self._auth.get_token()
                    resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                except AuthError:
                    logger.warning("Météo-France token unavailable for catalog fetch")
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    logger.warning("Météo-France catalog fetch error: %s", exc)
                else:
                    if resp.status_code == httpx.codes.OK:
                        return resp.content
                    if resp.status_code == httpx.codes.UNAUTHORIZED and not forced_refresh:
                        await self._auth.invalidate()  # expired token → one forced retry
                        forced_refresh = True
                        continue
                    logger.warning("Météo-France catalog -> HTTP %s", resp.status_code)
                    if resp.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
                        return None  # a non-401 4xx will not fix on retry
                if attempt < len(_CATALOG_BACKOFFS):
                    jitter = random.uniform(0.0, _BACKOFF_JITTER)  # noqa: S311 (jitter, not crypto)
                    await asyncio.sleep(_CATALOG_BACKOFFS[attempt] + jitter)
        return None

    def _parse_frames(self, body: bytes) -> list[Frame]:
        """Parse the LAME_D_EAU catalog and remember the frame this instance can serve."""
        found = self._parse_product_link(body, settings.METEOFRANCE_MAILLE)
        if found is None:
            return []
        ts, abs_url = found
        self._latest_ts = ts
        self._product_ref = abs_url
        return [Frame(timestamp=ts, ref=abs_url)]

    def _parse_product_link(self, body: bytes, maille: int) -> tuple[int, str] | None:
        """Find the ``maille=`` product link + validity time in a catalog body.

        Parameterised on the mesh so the REFLECTIVITE catalog can reuse it without
        touching the primary product's ``_latest_ts`` / ``_product_ref``.
        """
        try:
            data = json.loads(body)
        except ValueError, TypeError:
            logger.warning("Météo-France catalog: unparseable JSON")
            return None
        for link in data.get("links") or []:
            href = link.get("href", "")
            if "/produit" not in href or f"maille={maille}" not in href:
                continue
            abs_url = self._resolve_href(href)
            if abs_url is None:
                logger.warning("Météo-France catalog: product href rejected (SSRF guard)")
                continue
            ts = _parse_iso(link.get("validity_time"))
            if ts is None:
                continue
            return ts, abs_url
        logger.warning("Météo-France catalog: no maille=%s product link", maille)
        return None

    @staticmethod
    def _resolve_href(href: str) -> str | None:
        """Resolve a catalog href to an absolute URL, validated against the API base.

        Catalog hrefs are either absolute (``http…``) or a path appended to the API
        base. SSRF guard: a tampered catalog must not steer our
        server-side fetch, so the resolved URL must sit under
        ``METEOFRANCE_API_BASE_URL`` — anything else (another host, an escaped path)
        is rejected.
        """
        if not href:
            return None
        base = settings.METEOFRANCE_API_BASE_URL
        abs_url = href if href.startswith("http") else base + href
        return abs_url if abs_url.startswith(base.rstrip("/") + "/") else None

    # -- tiles (single-flight per ts) -----------------------------------------

    def tile_client(self) -> httpx.AsyncClient:
        """A client for the product download (the archiver opens one per frame)."""
        return httpx.AsyncClient(timeout=_PRODUCT_TIMEOUT)

    async def get_tile(
        self,
        ts: int,
        z: int,
        x: int,
        y: int,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> bytes | None:
        """Return one tile's PNG (None = empty), fetching+rendering the frame once.

        The 62 tiles of a frame share a single memoized fetch+decode+render task
        keyed by ``ts``. A ``ts`` that is no longer the upstream's latest
        product can't be fetched (latest-only), so this returns ``None`` for it.

        The renderable ``ts``/``ref`` come from ``get_frames`` on *this* instance, so
        the web-container fallback only serves tiles for a frame this worker has
        polled; misses fall through (``None``). That is fine because the archiver
        persists every frame to disk — the fallback is a rare cache-miss path, not
        the primary tile source.
        """
        if ts != self._latest_ts or self._product_ref is None:
            return None
        frame = await self._frame_for(ts, self._product_ref, client)
        return frame.get((z, x, y))

    def _lock_for_loop(self) -> asyncio.Lock:
        """Loop-bound lock (rebuilt on loop change); resets the memo on a new loop."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
            self._tasks = {}  # tasks from a previous loop are dead
        return self._lock

    async def _frame_for(self, ts: int, ref: str, client: httpx.AsyncClient | None) -> dict:
        """Return the rendered {(z,x,y): png|None} frame, single-flight per ts."""
        async with self._lock_for_loop():
            self._raise_if_cooling_down(ts)
            task = self._tasks.get(ts)
            if task is None:
                task = asyncio.create_task(self._render_frame_task(ts, ref, client))
                self._tasks[ts] = task
                task.add_done_callback(lambda t, k=ts: self._note_outcome(k, t))
                self._prune_memos()
        # Await OUTSIDE the lock so all 62 callers run concurrently on the one task.
        return await task

    def _raise_if_cooling_down(self, ts: int) -> None:
        """Re-raise a very recent failure for ``ts`` instead of re-fetching the frame.

        A failed task cannot simply stay memoized — that would poison the frame for
        its whole latest-only window. But evicting it *immediately* was worse: the
        archiver admits the 62 tiles through a TILE_FETCH_CONCURRENCY-wide semaphore,
        so they arrive in ~8 waves, and each wave found the memo empty and started a
        fresh download with its own full retry budget — 24 product downloads for one
        failing frame, and ~8x the wall-clock, pushing a frame past its own cadence.

        A short cooldown collapses the waves onto one outcome while still letting the
        next poll (a minute later, well past the window) genuinely retry.
        """
        found = self._failures.get(ts)
        if found is None:
            return
        deadline, exc = found
        if time.monotonic() >= deadline:
            del self._failures[ts]  # lapsed — the next caller may retry for real
            return
        raise TileUpstreamError from exc

    def _note_outcome(self, ts: int, task: asyncio.Task) -> None:
        """Drop a still-current memo entry whose task failed, and remember the failure.

        Cancelled counts as failed. Successful frames stay memoized as-is.
        """
        if not (task.cancelled() or task.exception() is not None):
            return
        if self._tasks.get(ts) is task:
            del self._tasks[ts]
        exc = asyncio.CancelledError() if task.cancelled() else task.exception()
        self._failures[ts] = (
            time.monotonic() + settings.METEOFRANCE_FAILURE_COOLDOWN,
            exc,
        )

    def _prune_memos(self) -> None:
        for stale in sorted(self._tasks)[:-_MAX_MEMOIZED]:
            del self._tasks[stale]
        for stale in sorted(self._failures)[:-_MAX_MEMOIZED]:
            del self._failures[stale]

    async def _render_frame_task(self, ts: int, ref: str, client: httpx.AsyncClient | None) -> dict:
        try:
            t0 = time.perf_counter()
            if client is not None:
                h5_bytes, bufr_bytes = await self._download_products(client, ref, ts)
            else:
                async with self.tile_client() as own:
                    h5_bytes, bufr_bytes = await self._download_products(own, ref, ts)
            # The other half of a frame's cost; pairs with ``frame_rendered`` so the
            # poll's duration_ms decomposes into download vs CPU without guesswork.
            emit(
                logger,
                logging.INFO,
                "frame_downloaded",
                provider=self.name,
                ts=ts,
                download_ms=_ms(time.perf_counter() - t0),
                rain_bytes=len(h5_bytes),
                wash_bytes=len(bufr_bytes) if bufr_bytes else 0,
            )
            return await asyncio.to_thread(_decode_render, h5_bytes, bufr_bytes, ts)
        except (RenderError, AuthError) as exc:
            raise TileUpstreamError from exc  # chain into the Protocol exception

    async def _download_products(
        self, client: httpx.AsyncClient, ref: str, ts: int
    ) -> tuple[bytes, bytes | None]:
        """Fetch the rain product and (when enabled) the reflectivity one, concurrently.

        Two downloads per frame, still exactly once per ``ts`` — both arms sit inside
        the single-flight memo, so 62 concurrent tile requests trigger one pair.

        The rain arm's failures propagate (it *is* the frame). The wash arm is
        best-effort: ``return_exceptions=True`` plus the broad catch below means a
        Météo-France reflectivity outage, an auth failure on that catalog, or a schema
        change degrades the frame to rain-only output instead of losing it.

        Best-effort has to cover *slowness* as well as failure, hence the deadline: the
        wash arm owns a retry budget of its own and the rain arm waits on it, so an
        unbounded arm would let a merely sluggish REFLECTIVITE endpoint stall the frame
        far past its 5-minute cadence. The ``TimeoutError`` lands in the same branch as
        every other reflectivity failure.
        """
        if not settings.METEOFRANCE_REFLECTIVITY_ENABLED:
            return await self._download_product(client, ref), None
        rain, wash = await asyncio.gather(
            self._download_product(client, ref),
            self._fetch_reflectivity(client, ts),
            return_exceptions=True,
        )
        if isinstance(rain, BaseException):
            raise rain
        if isinstance(wash, BaseException):
            logger.warning("Météo-France reflectivity unavailable: %s", wash)
            wash = None
        return rain, wash

    async def _fetch_reflectivity(self, client: httpx.AsyncClient, ts: int) -> bytes | None:
        """Catalog + download for the REFLECTIVITE mosaic; ``None`` on any miss.

        Time-boxed as a whole, because best-effort has to cover *slowness* as well as
        failure: this arm inherits the rain arm's retry budget (~137 s if every attempt
        times out) and the rain arm waits on it, so without the deadline a merely
        sluggish REFLECTIVITE endpoint would stall the frame far past its 5-minute
        cadence. The ``TimeoutError`` lands in the same branch as every other
        reflectivity failure.

        Deliberately *not* Redis-cached: the wash rides on the rain frame's
        single-flight memo, so caching its catalog would add a second staleness window
        for no gain. The href goes through the same SSRF guard as the rain product.

        The catalog's ``validity_time`` is checked against the rain frame rather than
        discarded: upstream publishes both products on one cadence, so a REFLECTIVITE
        entry that stops advancing means the mosaic has stalled — and compositing a
        stalled wash would write hours-old moisture into the archive under a current
        timestamp, indistinguishably, because the two layers are flattened into one tile.

        A 401 here must *not* invalidate the shared token: this arm and the rain arm
        run concurrently off one :class:`MeteoFranceAuth`, and an application ID that
        simply isn't subscribed to REFLECTIVITE would otherwise churn the rain arm's
        credentials on every single frame.
        """
        async with asyncio.timeout(settings.METEOFRANCE_REFLECTIVITY_DEADLINE):
            body = await self._fetch_description(settings.METEOFRANCE_REFLECTIVITY_OBSERVATION)
            if body is None:
                return None
            found = self._parse_product_link(body, settings.METEOFRANCE_REFLECTIVITY_MAILLE)
            if found is None:
                return None
            wash_ts, abs_url = found
            if not _wash_skew_ok(wash_ts, ts, "catalog"):
                return None
            return await self._download_product(client, abs_url, invalidate_on_401=False)

    async def _download_product(
        self, client: httpx.AsyncClient, ref: str, *, invalidate_on_401: bool = True
    ) -> bytes:
        forced_refresh = False
        for attempt in range(len(_PRODUCT_BACKOFFS) + 1):
            try:
                token = await self._auth.get_token()
                resp = await client.get(ref, headers={"Authorization": f"Bearer {token}"})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.warning("Météo-France product fetch error: %s", exc)
            else:
                if resp.status_code == httpx.codes.OK:
                    return resp.content
                if (
                    resp.status_code == httpx.codes.UNAUTHORIZED
                    and invalidate_on_401
                    and not forced_refresh
                ):
                    await self._auth.invalidate()
                    forced_refresh = True
                    continue
                logger.warning("Météo-France product -> HTTP %s", resp.status_code)
                if resp.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
                    break  # a non-401 4xx will not fix on retry
            if attempt < len(_PRODUCT_BACKOFFS):
                delay = _PRODUCT_BACKOFFS[attempt] + random.uniform(0.0, _BACKOFF_JITTER)  # noqa: S311
                await asyncio.sleep(delay)
        raise TileUpstreamError


def _parse_iso(value: object) -> int | None:
    """ISO-8601 UTC validity_time -> epoch seconds; None if absent/unparseable."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())
