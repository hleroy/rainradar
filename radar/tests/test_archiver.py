"""Archiver logic — backfill, empties, partials, gaps.

Upstream HTTP mocked with respx; tile root pointed at ``tmp_path``; no network.
"""

from __future__ import annotations

import re
from datetime import UTC
from datetime import datetime

import httpx
import pytest
import respx
from django.conf import settings
from django.test import override_settings

from radar import archiver
from radar import cache
from radar.models import ArchiveGap
from radar.models import RadarFrame
from radar.providers import get_active_provider
from radar.providers import get_provider
from radar.providers import meteofrance
from radar.providers import rainviewer
from radar.providers.rainviewer import RainViewerProvider

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

# Any RainViewer tile URL -> matched by this regex catch-all.
TILE_RE = re.compile(r"https://tilecache\.rainviewer\.com/.+\.png$")
MATRIX_SIZE = 62

# Météo-France endpoints for the isolation test.
MF_TOKEN_URL = "https://portail-api.meteofrance.fr/token"
MF_API_BASE = "https://public-api.meteofrance.fr/public/DPRadar/v1"
MF_DESC_URL = f"{MF_API_BASE}/mosaiques/METROPOLE/observations/LAME_D_EAU"


async def _noop(*_args, **_kwargs) -> None:
    pass


@pytest.fixture
def tile_root(tmp_path):
    with override_settings(TILE_ROOT=str(tmp_path)):
        yield tmp_path


def _mock_frames(sample):
    return respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample),
    )


@respx.mock
async def test_backfill_archives_all_and_is_idempotent(tile_root, sample_weather_maps, png_bytes):
    _mock_frames(sample_weather_maps)
    tile_route = respx.get(url__regex=TILE_RE).mock(
        return_value=httpx.Response(200, content=png_bytes),
    )
    provider = get_active_provider()

    stats = await archiver.poll_radar(provider)

    assert stats["ok"] is True
    assert stats["archived"] == 3
    assert await RadarFrame.objects.acount() == 3
    async for frame in RadarFrame.objects.all():
        assert frame.status == "ok"
        assert frame.tile_count == MATRIX_SIZE
    # Files actually landed on disk.
    assert any(tile_root.rglob("*.png"))
    # Archived set populated.
    assert await cache.archived_count("rainviewer") == 3

    fetches_after_first = tile_route.call_count
    assert fetches_after_first == MATRIX_SIZE * 3  # every tile of every frame

    # Idempotent: a second poll fetches zero further tiles.
    stats2 = await archiver.poll_radar(provider)
    assert stats2["archived"] == 0
    assert tile_route.call_count == fetches_after_first
    assert await RadarFrame.objects.acount() == 3


@respx.mock
async def test_empty_tiles_not_stored(tile_root):
    respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(404))
    provider = RainViewerProvider()

    frame = await archiver.archive_frame(provider, 1683790200)

    assert frame.status == "ok"  # empties are a normal result
    assert frame.tile_count == 0
    assert frame.missing == []
    assert len(frame.empty) == MATRIX_SIZE  # ...but remembered, so retries skip them
    assert not any(tile_root.rglob("*.png"))  # nothing written


@respx.mock
async def test_known_empty_tiles_are_not_refetched(tile_root, png_bytes, monkeypatch):
    """A retried partial frame must not re-download the tiles already known empty.

    A published frame is immutable upstream, and "no file on disk" cannot tell
    "empty" from "never fetched" — so without the recorded empties, every retry
    re-fetched the whole empty part of the matrix for the life of the frame.
    """
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    provider = RainViewerProvider()
    ts = 1683790200
    matrix = archiver._matrix()
    bad, good = matrix[0], matrix[1]
    # One tile 5xx (-> partial, so the frame is retried), one with real rain, and the
    # whole rest of the matrix legitimately empty.
    respx.get(provider.tile_url(ts, *bad)).mock(return_value=httpx.Response(500))
    respx.get(provider.tile_url(ts, *good)).mock(
        return_value=httpx.Response(200, content=png_bytes),
    )
    empties = respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(404))

    first = await archiver.archive_frame(provider, ts)
    assert first.status == "partial"
    assert len(first.empty) == MATRIX_SIZE - 2
    assert empties.call_count == MATRIX_SIZE - 2

    # The retry re-attempts only the tile that actually failed: the written one is on
    # disk, and the empties are now known.
    second = await archiver.archive_frame(provider, ts)

    assert second.status == "partial"
    assert empties.call_count == MATRIX_SIZE - 2  # zero further requests for the empties
    assert len(second.empty) == MATRIX_SIZE - 2  # and they survive the round-trip


@respx.mock
async def test_rate_limit_aborts_the_rest_of_the_frame(tile_root, monkeypatch):
    """A 429 must abandon the batch, not grind all 62 tiles into the limit."""
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    route = respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(429))
    provider = RainViewerProvider()

    frame = await archiver.archive_frame(provider, 1683790200)

    assert frame.status == "failed"
    assert frame.rate_limited is True
    assert len(frame.missing) == MATRIX_SIZE  # every tile still queued for retry
    # Only the tiles already admitted through the semaphore can have gone out; the
    # rest short-circuit. Previously this was 62 requests (3 attempts each).
    assert route.call_count <= settings.TILE_FETCH_CONCURRENCY
    # Not marked done -> the next poll retries it once the cooldown lapses.
    assert await cache.is_archived("rainviewer", 1683790200) is False


