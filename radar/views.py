"""HTTP endpoints. Async, provider-agnostic — views call only the
RadarProvider interface, the Redis cache, and the archive models, never a
specific upstream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC
from datetime import datetime
from urllib.parse import urlparse

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Max
from django.db.models import Min
from django.db.models import Q
from django.http import FileResponse
from django.http import HttpRequest
from django.http import HttpResponse
from django.http import JsonResponse
from django.http import StreamingHttpResponse
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt

from radar import cache
from radar import metrics as metrics_text
from radar import stats as stats_mod
from radar import storage
from radar import tiles
from radar.lightning import get_active_source as get_active_lightning_source
from radar.logging_json import emit
from radar.models import ArchiveGap
from radar.models import LightningStrike
from radar.models import PushSubscription
from radar.models import RadarFrame
from radar.providers import enabled_providers
from radar.providers import get_provider
from radar.providers.base import FramesUnavailable
from radar.providers.base import TileUpstreamError

logger = logging.getLogger("radar.views")

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"

# Human labels for the frames `providers` advert — proper nouns, not localized.
_PROVIDER_LABELS = {"rainviewer": "RainViewer", "meteofrance": "Météo-France (beta)"}

# The JSON API responses carry no content hash, so a browser must never reuse one
# without checking back. ``@no_cache`` (Cache-Control: no-cache) lets the response
# be stored but forces revalidation every load — keeping the live timeline, archive
# bounds, and stats provably fresh rather than relying on heuristic caching. Applied
# to every frontend-facing JSON view below (the SSE stream sets its own header).
no_cache = cache_control(no_cache=True)

_LAT_MAX = 90.0
_LON_MAX = 180.0
_BBOX_PARTS = 4


# -- provider selection -------------------------------------------------------


def _provider_from_request(request: HttpRequest):
    """Resolve ``?provider=`` (absent ⇒ default); (provider, None) or (None, 400).

    An unknown/disabled name yields a 400 ``{"error": "unknown_provider"}`` so the
    browser never steers us to a source we don't serve.
    """
    name = request.GET.get("provider") or settings.RADAR_PROVIDER
    if name not in enabled_providers():
        return None, JsonResponse({"error": "unknown_provider"}, status=400)
    return get_provider(name), None


def _providers_advert() -> list[dict]:
    """The `providers` advert: every enabled source's name/label/attribution/interval."""
    advert = []
    for name in enabled_providers():
        provider = get_provider(name)
        advert.append(
            {
                "name": name,
                "label": _PROVIDER_LABELS.get(name, name),
                "attribution": provider.attribution(),
                "frame_interval": provider.frame_interval,
            },
        )
    return advert


# -- frames -------------------------------------------------------------------


@no_cache
async def frames(request: HttpRequest) -> HttpResponse:
    """GET /api/radar/frames — live window or ?from=&to= historical query."""
    provider, err = _provider_from_request(request)
    if err is not None:
        return err
    raw_from = request.GET.get("from")
    raw_to = request.GET.get("to")
    if raw_from is not None or raw_to is not None:
        return await _frames_historical(provider, raw_from, raw_to)
    return await _frames_live(provider)


async def _frames_live(provider) -> HttpResponse:
    # Micro-cache the assembled live response: every client requests it on page
    # load and on the periodic refresh, yet it only changes when a new frame
    # lands (~10 min) or a gap opens. A short Redis TTL makes the two Postgres
    # queries below independent of visitor count. Best-effort on both sides —
    # a Redis hiccup falls through to the normal build.
    live_key = cache.frames_live_key(provider.name)
    with contextlib.suppress(Exception):
        cached = await cache.get_bytes(live_key)
        if cached is not None:
            return HttpResponse(cached, content_type="application/json")

    now = int(datetime.now(tz=UTC).timestamp())
    window_start = now - settings.LIVE_WINDOW_SECONDS
    try:
        ts_list = await _frame_timestamps(provider.name, window_start, now)
        gaps = await _gaps_overlapping(provider.name, window_start, now)
    except Exception:  # noqa: BLE001 — DB hiccup: degrade to the live provider
        ts_list, gaps = [], []

    if not ts_list:
        # Cold start / archiver lagging: fall back so LIVE never goes blank.
        try:
            frame_list = await provider.get_frames()
        except FramesUnavailable:
            return JsonResponse({"error": "frames_unavailable"}, status=503)
        ts_list = [f.timestamp for f in frame_list]

    resp = _frames_response(provider, ts_list, gaps)
    with contextlib.suppress(Exception):  # caching is best-effort
        await cache.set_bytes(live_key, resp.content, settings.FRAMES_LIVE_CACHE_TTL)
    return resp


