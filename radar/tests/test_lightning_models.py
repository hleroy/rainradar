"""LightningStrike persistence + partition maintenance.

These exercise real PostgreSQL behaviour (declarative partitioning, partition
routing, partition DROP), so they **skip on sqlite** — the suite runs dockerized
against Postgres. The dev sqlite fallback gets a plain table (migration
``0002``) which can't express any of this.
"""

from __future__ import annotations

from datetime import UTC
from datetime import date
from datetime import datetime

import pytest
from django.db import connection

from radar.lightning import partitions
from radar.models import LightningStrike

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="lightning_strike partitioning requires PostgreSQL",
    ),
]


@pytest.fixture(autouse=True)
def _reset_lightning_table():
    """Reset ``lightning_strike`` to a clean baseline around each test.

    The table is ``managed=False``, so Django's between-test truncation skips it
    and rows/partitions created by one test would otherwise leak into the next
    (and across ``--reuse-db`` sessions). TRUNCATE clears every partition (incl.
    DEFAULT), then we re-create the current + next two month partitions so the
    migration baseline is always present even after a drop test.
    """

    def _reset():
        baseline = set(partitions.months_from(partitions.current_month_utc(), 2))
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE lightning_strike")
            # Drop any non-baseline monthly partition a test created.
            for name in partitions.attached_partitions(cursor):
                if name == partitions.DEFAULT_PARTITION:
                    continue
                month = partitions._parse_month(name)
                if month is None or month in baseline:
                    continue
                cursor.execute(f'DROP TABLE IF EXISTS "{name}"')
            partitions.create_month_partitions(cursor, sorted(baseline))

    _reset()
    yield
    _reset()


def _partition_of(ts: datetime) -> str | None:
    """The partition a row with ``struck_at == ts`` physically landed in."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tableoid::regclass::text FROM lightning_strike WHERE struck_at = %s",
            [ts],
        )
        row = cursor.fetchone()
    return row[0] if row else None


def test_insert_lands_and_is_queryable():
    ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    LightningStrike.objects.create(struck_at=ts, lat=45.1, lon=3.2, intensity=7)
    got = LightningStrike.objects.get(struck_at=ts)
    assert got.lat == pytest.approx(45.1, abs=1e-4)
    assert got.lon == pytest.approx(3.2, abs=1e-4)
    assert got.intensity == 7
    assert got.id is not None  # server-generated IDENTITY


def test_bulk_create_and_range_query():
    rows = [
        LightningStrike(struck_at=datetime(2026, 6, 1, h, tzinfo=UTC), lat=44.0, lon=2.0)
        for h in range(5)
    ]
    LightningStrike.objects.bulk_create(rows)
    lo = datetime(2026, 6, 1, 1, tzinfo=UTC)
    hi = datetime(2026, 6, 1, 3, tzinfo=UTC)
    n = LightningStrike.objects.filter(struck_at__gte=lo, struck_at__lte=hi).count()
    assert n == 3


def test_intensity_nullable():
    ts = datetime(2026, 6, 20, 9, 30, tzinfo=UTC)
    LightningStrike.objects.create(struck_at=ts, lat=43.0, lon=1.0, intensity=None)
    assert LightningStrike.objects.get(struck_at=ts).intensity is None


def test_row_routes_to_its_month_partition():
    # Create a far-future month explicitly (independent of "today"), then assert
    # the row lands in *that* partition rather than DEFAULT.
    with connection.cursor() as cursor:
        created = partitions.create_month_partitions(cursor, [date(2027, 3, 1)])
    assert created == ["lightning_strike_2027_03"]
    ts = datetime(2027, 3, 10, 0, 0, tzinfo=UTC)
    LightningStrike.objects.create(struck_at=ts, lat=45.0, lon=3.0)
    assert _partition_of(ts) == "lightning_strike_2027_03"


def test_unpartitioned_month_falls_into_default():
    # A month with no explicit partition must still insert (DEFAULT safety net).
    ts = datetime(2030, 11, 5, 0, 0, tzinfo=UTC)
    LightningStrike.objects.create(struck_at=ts, lat=46.0, lon=4.0)
    assert _partition_of(ts) == "lightning_strike_default"


@pytest.mark.asyncio
async def test_ensure_partitions_is_idempotent():
    created_first = await partitions.ensure_partitions(months_ahead=2)
    created_again = await partitions.ensure_partitions(months_ahead=2)
    # First call may create some (or none, if the migration already did);
    # the second call must create nothing.
    assert created_again == []
    assert isinstance(created_first, list)


@pytest.mark.asyncio
async def test_drop_partitions_before_drops_only_fully_old():
    # Create three explicit historical months around a cutoff.
    await partitions.ensure_partitions(0)  # current month exists
    from asgiref.sync import sync_to_async  # noqa: PLC0415

    def _seed():
        with connection.cursor() as cursor:
            partitions.create_month_partitions(
                cursor,
                [date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)],
            )

    await sync_to_async(_seed)()

    # Cutoff inside February -> only January (whose whole range < cutoff) drops.
    dropped = await partitions.drop_partitions_before(date(2024, 2, 15))
    assert dropped == ["lightning_strike_2024_01"]

    def _names():
        with connection.cursor() as cursor:
            return partitions.attached_partitions(cursor)

    remaining = await sync_to_async(_names)()
    assert "lightning_strike_2024_01" not in remaining
    assert "lightning_strike_2024_02" in remaining
    assert "lightning_strike_2024_03" in remaining
    assert partitions.DEFAULT_PARTITION in remaining  # never dropped


@pytest.mark.asyncio
async def test_drop_partitions_before_never_drops_default():
    # A cutoff far in the future would make every month "old" — DEFAULT survives.
    await partitions.ensure_partitions(0)
    dropped = await partitions.drop_partitions_before(date(2099, 1, 1))
    assert partitions.DEFAULT_PARTITION not in dropped
