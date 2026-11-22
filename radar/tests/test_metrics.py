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
    'radar_frames_stored{provider="rainviewer",status="ok"}',
    'radar_frames_stored{provider="rainviewer",status="partial"}',
    'radar_frames_stored{provider="rainviewer",status="failed"}',
    'radar_frames_24h{provider="rainviewer"}',
    'radar_archive_gaps{provider="rainviewer"}',
    'radar_archive_gap_open{provider="rainviewer"}',
    'radar_last_poll_timestamp{provider="rainviewer"}',
    'radar_archive_earliest_timestamp{provider="rainviewer"}',
    'radar_archive_latest_timestamp{provider="rainviewer"}',
    "radar_tile_dir_bytes",
    "radar_storage_used_ratio",
}

EXPECTED_LIGHTNING_SERIES = {
    "lightning_strikes_total",
    "lightning_strikes_24h",
    "lightning_strikes_stored",
    "lightning_ws_connected",
    "lightning_ws_uptime_seconds",
    "lightning_ws_reconnects_total",
    "lightning_queue_dropped_total",
    "lightning_last_strike_timestamp",
    "lightning_partitions",
}


def _parse(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, value = line.split(" ", 1)
        values[name] = value
    return values


def _types(text: str) -> dict[str, str]:
    """``{family: type}`` from the ``# TYPE`` lines."""
    return {
        parts[2]: parts[3]
        for parts in (line.split() for line in text.splitlines() if line.startswith("# TYPE "))
    }


async def test_metrics_content_type_and_series(async_client):
    resp = await async_client.get("/metrics")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/plain; version=0.0.4"
    values = _parse(resp.content.decode())
    assert set(values) >= EXPECTED_SERIES
    assert set(values) >= EXPECTED_LIGHTNING_SERIES


@override_settings(FORECAST_ENABLED=True, METEOFRANCE_ENABLED=True)
async def test_metric_names_follow_prometheus_conventions(async_client):
    """``_total`` marks counters only. A gauge named ``_total`` that drops when
    retention prunes rows reads as a counter reset and spikes every ``rate()``."""
    text = (await async_client.get("/metrics")).content.decode()
    types = _types(text)
    assert types, "no # TYPE lines"
    for family, kind in types.items():
        if kind == "counter":
            assert family.endswith("_total"), f"counter {family} must end in _total"
        else:
            assert not family.endswith("_total"), f"{kind} {family} must not end in _total"
    # Every sample belongs to a declared family, so nothing is untyped.
    for sample in _parse(text):
        assert sample.split("{", 1)[0] in types, f"{sample} has no # TYPE"


@override_settings(METEOFRANCE_ENABLED=True)
async def test_per_provider_radar_series_have_no_unlabelled_twin(async_client):
    """sum()/max() over ``provider`` must never double-count an aggregate twin."""
    values = _parse((await async_client.get("/metrics")).content.decode())
    global_families = {"radar_tile_dir_bytes", "radar_storage_used_ratio"}
    for sample in values:
        family = sample.split("{", 1)[0]
        if family.startswith("radar_") and family not in global_families:
            assert "provider=" in sample, f"{sample} lacks a provider label"


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
    assert values["lightning_strikes_stored"] == "2"  # DB rows
    assert values["lightning_strikes_24h"] == "2"
    assert values["lightning_ws_connected"] == "1"
    assert values["lightning_ws_reconnects_total"] == "1"
    assert values["lightning_queue_dropped_total"] == "3"
    assert values["lightning_last_strike_timestamp"] == "1718960400"
    assert int(values["lightning_partitions"]) >= 1


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
    await cache.set_last_poll("rainviewer", 123456)

    resp = await async_client.get("/metrics")
    values = _parse(resp.content.decode())
    rv = '{provider="rainviewer"}'
    assert values['radar_frames_stored{provider="rainviewer",status="ok"}'] == "1"
    assert values['radar_frames_stored{provider="rainviewer",status="partial"}'] == "1"
    assert values['radar_frames_stored{provider="rainviewer",status="failed"}'] == "0"  # zero-fill
    assert values[f"radar_archive_gaps{rv}"] == "1"
    assert values[f"radar_archive_gap_open{rv}"] == "1"
    assert values["radar_tile_dir_bytes"] == "4096"
    assert values[f"radar_last_poll_timestamp{rv}"] == "123456"
    assert values[f"radar_archive_earliest_timestamp{rv}"] == "1000"
    assert values[f"radar_archive_latest_timestamp{rv}"] == "2000"
    assert "radar_last_poll_timestamp" not in values  # no unlabelled twin


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

    await ArchiveGap.objects.acreate(
        service="radar",
        provider="meteofrance",
        gap_start=datetime.fromtimestamp(1500, tz=UTC),
        gap_end=None,
        reason="ongoing",
        detail={},
    )

    values = _parse((await async_client.get("/metrics")).content.decode())
    assert values['radar_frames_stored{provider="rainviewer",status="ok"}'] == "1"
    assert values['radar_frames_stored{provider="meteofrance",status="ok"}'] == "1"
    assert values['radar_last_poll_timestamp{provider="rainviewer"}'] == "111"
    assert values['radar_last_poll_timestamp{provider="meteofrance"}'] == "222"
    # A Météo-France gap no longer looks like a RainViewer one.
    assert values['radar_archive_gap_open{provider="meteofrance"}'] == "1"
    assert values['radar_archive_gap_open{provider="rainviewer"}'] == "0"
    assert values['radar_archive_gaps{provider="meteofrance"}'] == "1"
    assert values['radar_archive_gaps{provider="rainviewer"}'] == "0"


@override_settings(METEOFRANCE_ENABLED=False)
async def test_disabled_provider_rows_add_no_series(async_client):
    """Leftover rows of a provider turned off never add series until pruned."""
    await RadarFrame.objects.acreate(
        timestamp=2000, provider="meteofrance", tile_count=5, status="ok", missing=[]
    )
    values = _parse((await async_client.get("/metrics")).content.decode())
    assert not any('provider="meteofrance"' in sample for sample in values)


async def test_forecast_series_only_with_the_flag(async_client):
    values = _parse((await async_client.get("/metrics")).content.decode())
    assert not any(name.startswith("forecast_") for name in values)


@override_settings(FORECAST_ENABLED=True)
async def test_forecast_series(async_client):
    await cache.set_forecast_current({"run": 1794395700, "published_at": 1794396250})
    await cache.incr_forecast("not_ready", 3)
    await cache.incr_forecast("bytes", 42_000_000)
    values = _parse((await async_client.get("/metrics")).content.decode())

    assert values["forecast_run_timestamp"] == "1794395700"
    assert values["forecast_publication_lag_seconds"] == "550"
    assert values["forecast_not_ready_total"] == "3"
    assert values["forecast_bytes_total"] == "42000000"
    assert values["forecast_rejected_total"] == "0"
    assert values["forecast_crashed_total"] == "0"


@override_settings(FORECAST_ENABLED=True)
async def test_forecast_series_before_any_run(async_client):
    values = _parse((await async_client.get("/metrics")).content.decode())
    assert values["forecast_run_timestamp"] == "0"
    assert values["forecast_publication_lag_seconds"] == "0"