async def _frames_historical(provider, raw_from, raw_to) -> JsonResponse:
    try:
        t_from = int(raw_from)
        t_to = int(raw_to)
    except TypeError, ValueError:
        return JsonResponse({"error": "invalid_range"}, status=400)
    if t_from > t_to or (t_to - t_from) > settings.MAX_QUERY_SPAN_SECONDS:
        return JsonResponse({"error": "range_too_large"}, status=400)
    try:
        ts_list = await _frame_timestamps(provider.name, t_from, t_to)
        gaps = await _gaps_overlapping(provider.name, t_from, t_to)
    except Exception:  # noqa: BLE001 — DB unreachable for an archive query
        return JsonResponse({"error": "archive_unavailable"}, status=503)
    return _frames_response(provider, ts_list, gaps)


def _frames_response(provider, ts_list, gaps) -> JsonResponse:
    return JsonResponse(
        {
            "frames": [{"timestamp": ts} for ts in ts_list],
            "provider": provider.name,
            "attribution": provider.attribution(),
            # Provider advert: every enabled source, so the frontend can offer
            # the "Source du radar" switch and read each source's cadence/attribution.
            "providers": _providers_advert(),
            "gaps": gaps,
            # Radar coverage [S, N, W, E]; the frontend bounds the tile layer to
            # it so Leaflet never requests tiles outside the matrix.
            "bbox": list(settings.RADAR_BBOX),
            # Additive lightning advert: the frontend learns the layer is
            # available + its attribution/bbox/window without a second request.
            # Radar-only callers ignore it. `enabled` reflects LIGHTNING_ENABLED so
            # the toggle is hidden entirely when the backend isn't serving the layer.
            "lightning": {
                "enabled": settings.LIGHTNING_ENABLED,
                "attribution": get_active_lightning_source().attribution(),
                "bbox": list(settings.LIGHTNING_BBOX),
                "display_hours": settings.LIGHTNING_DISPLAY_HOURS,
                # Background push advert: the frontend learns whether it
                # can register a Web Push subscription and with which VAPID key. Off ⇒
                # the frontend silently stays foreground-only. Clients that predate
                # background alerts, and radar-only ones, ignore it.
                "push": {
                    "enabled": settings.PUSH_ALERTS_ENABLED and bool(settings.VAPID_PUBLIC_KEY),
                    "vapid_public_key": settings.VAPID_PUBLIC_KEY,
                },
            },
        },
    )


async def _frame_timestamps(provider_name: str, t_from: int, t_to: int) -> list[int]:
    return [
        ts
        async for ts in RadarFrame.objects.filter(
            provider=provider_name,
            timestamp__gte=t_from,
            timestamp__lte=t_to,
        )
        .order_by("timestamp")
        .values_list("timestamp", flat=True)
    ]


async def _gaps_overlapping(provider_name: str, t_from: int, t_to: int) -> list[dict]:
    start_dt = datetime.fromtimestamp(t_from, tz=UTC)
    end_dt = datetime.fromtimestamp(t_to, tz=UTC)
    qs = (
        ArchiveGap.objects.filter(service="radar", provider=provider_name, gap_start__lte=end_dt)
        .filter(Q(gap_end__isnull=True) | Q(gap_end__gte=start_dt))
        .order_by("gap_start")
    )
    return [
        {
            "start": int(gap.gap_start.timestamp()),
            "end": int(gap.gap_end.timestamp()) if gap.gap_end else None,
        }
        async for gap in qs
    ]


