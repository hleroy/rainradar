"""Lightning endpoints: SSE stream, history, frames advert.

The SSE generator is driven directly (subscribe is real Redis pub/sub; the
dockerized suite has Redis). History/frames go through the async test client.
The ``lightning_strike`` table is managed=False (not auto-truncated), so the
history tests clear it first for deterministic counts.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC
from datetime import datetime

import pytest
from django.conf import settings
from django.test import override_settings

from radar import cache
from radar import views
from radar.models import LightningStrike

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


def _at(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


async def _seed(epoch, lat=45.0, lon=3.0, intensity=1):
    await LightningStrike.objects.acreate(
        struck_at=_at(epoch),
        lat=lat,
        lon=lon,
        intensity=intensity,
    )


def _json(lat, lon, t, intensity=1) -> bytes:
    return json.dumps({"lat": lat, "lon": lon, "time": t, "intensity": intensity}).encode()


# -- SSE stream ---------------------------------------------------------------


async def test_sse_replays_recent_buffer_oldest_first():
    now = time.time()
    await cache.push_recent(_json(45.0, 3.0, now - 2))
    await cache.push_recent(_json(46.0, 4.0, now - 1, intensity=2))

    gen = views._lightning_event_stream()
    try:
        first = await asyncio.wait_for(anext(gen), timeout=2)
        second = await asyncio.wait_for(anext(gen), timeout=2)
    finally:
        await gen.aclose()

    assert first.startswith("event: strike\ndata: ")
    assert '"lat": 45.0' in first  # oldest first
    assert '"lat": 46.0' in second


async def test_sse_drops_recent_older_than_horizon():
    now = time.time()
    with override_settings(LIGHTNING_RECENT_SECONDS=60, LIGHTNING_SSE_HEARTBEAT_SECONDS=0.05):
        await cache.push_recent(_json(1.0, 1.0, now - 999))
        gen = views._lightning_event_stream()
        try:
            # the stale recent entry is skipped, so the first yield is the heartbeat
            frame = await asyncio.wait_for(anext(gen), timeout=2)
        finally:
            await gen.aclose()
    assert frame == ": keepalive\n\n"


async def test_sse_forwards_live_published_strike():
    with override_settings(LIGHTNING_SSE_HEARTBEAT_SECONDS=5):
        gen = views._lightning_event_stream()
        # First pull subscribes then blocks on get_message; run it as a task and
        # publish once the subscription is live.
        task = asyncio.create_task(anext(gen))
        await asyncio.sleep(0.2)
        await cache.publish_strike(_json(47.5, 5.0, time.time(), intensity=9))
        try:
            frame = await asyncio.wait_for(task, timeout=2)
        finally:
            await gen.aclose()
    assert frame.startswith("event: strike\ndata: ")
    assert '"lat": 47.5' in frame


async def test_sse_returns_503_when_redis_down(async_client, monkeypatch):
    from unittest.mock import AsyncMock  # noqa: PLC0415

    monkeypatch.setattr(cache, "ping", AsyncMock(return_value=False))
    resp = await async_client.get("/api/lightning/stream")
    assert resp.status_code == 503
    assert resp.json() == {"error": "lightning_unavailable"}


# -- history ------------------------------------------------------------------


async def test_history_returns_range_oldest_to_newest(async_client):
    await LightningStrike.objects.all().adelete()
    await _seed(1700000000)
    await _seed(1700000100)
    await _seed(1700000200)

    resp = await async_client.get("/api/lightning/history?from=1700000050&to=1700000300")
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is False
    assert "Blitzortung.org" in body["attribution"]
    times = [s["time"] for s in body["strikes"]]
    assert times == [1700000100.0, 1700000200.0]  # in-range, oldest -> newest


async def test_history_bbox_filters(async_client):
    await LightningStrike.objects.all().adelete()
    await _seed(1700000000, lat=45.0, lon=3.0)  # inside
    await _seed(1700000010, lat=50.0, lon=8.0)  # outside the narrow bbox

    resp = await async_client.get(
        "/api/lightning/history?from=1699999900&to=1700000100&bbox=44,46,2,4",
    )
    body = resp.json()
    assert len(body["strikes"]) == 1
    assert body["strikes"][0]["lat"] == pytest.approx(45.0, abs=1e-3)


async def test_history_truncates_to_most_recent(async_client):
    await LightningStrike.objects.all().adelete()
    for i in range(3):
        await _seed(1700000000 + i * 10, intensity=i)

    with override_settings(LIGHTNING_HISTORY_MAX_STRIKES=2):
        resp = await async_client.get("/api/lightning/history?from=1699999900&to=1700000100")
    body = resp.json()
    assert body["truncated"] is True
    times = [s["time"] for s in body["strikes"]]
    assert times == [1700000010.0, 1700000020.0]  # most recent 2, oldest -> newest


async def test_history_missing_params_400(async_client):
    resp = await async_client.get("/api/lightning/history")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_range"


async def test_history_span_too_large_400(async_client):
    resp = await async_client.get("/api/lightning/history?from=0&to=200000")  # > 86400 default
    assert resp.status_code == 400
    assert resp.json()["error"] == "range_too_large"


async def test_history_from_after_to_400(async_client):
    resp = await async_client.get("/api/lightning/history?from=100&to=50")
    assert resp.status_code == 400
    assert resp.json()["error"] == "range_too_large"


async def test_history_bad_bbox_400(async_client):
    resp = await async_client.get("/api/lightning/history?from=1&to=2&bbox=1,2,3")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_bbox"


async def test_history_db_error_503(async_client, monkeypatch):
    from unittest.mock import AsyncMock  # noqa: PLC0415

    monkeypatch.setattr(views, "_query_history", AsyncMock(side_effect=RuntimeError("db down")))
    resp = await async_client.get("/api/lightning/history?from=1&to=2")
    assert resp.status_code == 503
    assert resp.json()["error"] == "lightning_unavailable"


# -- frames advert ------------------------------------------------------------


async def test_frames_includes_lightning_block(async_client):
    # Seed an archived frame so the live path doesn't need the upstream provider.
    from radar.models import RadarFrame  # noqa: PLC0415

    now = int(datetime.now(tz=UTC).timestamp())
    await RadarFrame.objects.acreate(
        timestamp=now - 300,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )

    resp = await async_client.get("/api/radar/frames")
    block = resp.json()["lightning"]
    assert block["enabled"] is settings.LIGHTNING_ENABLED  # False by default
    assert "Blitzortung.org" in block["attribution"]
    assert block["bbox"] == list(settings.LIGHTNING_BBOX)
    assert block["display_hours"] == settings.LIGHTNING_DISPLAY_HOURS


async def test_frames_lightning_enabled_reflects_setting(async_client):
    from radar.models import RadarFrame  # noqa: PLC0415

    now = int(datetime.now(tz=UTC).timestamp())
    await RadarFrame.objects.acreate(
        timestamp=now - 300,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    with override_settings(LIGHTNING_ENABLED=True):
        resp = await async_client.get("/api/radar/frames")
    assert resp.json()["lightning"]["enabled"] is True
