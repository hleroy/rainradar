"""Retention janitor — prunes >RETENTION_DAYS tiles + rows."""

from __future__ import annotations

from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.test import override_settings

from radar import archiver
from radar import cache
from radar import storage
from radar.lightning import partitions
from radar.models import RadarFrame

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

requires_pg = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="lightning partitioning requires PostgreSQL",
)


@pytest.fixture
def tile_root(tmp_path):
    with override_settings(TILE_ROOT=str(tmp_path), RETENTION_DAYS=90):
        yield tmp_path


async def test_janitor_deletes_old_keeps_recent(tile_root):
    now = datetime.now(tz=UTC)
    old_ts = int((now - timedelta(days=100)).timestamp())
    recent_ts = int((now - timedelta(days=1)).timestamp())

    for ts in (old_ts, recent_ts):
        storage.write_tile("rainviewer", ts, 5, 16, 11, b"radartile")
        await RadarFrame.objects.acreate(
            timestamp=ts,
            provider="rainviewer",
            tile_count=1,
            status="ok",
            missing=[],
        )
        await cache.add_archived("rainviewer", ts)

    old_dir = tile_root / "rainviewer" / storage.utc_date(old_ts)
    recent_dir = tile_root / "rainviewer" / storage.utc_date(recent_ts)
    assert old_dir.is_dir()
    assert recent_dir.is_dir()

    result = await archiver.retention_janitor()

    assert result["deleted_days"] == 1
    assert result["deleted_frames"] == 1
    assert result["freed_bytes"] > 0
    assert not old_dir.exists()
    assert recent_dir.is_dir()
    assert await RadarFrame.objects.filter(timestamp=old_ts).acount() == 0
    assert await RadarFrame.objects.filter(timestamp=recent_ts).acount() == 1
    # The purged timestamp is dropped from the archived set; the recent one stays.
    assert await cache.is_archived("rainviewer", old_ts) is False
    assert await cache.is_archived("rainviewer", recent_ts) is True


async def test_janitor_noop_when_nothing_due(tile_root):
    now = datetime.now(tz=UTC)
    recent_ts = int((now - timedelta(days=1)).timestamp())
    storage.write_tile("rainviewer", recent_ts, 5, 16, 11, b"x")
    await RadarFrame.objects.acreate(
        timestamp=recent_ts,
        provider="rainviewer",
        tile_count=1,
        status="ok",
        missing=[],
    )

    result = await archiver.retention_janitor()

    assert result["deleted_days"] == 0
    assert result["deleted_frames"] == 0
    assert result["freed_bytes"] == 0
    assert result["lightning_partitions_dropped"] == 0  # lightning disabled by default
    assert await RadarFrame.objects.acount() == 1


async def test_janitor_prunes_all_provider_trees(tile_root):
    """Old day dirs + rows + archived-set entries are pruned under every provider."""
    now = datetime.now(tz=UTC)
    old_ts = int((now - timedelta(days=100)).timestamp())
    with override_settings(METEOFRANCE_ENABLED=True):
        for name in ("rainviewer", "meteofrance"):
            storage.write_tile(name, old_ts, 5, 16, 11, b"tile")
            await RadarFrame.objects.acreate(
                timestamp=old_ts,
                provider=name,
                tile_count=1,
                status="ok",
                missing=[],
            )
            await cache.add_archived(name, old_ts)

        result = await archiver.retention_janitor()

    assert result["deleted_days"] == 2  # one old day dir under each provider subtree
    assert result["deleted_frames"] == 2
    assert not (tile_root / "rainviewer" / storage.utc_date(old_ts)).exists()
    assert not (tile_root / "meteofrance" / storage.utc_date(old_ts)).exists()
    assert await cache.is_archived("rainviewer", old_ts) is False
    assert await cache.is_archived("meteofrance", old_ts) is False


async def test_janitor_prunes_archived_set_of_disabled_provider(tile_root):
    """A provider turned off after use still gets its Redis archived set pruned."""
    now = datetime.now(tz=UTC)
    old_ts = int((now - timedelta(days=100)).timestamp())
    # meteofrance rows/tiles/archived set exist, but the provider is now DISABLED.
    storage.write_tile("meteofrance", old_ts, 5, 16, 11, b"tile")
    await RadarFrame.objects.acreate(
        timestamp=old_ts,
        provider="meteofrance",
        tile_count=1,
        status="ok",
        missing=[],
    )
    await cache.add_archived("meteofrance", old_ts)

    with override_settings(METEOFRANCE_ENABLED=False):
        result = await archiver.retention_janitor()

    assert result["deleted_frames"] == 1
    assert not (tile_root / "meteofrance" / storage.utc_date(old_ts)).exists()
    assert await cache.is_archived("meteofrance", old_ts) is False  # pruned despite disabled


@requires_pg
async def test_janitor_drops_old_lightning_partitions(tile_root):
    # A long-past month partition is older than the 90-day horizon and must be
    # dropped; the current month must survive. Lightning must be enabled.
    def _seed():
        with connection.cursor() as cursor:
            partitions.create_month_partitions(cursor, [date(2024, 1, 1)])
            return partitions.attached_partitions(cursor)

    before = await sync_to_async(_seed)()
    assert "lightning_strike_2024_01" in before

    with override_settings(LIGHTNING_ENABLED=True, LIGHTNING_RETENTION_DAYS=90):
        result = await archiver.retention_janitor()

    assert result["lightning_partitions_dropped"] >= 1

    def _snapshot():
        with connection.cursor() as cursor:
            return partitions.attached_partitions(cursor)

    remaining = await sync_to_async(_snapshot)()
    assert "lightning_strike_2024_01" not in remaining  # whole old month dropped
    assert partitions.DEFAULT_PARTITION in remaining  # never dropped
    current = partitions._partition_name(partitions.current_month_utc())
    assert current in remaining  # current month survives