# -- latest -------------------------------------------------------------------


@no_cache
async def latest(request: HttpRequest) -> JsonResponse:
    """GET /api/radar/latest — newest live frame timestamp."""
    provider, err = _provider_from_request(request)
    if err is not None:
        return err
    try:
        frame_list = await provider.get_frames()
    except FramesUnavailable:
        return JsonResponse({"error": "frames_unavailable"}, status=503)
    if not frame_list:
        return JsonResponse({"error": "frames_unavailable"}, status=503)
    return JsonResponse({"timestamp": frame_list[-1].timestamp})


# -- range --------------------------------------------------------------------


@no_cache
async def range_(request: HttpRequest) -> HttpResponse:
    """GET /api/radar/range — per-provider archive bounds for the date picker."""
    provider, err = _provider_from_request(request)
    if err is not None:
        return err
    cached = await cache.get_range_cache(provider.name)
    if cached is not None:
        return HttpResponse(cached, content_type="application/json")
    try:
        bounds = await RadarFrame.objects.filter(provider=provider.name).aaggregate(
            lo=Min("timestamp"),
            hi=Max("timestamp"),
        )
    except Exception:  # noqa: BLE001
        return JsonResponse({"error": "archive_unavailable"}, status=503)
    body = json.dumps({"earliest": bounds["lo"], "latest": bounds["hi"]}).encode()
    await cache.set_range_cache(provider.name, body, settings.ARCHIVE_RANGE_CACHE_TTL)
    return HttpResponse(body, content_type="application/json")


# -- tile ---------------------------------------------------------------------


def _tile_file_response(handle) -> FileResponse:
    resp = FileResponse(handle, content_type="image/png")
    resp["Cache-Control"] = IMMUTABLE_CACHE
    return resp


def _tile_unavailable() -> HttpResponse:
    """503 for a tile whose archive lookup could not be answered — never cached.

    The deliberate counterpart of :func:`_tile_no_content`. When the row lookup times
    out or the DB is unreachable we do not know whether this tile is empty, so 204
    would be a lie — and an ``immutable`` one, pinning a blank tile in every visitor's
    browser cache for a year. ``no-store`` + 503 makes Leaflet raise ``tileerror``,
    leave the tile blank, and ask again next visit.
    """
    resp = HttpResponse(status=503)
    resp["Cache-Control"] = "no-store"
    return resp


def _tile_no_content() -> HttpResponse:
    """204 for a tile with nothing to draw — cached as hard as a real tile.

    A published frame is immutable upstream, so "this tile is empty" is exactly as
    permanent a fact as its bytes would have been, and it deserves the same
    ``immutable`` header. Without it the browser re-asked on every pan, zoom and
    revisit — and for a provider that persists only non-empty tiles (Météo-France)
    that is most of the viewport, every time.
    """
    resp = HttpResponse(status=204)
    resp["Cache-Control"] = IMMUTABLE_CACHE
    return resp


async def _archived_empty(provider: str, ts: int) -> set[tuple[int, int, int]] | None:
    """Tiles this frame was archived with nothing to draw for; ``None`` if unarchived.

    The serving-side counterpart of ``archiver._known_empty``: the archiver records
    every empty tile on the frame row precisely because "no file on disk" cannot tell
    "empty" from "never fetched", and that record is just as usable when *serving* a
    miss as when retrying one. Distinguishes "no row" (``None``) from "row with no
    empty tiles" (an empty set), because the caller needs both answers.
    """
    row = (
        await RadarFrame.objects.filter(provider=provider, timestamp=ts)
        .values_list("empty", flat=True)
        .afirst()
    )
    if row is None:
        return None
    return {(e["z"], e["x"], e["y"]) for e in row}


_archive_sem: asyncio.Semaphore | None = None
_archive_sem_loop: asyncio.AbstractEventLoop | None = None