@respx.mock
async def test_poll_stops_backfilling_when_rate_limited(
    tile_root,
    sample_weather_maps,
    monkeypatch,
):
    """One throttled frame ends the poll — a cold start must not walk the window."""
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    _mock_frames(sample_weather_maps)
    route = respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(429))

    result = await archiver.poll_radar(RainViewerProvider())

    assert result["frames_seen"] == 3
    assert result["archived"] == 1  # gave up after the first throttled frame
    assert route.call_count <= settings.TILE_FETCH_CONCURRENCY
    # The deferred frames are untouched, so the next poll picks them up.
    assert await RadarFrame.objects.filter(provider="rainviewer").acount() == 1


@respx.mock
async def test_partial_when_some_tiles_5xx(tile_root, png_bytes, monkeypatch):
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    provider = RainViewerProvider()
    ts = 1683790200
    matrix = archiver._matrix()
    bad = matrix[0]
    bad_url = provider.tile_url(ts, *bad)
    # Specific bad tile first (5xx), everything else 200.
    respx.get(bad_url).mock(return_value=httpx.Response(500))
    respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(200, content=png_bytes))

    frame = await archiver.archive_frame(provider, ts)

    assert frame.status == "partial"
    assert frame.tile_count == MATRIX_SIZE - 1
    assert frame.missing == [{"z": bad[0], "x": bad[1], "y": bad[2]}]
    # A partial frame is NOT marked done -> eligible for retry next poll.
    assert await cache.is_archived("rainviewer", ts) is False


@respx.mock
async def test_failed_when_all_tiles_5xx(tile_root, monkeypatch):
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(500))
    provider = RainViewerProvider()

    frame = await archiver.archive_frame(provider, 1683790200)

    assert frame.status == "failed"
    assert frame.tile_count == 0
    assert len(frame.missing) == MATRIX_SIZE


@respx.mock
async def test_gap_detected_when_frames_aged_out(tile_root, sample_weather_maps, png_bytes):
    _mock_frames(sample_weather_maps)
    respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(200, content=png_bytes))
    provider = get_active_provider()

    # Prior archived frame far behind the oldest upstream frame (1683790200).
    prev_max = 1683790200 - 10000
    await RadarFrame.objects.acreate(
        timestamp=prev_max,
        provider="rainviewer",
        tile_count=MATRIX_SIZE,
        status="ok",
        missing=[],
    )
    await cache.add_archived("rainviewer", prev_max)

    await archiver.poll_radar(provider)

    gaps = [g async for g in ArchiveGap.objects.all()]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.gap_start == datetime.fromtimestamp(prev_max + settings.FRAME_INTERVAL, tz=UTC)
    assert gap.gap_end == datetime.fromtimestamp(1683790200, tz=UTC)
    assert gap.detail["prev_max"] == prev_max

    # Re-poll must not duplicate the gap.
    await archiver.poll_radar(provider)
    assert await ArchiveGap.objects.acount() == 1


@respx.mock
async def test_poll_increments_storage_gauge(tile_root, sample_weather_maps, png_bytes):
    _mock_frames(sample_weather_maps)
    respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(200, content=png_bytes))
    provider = get_active_provider()

    assert await cache.get_tile_dir_bytes() == 0
    await archiver.poll_radar(provider)

    # 3 frames * 62 tiles * the PNG size, accumulated via INCRBY (no rescan).
    assert await cache.get_tile_dir_bytes() == 3 * MATRIX_SIZE * len(png_bytes)


@respx.mock
async def test_upstream_failure_opens_then_closes_ongoing_gap(
    tile_root,
    sample_weather_maps,
    png_bytes,
    monkeypatch,
):
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    provider = get_active_provider()

    frames_route = respx.get(settings.RAINVIEWER_API_URL).mock(return_value=httpx.Response(500))
    respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(200, content=png_bytes))

    # First failure is below GAP_OPEN_AFTER_FAILURES (default 2): no gap yet.
    stats = await archiver.poll_radar(provider)
    assert stats["ok"] is False
    assert await ArchiveGap.objects.filter(gap_end__isnull=True).acount() == 0

    # Second consecutive failure crosses the threshold: the ongoing gap opens.
    stats = await archiver.poll_radar(provider)
    assert stats["ok"] is False
    open_gaps = [g async for g in ArchiveGap.objects.filter(gap_end__isnull=True)]
    assert len(open_gaps) == 1
    assert open_gaps[0].reason == archiver.GAP_REASON_UNREACHABLE

    # Recover: next poll succeeds and closes the ongoing gap.
    frames_route.mock(return_value=httpx.Response(200, json=sample_weather_maps))
    stats2 = await archiver.poll_radar(provider)
    assert stats2["ok"] is True
    assert await ArchiveGap.objects.filter(gap_end__isnull=True).acount() == 0
    assert await ArchiveGap.objects.acount() == 1  # the same row, now closed


