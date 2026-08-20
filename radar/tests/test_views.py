"""Endpoint contracts. Mocked upstream via respx; no real network."""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime

import httpx
import pytest
import respx
from django.conf import settings
from django.db.utils import OperationalError
from django.test import override_settings

from radar import cache
from radar import storage
from radar import views
from radar.models import ArchiveGap
from radar.models import RadarFrame
from radar.providers import rainviewer

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

# 1683790200 = 2023-05-11 08:50:00 UTC -> date dir 2023-05-11. The tile URL uses
# the frame's opaque hex token (its path in SAMPLE_WEATHER_MAPS), not the epoch.
TS = 1683790200
DATE = "2023-05-11"
TILE_URL = "https://tilecache.rainviewer.com/v2/radar/1a2b3c4d5e6f/256/5/16/11/2/1_1.png"
TILE_PATH = f"/tiles/{DATE}/{TS}/5/16/11.png"


async def _noop(*_args, **_kwargs) -> None:
    pass


@pytest.fixture(autouse=True)
def _tile_root(tmp_path):
    with override_settings(TILE_ROOT=str(tmp_path)):
        yield tmp_path


# -- frames: live -------------------------------------------------------------


@respx.mock
async def test_frames_live_shape_with_gaps(async_client, sample_weather_maps):
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    resp = await async_client.get("/api/radar/frames")
    assert resp.status_code == 200
    # No content hashing -> the JSON must always be revalidated, never reused blind.
    assert resp["Cache-Control"] == "no-cache"
    body = resp.json()
    assert body["provider"] == "rainviewer"
    assert "rainviewer.com" in body["attribution"]
    assert body["gaps"] == []
    assert body["bbox"] == list(settings.RADAR_BBOX)  # frontend bounds the tile layer to it
    # DB empty -> live fallback to the provider window.
    assert body["frames"] == [
        {"timestamp": 1683790200},
        {"timestamp": 1683790800},
        {"timestamp": 1683791400},
    ]


@respx.mock
async def test_frames_unavailable_returns_503(async_client, monkeypatch):
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    respx.get(settings.RAINVIEWER_API_URL).mock(return_value=httpx.Response(500))
    resp = await async_client.get("/api/radar/frames")
    assert resp.status_code == 503
    assert resp.json() == {"error": "frames_unavailable"}


async def test_frames_live_uses_archive_when_present(async_client):
    now = int(datetime.now(tz=UTC).timestamp())
    for ts in (now - 600, now - 300):
        await RadarFrame.objects.acreate(
            timestamp=ts,
            provider="rainviewer",
            tile_count=62,
            status="ok",
            missing=[],
        )
    # No respx mock: the archive must answer without touching upstream.
    resp = await async_client.get("/api/radar/frames")
    assert resp.status_code == 200
    body = resp.json()
    assert [f["timestamp"] for f in body["frames"]] == [now - 600, now - 300]