def _get_archive_semaphore() -> asyncio.Semaphore:
    """Loop-bound cap on *simultaneous* archive-row lookups (rebuilt on loop change).

    Mirrors :func:`radar.cache.get_client` and ``alerts.webpush._get_semaphore``: an
    asyncio primitive belongs to the loop that created it, and WSGI ``runserver`` makes
    a fresh loop per request.

    Why the tile view needs one at all: Django's ASGI handler runs each request inside
    its own ``ThreadSensitiveContext`` — a single-worker thread pool per request — and a
    Django DB connection is thread-local. So N concurrent tile misses are N concurrent
    Postgres connections, and nothing upstream of here bounds N (``/tiles/…`` is
    deliberately unthrottled in Nginx). This is the bound.
    """
    global _archive_sem, _archive_sem_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _archive_sem is None or _archive_sem_loop is not loop:
        _archive_sem = asyncio.Semaphore(settings.TILE_ARCHIVE_LOOKUP_CONCURRENCY)
        _archive_sem_loop = loop
    return _archive_sem


def _emit_tile_fallback(  # noqa: PLR0913, PLR0917 — provider + tile coords + result are the fields
    provider: str,
    ts: int,
    z: int,
    x: int,
    y: int,
    result: str,
    level: int,
) -> None:
    emit(logger, level, "tile_fallback", provider=provider, ts=ts, z=z, x=x, y=y, result=result)