@respx.mock
async def test_meteofrance_failure_isolated_from_rainviewer(
    tile_root,
    sample_weather_maps,
    png_bytes,
    monkeypatch,
):
    """A Météo-France poll failure never touches the RainViewer poll."""
    monkeypatch.setattr(meteofrance.asyncio, "sleep", _noop)
    # RainViewer upstream healthy.
    _mock_frames(sample_weather_maps)
    respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(200, content=png_bytes))
    # Météo-France: token OK, but the catalog is down (500) — get_frames ⇒ unavailable.
    respx.post(MF_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx.get(MF_DESC_URL).mock(return_value=httpx.Response(500))

    with override_settings(
        METEOFRANCE_ENABLED=True,
        METEOFRANCE_APPLICATION_ID="dGVzdA==",
        METEOFRANCE_TOKEN_URL=MF_TOKEN_URL,
        METEOFRANCE_API_BASE_URL=MF_API_BASE,
        METEOFRANCE_ZONE="METROPOLE",
        METEOFRANCE_OBSERVATION="LAME_D_EAU",
    ):
        rv_stats = await archiver.poll_radar(get_provider("rainviewer"))
        mf_stats = await archiver.poll_radar(get_provider("meteofrance"))

    # RainViewer archived its full window; Météo-France degraded gracefully (no raise).
    assert rv_stats["ok"] is True
    assert rv_stats["archived"] == 3
    assert mf_stats["ok"] is False
    assert await RadarFrame.objects.filter(provider="rainviewer").acount() == 3
    assert await RadarFrame.objects.filter(provider="meteofrance").acount() == 0


@respx.mock
async def test_meteofrance_failing_frame_downloads_once(tile_root, monkeypatch):
    """A failing Météo-France frame must cost one download budget, not one per wave.

    archive_frame admits the 62 tiles through a TILE_FETCH_CONCURRENCY-wide
    semaphore, so they reach the provider's single-flight memo in waves. While a
    failed task was evicted immediately, each wave found the memo empty and started a
    fresh download with its own retry budget — 27 product downloads for one frame,
    and as many times the wall-clock, pushing the frame past its own 5-minute cadence.
    """
    monkeypatch.setattr(meteofrance.asyncio, "sleep", _noop)
    respx.post(MF_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    validity = "2026-07-19T11:35:00Z"
    ts = int(datetime(2026, 7, 19, 11, 35, tzinfo=UTC).timestamp())
    respx.get(MF_DESC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "links": [
                    {
                        "href": "/mosaiques/METROPOLE/observations/LAME_D_EAU/produit?maille=500",
                        "validity_time": validity,
                    },
                ],
            },
        ),
    )
    product = respx.get(f"{MF_DESC_URL}/produit?maille=500").mock(
        return_value=httpx.Response(500),
    )

    with override_settings(
        METEOFRANCE_ENABLED=True,
        METEOFRANCE_APPLICATION_ID="dGVzdA==",
        METEOFRANCE_TOKEN_URL=MF_TOKEN_URL,
        METEOFRANCE_API_BASE_URL=MF_API_BASE,
        METEOFRANCE_ZONE="METROPOLE",
        METEOFRANCE_OBSERVATION="LAME_D_EAU",
        METEOFRANCE_MAILLE=500,
    ):
        provider = get_provider("meteofrance")
        await provider.get_frames()
        frame = await archiver.archive_frame(provider, ts)

    assert frame.status == "failed"
    assert len(frame.missing) == MATRIX_SIZE  # every tile still queued for retry
    # One retry budget (_PRODUCT_BACKOFFS + 1), shared by all 62 tiles. Was 24.
    # One retry budget (_PRODUCT_BACKOFFS + 1), shared by all 62 tiles. Measured at
    # 27 downloads for this same frame before the fix.
    assert product.call_count == len(meteofrance._PRODUCT_BACKOFFS) + 1


@respx.mock
async def test_aged_out_gap_not_duplicated_over_existing(tile_root, sample_weather_maps, png_bytes):
    _mock_frames(sample_weather_maps)
    respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(200, content=png_bytes))
    provider = get_active_provider()

    prev_max = 1683790200 - 10000
    await RadarFrame.objects.acreate(
        timestamp=prev_max,
        provider="rainviewer",
        tile_count=MATRIX_SIZE,
        status="ok",
        missing=[],
    )
    await cache.add_archived("rainviewer", prev_max)
    # An ongoing gap that was opened during the outage and closed on recovery,
    # already covering the same aged-out window.
    await ArchiveGap.objects.acreate(
        service="radar",
        gap_start=datetime.fromtimestamp(prev_max + settings.FRAME_INTERVAL, tz=UTC),
        gap_end=datetime.fromtimestamp(1683790200, tz=UTC),
        reason=archiver.GAP_REASON_UNREACHABLE,
        detail={},
    )

    await archiver.poll_radar(provider)

    # detect_aged_out_gap must not stack a second overlapping row.
    assert await ArchiveGap.objects.acount() == 1