async def test_frames_live_micro_cached(async_client):
    """The assembled live response is served from Redis within the TTL."""
    now = int(datetime.now(tz=UTC).timestamp())
    await RadarFrame.objects.acreate(
        timestamp=now - 600,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    resp = await async_client.get("/api/radar/frames")
    assert resp.status_code == 200
    assert [f["timestamp"] for f in resp.json()["frames"]] == [now - 600]
    # A frame landing inside the TTL stays invisible until it expires: the
    # second request is answered from the micro-cache, not Postgres.
    await RadarFrame.objects.acreate(
        timestamp=now - 300,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    resp2 = await async_client.get("/api/radar/frames")
    assert resp2.status_code == 200
    assert [f["timestamp"] for f in resp2.json()["frames"]] == [now - 600]
    # Browser-facing semantics are unchanged on the cached path.
    assert resp2["Cache-Control"] == "no-cache"


async def test_frames_conditional_get_returns_304(async_client):
    """An unchanged payload revalidates to an empty 304 via ETag/If-None-Match."""
    now = int(datetime.now(tz=UTC).timestamp())
    await RadarFrame.objects.acreate(
        timestamp=now - 600,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    resp = await async_client.get("/api/radar/frames")
    etag = resp["ETag"]
    assert etag
    resp2 = await async_client.get("/api/radar/frames", headers={"If-None-Match": etag})
    assert resp2.status_code == 304
    assert not resp2.content


# -- frames: historical -------------------------------------------------------


async def test_frames_historical_returns_range_and_gaps(async_client):
    for ts in (1000, 2000, 3000, 9000):
        await RadarFrame.objects.acreate(
            timestamp=ts,
            provider="rainviewer",
            tile_count=62,
            status="ok",
            missing=[],
        )
    await ArchiveGap.objects.acreate(
        service="radar",
        gap_start=datetime.fromtimestamp(1500, tz=UTC),
        gap_end=datetime.fromtimestamp(1800, tz=UTC),
        reason="test",
        detail={},
    )
    resp = await async_client.get("/api/radar/frames?from=1000&to=3000")
    assert resp.status_code == 200
    body = resp.json()
    assert [f["timestamp"] for f in body["frames"]] == [1000, 2000, 3000]
    assert body["gaps"] == [{"start": 1500, "end": 1800}]


async def test_frames_historical_not_micro_cached(async_client):
    """Only the live window is micro-cached; archive queries always hit the DB."""
    await RadarFrame.objects.acreate(
        timestamp=1000,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    resp = await async_client.get("/api/radar/frames?from=500&to=1500")
    assert [f["timestamp"] for f in resp.json()["frames"]] == [1000]
    await RadarFrame.objects.acreate(
        timestamp=1200,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    resp2 = await async_client.get("/api/radar/frames?from=500&to=1500")
    assert [f["timestamp"] for f in resp2.json()["frames"]] == [1000, 1200]


async def test_frames_historical_oversized_span_400(async_client):
    resp = await async_client.get("/api/radar/frames?from=0&to=999999999")
    assert resp.status_code == 400
    assert resp.json() == {"error": "range_too_large"}


async def test_frames_historical_inverted_span_400(async_client):
    resp = await async_client.get("/api/radar/frames?from=2000&to=1000")
    assert resp.status_code == 400


async def test_frames_historical_ongoing_gap_serializes_null_end(async_client):
    await RadarFrame.objects.acreate(
        timestamp=2000,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    await ArchiveGap.objects.acreate(
        service="radar",
        gap_start=datetime.fromtimestamp(1500, tz=UTC),
        gap_end=None,
        reason="ongoing",
        detail={},
    )
    resp = await async_client.get("/api/radar/frames?from=1000&to=3000")
    body = resp.json()
    assert body["gaps"] == [{"start": 1500, "end": None}]


# -- latest -------------------------------------------------------------------


@respx.mock
async def test_latest_returns_newest(async_client, sample_weather_maps):
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    resp = await async_client.get("/api/radar/latest")
    assert resp.status_code == 200
    assert resp.json() == {"timestamp": 1683791400}


# -- range --------------------------------------------------------------------


async def test_range_empty_archive(async_client):
    resp = await async_client.get("/api/radar/range")
    assert resp.status_code == 200
    assert resp.json() == {"earliest": None, "latest": None}


async def test_range_bounds(async_client):
    for ts in (1000, 5000, 3000):
        await RadarFrame.objects.acreate(
            timestamp=ts,
            provider="rainviewer",
            tile_count=1,
            status="ok",
            missing=[],
        )
    resp = await async_client.get("/api/radar/range")
    assert resp.status_code == 200
    assert resp.json() == {"earliest": 1000, "latest": 5000}


# -- tile ---------------------------------------------------------------------


async def test_tile_disk_hit(async_client, png_bytes):
    await RadarFrame.objects.acreate(
        timestamp=TS,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    storage.write_tile("rainviewer", TS, 5, 16, 11, png_bytes)
    resp = await async_client.get(TILE_PATH)
    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/png"
    assert resp["Cache-Control"] == "public, max-age=31536000, immutable"
    assert b"".join(resp.streaming_content) == png_bytes


@respx.mock
async def test_tile_miss_fetches_persists_and_returns(async_client, sample_weather_maps, png_bytes):
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    route = respx.get(TILE_URL).mock(return_value=httpx.Response(200, content=png_bytes))
    resp = await async_client.get(TILE_PATH)
    assert resp.status_code == 200
    assert resp.content == png_bytes
    assert resp["Cache-Control"] == "public, max-age=31536000, immutable"
    # Persisted to disk -> a second request is served from disk (no extra fetch).
    assert storage.tile_exists("rainviewer", TS, 5, 16, 11)
    resp2 = await async_client.get(TILE_PATH)
    assert resp2.status_code == 200
    assert route.call_count == 1


@respx.mock
async def test_tile_invalid_zoom_returns_404(async_client):
    resp = await async_client.get(f"/tiles/{DATE}/{TS}/8/16/11.png")
    assert resp.status_code == 404


@respx.mock
async def test_tile_outside_matrix_returns_404(async_client, sample_weather_maps):
    # 5/0/0 is a valid grid tile but lies outside the France coverage matrix —
    # it must 404 before any upstream fetch (no storage/upstream abuse).
    frames_route = respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    resp = await async_client.get(f"/tiles/{DATE}/{TS}/5/0/0.png")
    assert resp.status_code == 404
    assert frames_route.call_count == 0
    assert not storage.tile_exists("rainviewer", TS, 5, 0, 0)


@respx.mock
async def test_tile_bad_date_for_ts_returns_404(async_client, sample_weather_maps):
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    # 2020-01-01 does not match the UTC date of TS -> 404 before any fetch.
    resp = await async_client.get(f"/tiles/2020-01-01/{TS}/5/16/11.png")
    assert resp.status_code == 404


@respx.mock
async def test_tile_unknown_timestamp_returns_404(async_client, sample_weather_maps):
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    resp = await async_client.get("/tiles/2001-09-09/999/5/16/11.png")
    assert resp.status_code == 404


@respx.mock
async def test_tile_upstream_5xx_returns_502(async_client, sample_weather_maps, monkeypatch):
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    respx.get(TILE_URL).mock(return_value=httpx.Response(500))
    resp = await async_client.get(TILE_PATH)
    assert resp.status_code == 502


@respx.mock
async def test_tile_upstream_404_returns_empty_204(async_client, sample_weather_maps):
    # An upstream 404 is a legitimate empty region (like a Météo-France empty tile):
    # the view returns 204, not 404, so the browser renders a blank tile without
    # logging a console error.
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    respx.get(TILE_URL).mock(return_value=httpx.Response(404))
    resp = await async_client.get(TILE_PATH)
    assert resp.status_code == 204
    # "Empty" is as permanent a fact as the bytes would have been — a published
    # frame never changes — so it caches like a real tile instead of being re-asked
    # on every pan, zoom and revisit.
    assert resp["Cache-Control"] == "public, max-age=31536000, immutable"


@respx.mock
async def test_tile_known_empty_answers_from_the_archive_row(async_client, sample_weather_maps):
    """A tile the archiver recorded as empty never reaches upstream.

    This is the fallback's *hot* path for a sparse archive: Météo-France persists only
    non-empty tiles, so on a quiet day every tile in the viewport misses Nginx. The
    frame is still upstream's latest, so without the row check each of those misses
    re-downloaded the product and re-rendered all 62 tiles in the web container —
    and Leaflet's layer `load`, hence the frontend cross-fade, waited on it.
    """
    frames_route = respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    tile_route = respx.get(TILE_URL).mock(return_value=httpx.Response(200, content=b"png"))
    await RadarFrame.objects.acreate(
        timestamp=TS,
        provider="rainviewer",
        tile_count=61,
        status="ok",
        missing=[],
        empty=[{"z": 5, "x": 16, "y": 11}],
    )
    resp = await async_client.get(TILE_PATH)
    assert resp.status_code == 204
    assert resp["Cache-Control"] == "public, max-age=31536000, immutable"
    # Neither the frame index nor the tile itself was fetched.
    assert frames_route.call_count == 0
    assert tile_route.call_count == 0


@respx.mock
async def test_tile_archive_row_only_short_circuits_its_own_empty_tiles(
    async_client,
    sample_weather_maps,
    png_bytes,
):
    """The short-circuit is per tile: a row listing *other* empty tiles must not swallow
    a tile that simply has not been written yet."""
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    route = respx.get(TILE_URL).mock(return_value=httpx.Response(200, content=png_bytes))
    await RadarFrame.objects.acreate(
        timestamp=TS,
        provider="rainviewer",
        tile_count=61,
        status="ok",
        missing=[],
        empty=[{"z": 5, "x": 15, "y": 11}],  # a neighbour, not the requested tile
    )
    resp = await async_client.get(TILE_PATH)
    assert resp.status_code == 200
    assert resp.content == png_bytes
    assert route.call_count == 1


@respx.mock
async def test_tile_aged_out_of_the_live_window_returns_cacheable_204(
    async_client,
    sample_weather_maps,
):
    """An archived frame past the upstream window with no tile on disk: 204, cacheable.

    Nothing will ever fill that hole — the poll only backfills frames still in the
    window — so the answer is permanent and caches like the rest.
    """
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    await RadarFrame.objects.acreate(
        timestamp=999,
        provider="rainviewer",
        tile_count=0,
        status="partial",
        missing=[{"z": 5, "x": 16, "y": 11}],
    )
    resp = await async_client.get("/tiles/1970-01-01/999/5/16/11.png")
    assert resp.status_code == 204
    assert resp["Cache-Control"] == "public, max-age=31536000, immutable"


@respx.mock
async def test_tile_archived_frame_answers_204_without_the_row(async_client, sample_weather_maps):
    """A frame in the archived set settles a miss from Redis alone — no DB, no upstream.

    `status='ok'` means every matrix tile was attempted and none errored, so each one is
    either on disk or on the row's `empty` list; the archived set holds exactly the ok
    frames. "In the set, not on disk" is therefore already "nothing to draw".

    This is the whole of historical navigation. Without it, replaying an archived day
    cost one Postgres connection per missing tile — Django's ASGI handler gives each
    request its own thread and a connection is thread-local — which exhausted
    max_connections and turned every tile into a 500.

    Deliberately seeds *no* RadarFrame row: reaching the DB at all would 404 here, so a
    204 proves the Redis tier short-circuited ahead of it.
    """
    frames_route = respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    tile_route = respx.get(TILE_URL).mock(return_value=httpx.Response(200, content=b"png"))
    await cache.add_archived("rainviewer", TS)

    resp = await async_client.get(TILE_PATH)
    assert resp.status_code == 204
    assert resp["Cache-Control"] == "public, max-age=31536000, immutable"
    assert await RadarFrame.objects.acount() == 0  # the answer never needed a row
    assert frames_route.call_count == 0
    assert tile_route.call_count == 0


@respx.mock
async def test_tile_archived_set_miss_still_consults_the_row(async_client, sample_weather_maps):
    """The Redis tier is a fast path, not a replacement: an unarchived frame uses the row.

    Guards the degradation this design leans on — Redis carries no persistence, so after
    a flush every tile must still be answered correctly from Postgres (bounded), just
    more expensively, until the archiver's cold-start rebuild refills the set.
    """
    frames_route = respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    await RadarFrame.objects.acreate(
        timestamp=TS,
        provider="rainviewer",
        tile_count=61,
        status="ok",
        missing=[],
        empty=[{"z": 5, "x": 16, "y": 11}],
    )
    assert not await cache.is_archived("rainviewer", TS)  # flushed by the conftest

    resp = await async_client.get(TILE_PATH)
    assert resp.status_code == 204
    assert resp["Cache-Control"] == "public, max-age=31536000, immutable"
    assert frames_route.call_count == 0


async def test_tile_archive_lookup_failure_sheds_503_uncached(async_client, monkeypatch):
    """A DB error in the row lookup is a 503, never a 500 — and never a cached 204.

    Direct regression test for the incident: `_archived_empty` was the only unguarded DB
    call in any view, so connection-pool pressure surfaced as a wall of 500s instead of
    graceful shedding.

    The 204 half matters as much as the 500 half. We do not know whether the tile is
    empty, and `_tile_no_content` is `immutable` — answering 204 here would pin a blank
    tile in every visitor's browser cache for a year.
    """

    async def _boom(*_args, **_kwargs):
        msg = "FATAL: sorry, too many clients already"
        raise OperationalError(msg)

    monkeypatch.setattr(views, "_archived_empty", _boom)
    resp = await async_client.get(TILE_PATH)
    assert resp.status_code == 503
    assert resp["Cache-Control"] == "no-store"


@override_settings(TILE_ARCHIVE_LOOKUP_CONCURRENCY=3)
async def test_tile_archive_lookup_is_concurrency_bounded(async_client, monkeypatch):
    """N concurrent tile misses must never mean N concurrent archive lookups.

    Load-bearing. Django's ASGI handler runs each request inside its own
    `ThreadSensitiveContext`, and a Django DB connection is thread-local, so an
    unbounded fan-out of tile misses *is* an unbounded fan-out of Postgres connections —
    and `/tiles/…` is deliberately unthrottled in Nginx. This semaphore is the only
    thing standing between a client replaying an archived day and `max_connections`.
    """
    live = 0
    peak = 0

    async def _slow(*_args, **_kwargs):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.02)
            # Answer "this tile is empty" so the request ends at 204 and never walks
            # on to the upstream steps — the bound is what is under test, not the fetch.
            return {(5, 16, 11)}
        finally:
            live -= 1

    monkeypatch.setattr(views, "_archived_empty", _slow)
    # Rebuild the loop-bound semaphore so it picks up the overridden setting.
    views._archive_sem = None

    async def _one(x):
        return await async_client.get(f"/tiles/{DATE}/{TS}/5/{x}/11.png")

    # A full frame's worth of misses, as one page-load fan-out would produce.
    await asyncio.gather(*(_one(16) for _ in range(24)))
    assert peak <= 3, f"archive lookups ran {peak}-wide against a cap of 3"


# -- provider selection + advert ----------------------------------------------


@override_settings(METEOFRANCE_ENABLED=False)
async def test_frames_advert_single_provider_when_meteofrance_off(async_client):
    now = int(datetime.now(tz=UTC).timestamp())
    await RadarFrame.objects.acreate(
        timestamp=now - 600,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    body = (await async_client.get("/api/radar/frames")).json()
    advert = body["providers"]
    assert [p["name"] for p in advert] == ["rainviewer"]
    assert advert[0]["label"] == "RainViewer"
    assert advert[0]["frame_interval"] == settings.FRAME_INTERVAL
    assert "rainviewer.com" in advert[0]["attribution"]


@override_settings(METEOFRANCE_ENABLED=True)
async def test_frames_advert_lists_both_when_enabled(async_client):
    now = int(datetime.now(tz=UTC).timestamp())
    await RadarFrame.objects.acreate(
        timestamp=now - 600,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    advert = (await async_client.get("/api/radar/frames")).json()["providers"]
    assert [p["name"] for p in advert] == ["rainviewer", "meteofrance"]
    mf = next(p for p in advert if p["name"] == "meteofrance")
    # Proper noun, not localized; the beta suffix rides along in the same advert.
    assert mf["label"] == "Météo-France (beta)"
    assert mf["frame_interval"] == settings.METEOFRANCE_FRAME_INTERVAL
    assert "Licence Ouverte" in mf["attribution"]


@override_settings(METEOFRANCE_ENABLED=True)
async def test_frames_provider_param_selects_meteofrance(async_client):
    now = int(datetime.now(tz=UTC).timestamp())
    # Same timestamp under two providers — provider filtering must isolate them.
    await RadarFrame.objects.acreate(
        timestamp=now - 300,
        provider="meteofrance",
        tile_count=5,
        status="ok",
        missing=[],
    )
    await RadarFrame.objects.acreate(
        timestamp=now - 300,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    body = (await async_client.get("/api/radar/frames?provider=meteofrance")).json()
    assert body["provider"] == "meteofrance"
    assert "Météo-France" in body["attribution"]
    assert [f["timestamp"] for f in body["frames"]] == [now - 300]


async def test_frames_unknown_provider_400(async_client):
    resp = await async_client.get("/api/radar/frames?provider=bogus")
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown_provider"}


@override_settings(METEOFRANCE_ENABLED=False)
async def test_frames_meteofrance_disabled_is_400(async_client):
    resp = await async_client.get("/api/radar/frames?provider=meteofrance")
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown_provider"}


@override_settings(METEOFRANCE_ENABLED=True)
async def test_range_is_per_provider(async_client):
    for ts in (1000, 5000):
        await RadarFrame.objects.acreate(
            timestamp=ts,
            provider="rainviewer",
            tile_count=1,
            status="ok",
            missing=[],
        )
    for ts in (2000, 3000):
        await RadarFrame.objects.acreate(
            timestamp=ts,
            provider="meteofrance",
            tile_count=1,
            status="ok",
            missing=[],
        )
    rv = await async_client.get("/api/radar/range?provider=rainviewer")
    assert rv.json() == {"earliest": 1000, "latest": 5000}
    mf = await async_client.get("/api/radar/range?provider=meteofrance")
    assert mf.json() == {"earliest": 2000, "latest": 3000}


# -- provider-scoped tile routes ----------------------------------------------

RV_TILE_PATH = f"/tiles/rainviewer/{DATE}/{TS}/5/16/11.png"
MF_TILE_PATH = f"/tiles/meteofrance/{DATE}/{TS}/5/16/11.png"


async def test_provider_scoped_tile_disk_hit(async_client, png_bytes):
    await RadarFrame.objects.acreate(
        timestamp=TS,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    storage.write_tile("rainviewer", TS, 5, 16, 11, png_bytes)
    resp = await async_client.get(RV_TILE_PATH)
    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == png_bytes


@override_settings(METEOFRANCE_ENABLED=True)
async def test_meteofrance_tile_disk_hit(async_client, png_bytes):
    await RadarFrame.objects.acreate(
        timestamp=TS,
        provider="meteofrance",
        tile_count=5,
        status="ok",
        missing=[],
    )
    storage.write_tile("meteofrance", TS, 5, 16, 11, png_bytes)
    resp = await async_client.get(MF_TILE_PATH)
    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == png_bytes


@override_settings(METEOFRANCE_ENABLED=False)
async def test_meteofrance_tile_404_when_disabled(async_client, png_bytes):
    # A disabled provider's route 404s before touching disk or upstream.
    storage.write_tile("meteofrance", TS, 5, 16, 11, png_bytes)
    resp = await async_client.get(MF_TILE_PATH)
    assert resp.status_code == 404


async def test_rainviewer_legacy_path_dual_read(async_client, png_bytes):
    # A tile still at the legacy path is served via the rainviewer dual-read.
    await RadarFrame.objects.acreate(
        timestamp=TS,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    legacy = storage.tile_root() / DATE / str(TS) / "5" / "16" / "11.png"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(png_bytes)
    resp = await async_client.get(RV_TILE_PATH)
    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == png_bytes


async def test_legacy_tile_url_aliases_rainviewer(async_client, png_bytes):
    # The legacy /tiles/{date}/… URL serves the rainviewer tile from the new layout.
    await RadarFrame.objects.acreate(
        timestamp=TS,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    storage.write_tile("rainviewer", TS, 5, 16, 11, png_bytes)
    resp = await async_client.get(TILE_PATH)  # legacy /tiles/{date}/…
    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == png_bytes


# -- health -------------------------------------------------------------------


async def test_healthz_ok(async_client):
    resp = await async_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_ok(async_client):
    resp = await async_client.get("/readyz")
    assert resp.status_code == 200


async def test_readyz_503_when_redis_down(async_client, monkeypatch):
    async def _down() -> bool:
        return False

    monkeypatch.setattr(cache, "ping", _down)
    resp = await async_client.get("/readyz")
    assert resp.status_code == 503