async def tile(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0917 — coords + the miss ladder
    _request: HttpRequest,
    provider: str,
    date: str,
    ts: int,
    z: int,
    x: int,
    y: int,
) -> HttpResponse:
    """GET /tiles/{provider}/{date}/{ts}/{z}/{x}/{y}.png — disk, then archive, then provider."""
    # 0. Only serve enabled providers (an unknown/disabled name is a 404, not a fetch).
    if provider not in enabled_providers():
        return HttpResponse(status=404)
    # 1. Validate params (int converters enforce numerics; check ranges + date).
    if not tiles.is_valid_tile(z, x, y, settings.RADAR_ZOOM_MIN, settings.RADAR_ZOOM_MAX):
        return HttpResponse(status=404)
    if not storage.DATE_DIR_RE.match(date) or date != storage.utc_date(ts):
        return HttpResponse(status=404)
    # Restrict to the computed coverage matrix: an in-grid tile outside France
    # must never trigger an upstream fetch + on-disk write (storage/upstream
    # abuse — only the 62 matrix tiles are ever archived).
    matrix = tiles.matrix_frozenset(
        tuple(settings.RADAR_BBOX),
        settings.RADAR_ZOOM_MIN,
        settings.RADAR_ZOOM_MAX,
    )
    if (z, x, y) not in matrix:
        return HttpResponse(status=404)

    # 2. Disk hit -> sendfile with the immutable header (no DB/upstream needed).
    #    In prod Nginx serves this directly; Django only sees genuine misses.
    path = storage.tile_path(provider, ts, z, x, y, date=date)
    if await sync_to_async(path.is_file)():
        return _tile_file_response(await sync_to_async(path.open)("rb"))

    # 3. Miss -> settle it from the archive before ever asking upstream. A frame is
    #    immutable once published, so a tile the archiver already found empty is
    #    empty forever. This is what keeps a sparse-archive provider off the expensive
    #    path below: Météo-France persists only non-empty tiles, so on a quiet day every
    #    tile in the viewport misses the static cache — and for the *newest* frame
    #    each of those used to re-download the ~1.7 MB product and re-render all 62
    #    tiles inside the web container. Leaflet's layer `load` waits for the
    #    slowest tile, so that render was the frontend's whole cross-fade delay.
    #
    # 3a. Redis first. `status='ok'` means every matrix tile was attempted and none
    #     errored, so each one is either on disk or in the row's `empty` list — and the
    #     archived set holds exactly the ok frames. "In the set, not on disk" therefore
    #     already answers "nothing to draw", with no row lookup at all. That is the
    #     whole of historical navigation: replaying an archived day used to cost one
    #     Postgres connection per missing tile, which is how the fallback exhausted
    #     max_connections and 500'd. A Redis flush just misses here and falls through to
    #     3b (poll_radar's cold-start branch rebuilds the set on the next poll).
    #     Best-effort, like every other cache read here: a Redis hiccup must degrade to
    #     3b, never 500 the tile — that failure mode is the whole point of this change.
    archived_frame = False
    with contextlib.suppress(Exception):
        archived_frame = await cache.is_archived(provider, ts)
    if archived_frame:
        # DEBUG for the same reason as `archived_empty` below: the common answer on a
        # sparse archive, and a pure cache hit.
        _emit_tile_fallback(provider, ts, z, x, y, "archived_frame", logging.DEBUG)
        return _tile_no_content()

    # 3b. Not fully archived (the live frame, or a partial one still being retried)
    #     -> the row, with one indexed query. Bounded and time-boxed: unlike every
    #     other view's DB access this one is reached once per *tile*, so it is the one
    #     place a client fan-out translates 1:1 into Postgres connections.
    try:
        async with asyncio.timeout(settings.TILE_ARCHIVE_LOOKUP_TIMEOUT):
            async with _get_archive_semaphore():
                empty = await _archived_empty(provider, ts)
    except Exception:  # noqa: BLE001 — DB unreachable/slow: shed, never 500 the tile
        # Both halves land here on purpose: a TimeoutError (we queued too long) and a
        # DB error are the same answer to the caller — we cannot say whether this tile
        # is empty, so we must not pretend to. 503, uncached.
        _emit_tile_fallback(provider, ts, z, x, y, "db_unavailable", logging.ERROR)
        return _tile_unavailable()
    if empty is not None and (z, x, y) in empty:
        # DEBUG, not INFO: this is now the common answer for a sparse archive, and
        # it is a cheap cache hit — logging it per tile per client would be pure noise.
        _emit_tile_fallback(provider, ts, z, x, y, "archived_empty", logging.DEBUG)
        return _tile_no_content()

    # 4. Still a miss -> we need upstream. Fetch the live frame index first (cheap:
    #    60s Redis cache, kept warm by the archiver) both to validate ts and to give
    #    the provider this frame's opaque ref so get_tile builds the right URL.
    provider_obj = get_provider(provider)
    try:
        frame_list = await provider_obj.get_frames()
    except FramesUnavailable:
        frame_list = []
    live_ts = {f.timestamp for f in frame_list}
    if ts not in live_ts:
        # Unknown ts -> 404. Known (archived) but aged out of the upstream window
        # with no tile on disk, and not on the row's empty list either: a tile that
        # errored (it is in `missing`) and can never be retried now that the frame
        # has left the backfill window, or a rare write skip. 204 is still the best
        # answer — the region is simply not renderable — and it is permanent, so it
        # caches like the rest.
        if empty is None:
            return HttpResponse(status=404)
        _emit_tile_fallback(provider, ts, z, x, y, "gone", logging.INFO)
        return _tile_no_content()

    # 5. Fetch upstream, persist atomically, return with the immutable header.
    try:
        data = await provider_obj.get_tile(ts, z, x, y)
    except TileUpstreamError:
        _emit_tile_fallback(provider, ts, z, x, y, "error", logging.ERROR)
        return HttpResponse(status=502)
    if data is None:
        _emit_tile_fallback(provider, ts, z, x, y, "empty", logging.INFO)
        # 204, not 404: the tile is valid, there is simply no precipitation to draw
        # here (providers like Météo-France persist only non-empty tiles). A
        # 2xx keeps the browser console clean — a 4xx would be logged as an error —
        # while Leaflet renders the empty body as a blank tile exactly as before.
        return _tile_no_content()  # legitimate empty region
    await sync_to_async(storage.write_tile)(provider, ts, z, x, y, data, date=date)
    _emit_tile_fallback(provider, ts, z, x, y, "fetched", logging.INFO)
    resp = HttpResponse(data, content_type="image/png")
    resp["Cache-Control"] = IMMUTABLE_CACHE
    return resp


