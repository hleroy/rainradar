"""``GET /api/stats`` — About-dialog statistics."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from django.conf import settings
from django.test import override_settings

from radar import cache
from radar import stats as stats_mod
from radar.models import LightningStrike
from radar.models import RadarFrame

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def _seed_frames() -> None:
    await RadarFrame.objects.acreate(
        timestamp=1000,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    await RadarFrame.objects.acreate(
        timestamp=2000,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )


def _is_int_or_none(v) -> bool:
    return v is None or isinstance(v, int)


async def test_stats_shape(async_client):
    resp = await async_client.get("/api/stats")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/json"
    body = resp.json()
    assert set(body) == {"radar", "lightning", "storage", "live", "generated_at"}
    assert _is_int_or_none(body["radar"]["earliest"])
    assert _is_int_or_none(body["radar"]["latest"])
    assert _is_int_or_none(body["lightning"]["last_strike"])
    assert _is_int_or_none(body["storage"]["bytes"])
    assert _is_int_or_none(body["live"]["last_frame"])
    assert isinstance(body["generated_at"], int)


async def test_stats_radar_fields_match_db(async_client):
    await _seed_frames()
    body = (await async_client.get("/api/stats")).json()
    assert body["radar"]["frames_total"] == 2
    assert body["radar"]["earliest"] == 1000
    assert body["radar"]["latest"] == 2000
    assert body["radar"]["retention_days"] == settings.RETENTION_DAYS
    assert body["live"]["last_frame"] == 2000


@override_settings(METEOFRANCE_ENABLED=False)
async def test_stats_providers_single_entry_by_default(async_client):
    await _seed_frames()
    provs = (await async_client.get("/api/stats")).json()["radar"]["providers"]
    assert [p["name"] for p in provs] == ["rainviewer"]
    assert provs[0]["frames"] == 2
    assert provs[0]["oldest"] == 1000
    assert provs[0]["newest"] == 2000


@override_settings(METEOFRANCE_ENABLED=True)
async def test_stats_lists_per_provider_breakdown(async_client):
    await _seed_frames()  # 2 rainviewer rows @1000,2000
    await RadarFrame.objects.acreate(
        timestamp=3000,
        provider="meteofrance",
        tile_count=5,
        status="ok",
        missing=[],
    )
    body = (await async_client.get("/api/stats")).json()
    provs = {p["name"]: p for p in body["radar"]["providers"]}
    assert set(provs) == {"rainviewer", "meteofrance"}
    assert provs["rainviewer"]["frames"] == 2
    assert provs["meteofrance"]["frames"] == 1
    assert provs["meteofrance"]["oldest"] == 3000
    assert provs["meteofrance"]["newest"] == 3000


async def test_stats_lightning_counts_and_24h_window(async_client):
    # Partitioned rows aren't rolled back under transaction=True (cf. test_metrics).
    await LightningStrike.objects.all().adelete()
    now = datetime.now(tz=UTC)
    await LightningStrike.objects.acreate(struck_at=now, lat=45.0, lon=3.0, intensity=4)
    await LightningStrike.objects.acreate(
        struck_at=now - timedelta(hours=25),
        lat=46.0,
        lon=4.0,
        intensity=2,
    )
    body = (await async_client.get("/api/stats")).json()
    assert body["lightning"]["archived_total"] == 2
    assert body["lightning"]["strikes_24h"] == 1  # the 25h-old strike is excluded
    assert body["lightning"]["enabled"] == settings.LIGHTNING_ENABLED


async def test_stats_storage_bytes_mirrors_gauge(async_client):
    await cache.set_tile_dir_bytes(13314398720)
    body = (await async_client.get("/api/stats")).json()
    assert body["storage"]["bytes"] == 13314398720


async def test_stats_storage_bytes_null_when_unset(async_client):
    # _clean_state flushes Redis, so the gauge key is absent here.
    body = (await async_client.get("/api/stats")).json()
    assert body["storage"]["bytes"] is None


async def test_stats_served_from_cache_within_ttl(async_client):
    await _seed_frames()
    first = (await async_client.get("/api/stats")).json()
    assert first["radar"]["frames_total"] == 2

    # Mutate the DB; a cache hit must return the original (stale) bytes verbatim.
    await RadarFrame.objects.acreate(
        timestamp=3000,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    second = (await async_client.get("/api/stats")).json()
    assert second == first
    assert second["radar"]["frames_total"] == 2  # not 3 — DB wasn't re-queried


async def test_stats_degrades_to_null_on_read_error(async_client, monkeypatch):
    await _seed_frames()

    async def _boom():
        msg = "redis down"
        raise RuntimeError(msg)

    monkeypatch.setattr(stats_mod.cache, "get_ws_connected", _boom)
    body = (await async_client.get("/api/stats")).json()
    assert body["lightning"]["ws_connected"] is None  # the failing read -> null
    assert body["radar"]["frames_total"] == 2  # radar fields still correct
    # generated_at still present -> the view returned 200, not a 5xx.
    assert isinstance(body["generated_at"], int)


async def test_stats_rejects_non_get(async_client):
    resp = await async_client.post("/api/stats")
    assert resp.status_code == 405
