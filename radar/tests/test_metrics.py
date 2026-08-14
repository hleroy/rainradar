"""/metrics exposition."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest
from django.test import override_settings

from radar import cache
from radar.models import ArchiveGap
from radar.models import LightningStrike
from radar.models import RadarFrame

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

EXPECTED_SERIES = {
    "radar_frames_total",
    "radar_frames_24h",
    "radar_frames_partial_total",
    "radar_frames_failed_total",
    "radar_archive_gaps_total",
    "radar_archive_gap_open",
    "radar_tile_dir_bytes",
    "radar_storage_used_ratio",
    "radar_last_poll_timestamp",
    "radar_archive_earliest_timestamp",
    "radar_archive_latest_timestamp",
}

EXPECTED_LIGHTNING_SERIES = {
    "lightning_strikes_total",
    "lightning_strikes_24h",
    "lightning_archived_total",
    "lightning_ws_connected",
    "lightning_ws_uptime_seconds",
    "lightning_ws_reconnects_total",
    "lightning_queue_dropped_total",
    "lightning_last_strike_timestamp",
    "lightning_partitions_total",
}


def _parse(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, value = line.split(" ", 1)
        values[name] = value
    return values


async def test_metrics_content_type_and_series(async_client):
    resp = await async_client.get("/metrics")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/plain; version=0.0.4"
    values = _parse(resp.content.decode())
    assert set(values) >= EXPECTED_SERIES
    assert set(values) >= EXPECTED_LIGHTNING_SERIES


async def test_metrics_reflect_lightning_state(async_client):
    await LightningStrike.objects.all().adelete()
    now = datetime.now(tz=UTC)
    await LightningStrike.objects.acreate(struck_at=now, lat=45.0, lon=3.0, intensity=4)
    await LightningStrike.objects.acreate(struck_at=now, lat=46.0, lon=4.0, intensity=2)
    await cache.incr_strikes_total(7)
    await cache.incr_reconnects()
    await cache.incr_queue_dropped(3)
    await cache.set_last_strike_ts(1718960400)
    await cache.set_ws_connected(connected=True, since=1718960000)

    resp = await async_client.get("/metrics")
    values = _parse(resp.content.decode())
    assert values["lightning_strikes_total"] == "7"  # Redis counter
    assert values["lightning_archived_total"] == "2"  # DB rows
    assert values["lightning_strikes_24h"] == "2"
    assert values["lightning_ws_connected"] == "1"
    assert values["lightning_ws_reconnects_total"] == "1"
    assert values["lightning_queue_dropped_total"] == "3"
    assert values["lightning_last_strike_timestamp"] == "1718960400"
    assert int(values["lightning_partitions_total"]) >= 1


async def test_metrics_reflect_seeded_rows(async_client):
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
        tile_count=60,
        status="partial",
        missing=[{"z": 7, "x": 1, "y": 2}],
    )
    await ArchiveGap.objects.acreate(
        service="radar",
        gap_start=datetime.fromtimestamp(1500, tz=UTC),
        gap_end=None,
        reason="ongoing",
        detail={},
    )
    await cache.set_tile_dir_bytes(4096)
    await cache.set_last_poll("rainviewer", 123456)  # writes per-provider + global keys

    resp = await async_client.get("/metrics")
    values = _parse(resp.content.decode())
    assert values["radar_frames_total"] == "2"
    assert values["radar_frames_partial_total"] == "1"
    assert values["radar_archive_gaps_total"] == "1"
    assert values["radar_archive_gap_open"] == "1"
    assert values["radar_tile_dir_bytes"] == "4096"
    assert values["radar_last_poll_timestamp"] == "123456"
    assert values["radar_archive_earliest_timestamp"] == "1000"
    assert values["radar_archive_latest_timestamp"] == "2000"
    # Provider-labelled series land alongside the unlabelled ones.
    assert values['radar_frames_archived_total{provider="rainviewer"}'] == "2"
    assert values['radar_last_poll_timestamp{provider="rainviewer"}'] == "123456"


@override_settings(METEOFRANCE_ENABLED=True)
async def test_metrics_provider_labels_both_when_enabled(async_client):
    await RadarFrame.objects.acreate(
        timestamp=1000,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    await RadarFrame.objects.acreate(
        timestamp=2000,
        provider="meteofrance",
        tile_count=5,
        status="ok",
        missing=[],
    )
    await cache.set_last_poll("rainviewer", 111)
    await cache.set_last_poll("meteofrance", 222)

    values = _parse((await async_client.get("/metrics")).content.decode())
    assert values['radar_frames_archived_total{provider="rainviewer"}'] == "1"
    assert values['radar_frames_archived_total{provider="meteofrance"}'] == "1"
    assert values['radar_last_poll_timestamp{provider="rainviewer"}'] == "111"
    assert values['radar_last_poll_timestamp{provider="meteofrance"}'] == "222"
    # The unlabelled series stays for dashboard continuity.
    assert "radar_last_poll_timestamp" in values