# -- lightning: SSE live stream -----------------------------------------------


def _sse_frame(strike_json: bytes) -> str:
    """One SSE ``event: strike`` frame carrying the raw strike JSON."""
    return f"event: strike\ndata: {strike_json.decode()}\n\n"


async def _lightning_event_stream():
    """Replay the recent buffer, then forward Redis pub/sub strikes live.

    Touches only Redis — never holds a DB connection open for the stream's life.
    A comment heartbeat keeps the connection alive through proxies and surfaces
    dead clients. On client disconnect the ASGI server cancels this generator and
    the ``finally`` block tears down the subscription.
    """
    # 1. Replay recent activity so a fresh client sees the current storm at once.
    cutoff = time.time() - settings.LIGHTNING_RECENT_SECONDS
    for raw in await cache.recent_strikes():
        try:
            obj = json.loads(raw)
        except ValueError, TypeError:
            continue
        if obj.get("time", 0) >= cutoff:
            yield _sse_frame(raw)

    # 2. Live tail.
    pubsub = cache.pubsub()
    await pubsub.subscribe(cache.LIGHTNING_CHANNEL)
    try:
        while True:
            message = await pubsub.get_message(
                timeout=settings.LIGHTNING_SSE_HEARTBEAT_SECONDS,
            )
            if message is None:
                yield ": keepalive\n\n"  # idle heartbeat
                continue
            if message.get("type") != "message":
                continue  # subscribe/unsubscribe confirmations — not a strike
            yield _sse_frame(message["data"])
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(cache.LIGHTNING_CHANNEL)
            await pubsub.aclose()


async def lightning_stream(_request: HttpRequest) -> HttpResponse:
    """GET /api/lightning/stream — Server-Sent-Events live strikes."""
    if not await cache.ping():
        return JsonResponse({"error": "lightning_unavailable"}, status=503)
    resp = StreamingHttpResponse(
        _lightning_event_stream(),
        content_type="text/event-stream",
    )
    resp["Cache-Control"] = "no-cache"
    resp["Connection"] = "keep-alive"
    # Belt-and-suspenders against any buffering proxy (Nginx/Traefik) — also set
    # in the prod Nginx location.
    resp["X-Accel-Buffering"] = "no"
    return resp


# -- lightning: history -------------------------------------------------------


def _parse_bbox(raw: str) -> list[float]:
    """``"S,N,W,E"`` -> ``[S, N, W, E]`` floats; raises ValueError if malformed."""
    parts = [float(p) for p in raw.split(",")]
    if len(parts) != _BBOX_PARTS:
        msg = "bbox needs four comma-separated floats"
        raise ValueError(msg)
    south, north, west, east = parts
    in_range = -_LAT_MAX <= south <= _LAT_MAX and -_LON_MAX <= west <= _LON_MAX
    if south > north or west > east or not in_range:
        msg = "bbox out of range"
        raise ValueError(msg)
    return parts


async def _query_history(t_from: int, t_to: int, bbox) -> tuple[list[dict], bool]:
    south, north, west, east = bbox
    limit = settings.LIGHTNING_HISTORY_MAX_STRIKES
    qs = (
        LightningStrike.objects.filter(
            struck_at__gte=datetime.fromtimestamp(t_from, tz=UTC),
            struck_at__lte=datetime.fromtimestamp(t_to, tz=UTC),
            lat__gte=south,
            lat__lte=north,
            lon__gte=west,
            lon__lte=east,
        )
        .order_by("-struck_at")  # newest first so the cap keeps the MOST RECENT N
        .values_list("lat", "lon", "struck_at", "intensity")
    )
    rows = [row async for row in qs[: limit + 1]]
    truncated = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()  # return oldest -> newest
    strikes = [
        {"lat": lat, "lon": lon, "time": struck_at.timestamp(), "intensity": intensity}
        for (lat, lon, struck_at, intensity) in rows
    ]
    return strikes, truncated


@no_cache
async def lightning_history(request: HttpRequest) -> JsonResponse:
    """GET /api/lightning/history?from=&to=&bbox= — archived strikes."""
    try:
        t_from = int(request.GET["from"])
        t_to = int(request.GET["to"])
    except KeyError, TypeError, ValueError:
        return JsonResponse({"error": "invalid_range"}, status=400)
    if t_from > t_to or (t_to - t_from) > settings.LIGHTNING_HISTORY_MAX_SPAN_SECONDS:
        return JsonResponse({"error": "range_too_large"}, status=400)

    raw_bbox = request.GET.get("bbox")
    try:
        bbox = _parse_bbox(raw_bbox) if raw_bbox else list(settings.LIGHTNING_BBOX)
    except ValueError:
        return JsonResponse({"error": "invalid_bbox"}, status=400)

    try:
        strikes, truncated = await _query_history(t_from, t_to, bbox)
    except Exception:  # noqa: BLE001 — DB unreachable for the archive query
        return JsonResponse({"error": "lightning_unavailable"}, status=503)

    return JsonResponse(
        {
            "strikes": strikes,
            "truncated": truncated,
            "attribution": get_active_lightning_source().attribution(),
        },
    )


# -- storm alerts: Web Push subscription --------------------------------------

_PUSH_BODY_MAX = 8192  # 8 KiB — a subscription payload is tiny; anything larger is abuse
_ENDPOINT_MAX = 1024
_KEY_MAX = 200


def _bad(reason: str) -> JsonResponse:
    return JsonResponse({"error": reason}, status=400)


def _coarsen(x: float) -> float:
    """Round an anchor coordinate to ~1 km (2 decimals) before it is stored."""
    return round(x, 2)


def _endpoint_allowed(url: object) -> bool:
    """True if ``url`` is an https push-service endpoint on the host allow-list.

    Matches on the parsed **hostname** only (never a substring of the whole URL), so a
    path/userinfo like ``https://push.apple.com@evil.com`` or a suffix hidden in the
    path is rejected — the classic SSRF trap.
    """
    if not isinstance(url, str) or not (0 < len(url) <= _ENDPOINT_MAX):
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in settings.PUSH_ENDPOINT_ALLOWED_SUFFIXES
    )


def _push_disabled() -> JsonResponse:
    return JsonResponse({"error": "not_found"}, status=404)


@csrf_exempt
async def alerts_subscribe(request: HttpRequest) -> JsonResponse:  # noqa: C901, PLR0911
    """POST /api/alerts/subscribe — upsert a Web Push subscription.

    csrf-exempt + POST-only: these are unauthenticated, endpoint-keyed rows (CSRF
    protects nothing here), and the CSRF middleware would otherwise 403 before the
    view runs. Validates hard, coarsens the anchor, and upserts by unique endpoint.
    """
    if not settings.PUSH_ALERTS_ENABLED:
        return _push_disabled()
    if request.method != "POST":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    if len(request.body) > _PUSH_BODY_MAX:
        return _bad("too_large")
    try:
        data = json.loads(request.body)
    except ValueError, TypeError:
        return _bad("invalid_json")

    endpoint = data.get("endpoint")
    keys = data.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not _endpoint_allowed(endpoint):
        return _bad("invalid_endpoint")
    if not (isinstance(p256dh, str) and 0 < len(p256dh) <= _KEY_MAX):
        return _bad("invalid_keys")
    if not (isinstance(auth, str) and 0 < len(auth) <= _KEY_MAX):
        return _bad("invalid_keys")
    try:
        lat = float(data.get("lat"))
        lon = float(data.get("lon"))
    except TypeError, ValueError:
        return _bad("invalid_anchor")
    s, n, w, e = settings.LIGHTNING_BBOX
    if not (s <= lat <= n and w <= lon <= e):
        return _bad("out_of_coverage")
    locale = data.get("locale") if data.get("locale") in ("fr", "en") else "en"

    # Cap only *new* endpoints; an existing endpoint may always re-upsert.
    if not await PushSubscription.objects.filter(endpoint=endpoint).aexists():
        if await PushSubscription.objects.acount() >= settings.PUSH_MAX_SUBSCRIPTIONS:
            return JsonResponse({"error": "capacity"}, status=429)

    await PushSubscription.objects.aupdate_or_create(
        endpoint=endpoint,
        defaults={
            "p256dh": p256dh,
            "auth": auth,
            "lat": _coarsen(lat),
            "lon": _coarsen(lon),
            "locale": locale,
        },
    )
    emit(logger, logging.INFO, "push_subscribed", service="alerts", locale=locale)
    return JsonResponse({"ok": True})


@csrf_exempt
async def alerts_unsubscribe(request: HttpRequest) -> JsonResponse:
    """POST /api/alerts/unsubscribe — delete a subscription by endpoint (idempotent)."""
    if not settings.PUSH_ALERTS_ENABLED:
        return _push_disabled()
    if request.method != "POST":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    if len(request.body) > _PUSH_BODY_MAX:
        return _bad("too_large")
    try:
        data = json.loads(request.body)
    except ValueError, TypeError:
        return _bad("invalid_json")
    endpoint = data.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return _bad("invalid_endpoint")
    await PushSubscription.objects.filter(endpoint=endpoint).adelete()
    emit(logger, logging.INFO, "push_unsubscribed", service="alerts")
    return JsonResponse({"ok": True})


# -- stats (About dialog) -----------------------------------------------------


@no_cache
async def stats(request: HttpRequest) -> HttpResponse:
    """GET /api/stats — archive statistics for the About dialog.

    Public, read-only, Redis-cached. Re-shapes counts already gathered for
    /metrics; never 5xxs (a Redis/DB hiccup yields partial data with nulls).
    """
    if request.method != "GET":
        return JsonResponse({"error": "method_not_allowed"}, status=405)

    try:
        cached = await cache.get_stats_json()
    except Exception:  # noqa: BLE001 — Redis miss/hiccup: recompute below
        cached = None
    if cached is not None:
        return HttpResponse(cached, content_type="application/json")

    payload = await stats_mod.gather_stats()
    body = json.dumps(payload).encode()
    with contextlib.suppress(Exception):  # caching is best-effort
        await cache.set_stats_json(body, settings.STATS_CACHE_TTL)
    return HttpResponse(body, content_type="application/json")


# -- metrics ------------------------------------------------------------------


async def metrics(_request: HttpRequest) -> HttpResponse:
    """GET /metrics — Prometheus text exposition.

    Redis-cached (``METRICS_CACHE_TTL``) like /api/stats: the exposition runs a
    dozen aggregates including a full COUNT(*) over the partitioned
    lightning_strike table, and the endpoint is public — without the cache every
    scrape (or hammering client) would hit Postgres directly.
    """
    try:
        cached = await cache.get_metrics_text()
    except Exception:  # noqa: BLE001 — Redis hiccup: recompute below
        cached = None
    if cached is not None:
        return HttpResponse(cached, content_type=metrics_text.CONTENT_TYPE)

    text = await metrics_text.render()
    with contextlib.suppress(Exception):  # caching is best-effort
        await cache.set_metrics_text(text.encode(), settings.METRICS_CACHE_TTL)
    return HttpResponse(text, content_type=metrics_text.CONTENT_TYPE)


# -- health -------------------------------------------------------------------


async def healthz(_request: HttpRequest) -> JsonResponse:
    """GET /healthz — liveness, always 200."""
    return JsonResponse({"status": "ok"})


async def readyz(_request: HttpRequest) -> JsonResponse:
    """GET /readyz — readiness: Redis (required) + DB reachability."""
    if not await cache.ping():
        return JsonResponse({"status": "unavailable"}, status=503)
    try:
        await RadarFrame.objects.aexists()
    except Exception:  # noqa: BLE001 — DB unreachable -> not ready
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
