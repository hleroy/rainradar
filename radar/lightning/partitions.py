"""Monthly RANGE partition maintenance for ``lightning_strike``.

The table itself is declared in migration ``0002`` (raw SQL); these helpers keep
its month partitions in shape afterwards:

* :func:`ensure_partitions` pre-creates the current month + ``months_ahead`` so an
  insert never falls into the slow ``DEFAULT`` partition (and never *fails*) on
  the 1st. Idempotent — ``CREATE TABLE IF NOT EXISTS``.
* :func:`drop_partitions_before` is retention: it ``DROP``s whole month partitions
  whose entire range is older than the cutoff, instead of a per-row ``DELETE``
. The ``DEFAULT`` partition is the safety net and is **never**
  dropped.

All month math is **UTC** and computed — no hardcoded dates. The DDL is
raw SQL via ``connection.cursor()``; the partition bounds/names are computed
internally (never user input), so direct string formatting is safe here.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC
from datetime import date
from datetime import datetime

from asgiref.sync import sync_to_async
from django.db import connection

from radar.logging_json import emit

logger = logging.getLogger("radar.lightning")

PARENT = "lightning_strike"
DEFAULT_PARTITION = "lightning_strike_default"
_NAME_RE = re.compile(r"^lightning_strike_(\d{4})_(\d{2})$")
_DECEMBER = 12


# -- month arithmetic (pure, UTC) ---------------------------------------------


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    """First day of the month after the one containing ``d``."""
    if d.month == _DECEMBER:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _partition_name(month_start: date) -> str:
    return f"{PARENT}_{month_start.year:04d}_{month_start.month:02d}"


def _parse_month(name: str) -> date | None:
    """``lightning_strike_2026_06`` -> ``date(2026, 6, 1)``; else ``None``."""
    m = _NAME_RE.match(name)
    if m is None:
        return None
    return date(int(m.group(1)), int(m.group(2)), 1)


def months_from(start_month: date, months_ahead: int) -> list[date]:
    """``[start_month, +1, … +months_ahead]`` (each the 1st of its month)."""
    months = [start_month]
    cur = start_month
    for _ in range(months_ahead):
        cur = _next_month(cur)
        months.append(cur)
    return months


def current_month_utc() -> date:
    return _month_start(datetime.now(tz=UTC).date())


# -- raw-SQL primitives (cursor-driven, reused by the migration) --------------


def attached_partitions(cursor) -> set[str]:
    """Names of the partitions currently attached to ``lightning_strike``."""
    cursor.execute(
        """
        SELECT child.relname
        FROM pg_inherits
        JOIN pg_class child ON child.oid = pg_inherits.inhrelid
        JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
        WHERE parent.relname = %s
        """,
        [PARENT],
    )
    return {row[0] for row in cursor.fetchall()}


def create_month_partitions(cursor, months: list[date]) -> list[str]:
    """Create any missing month partitions in ``months``. Returns ones created.

    Shared by :func:`ensure_partitions` and migration ``0002`` so the DDL lives in
    exactly one place. Idempotent: existing partitions are skipped.
    """
    existing = attached_partitions(cursor)
    created: list[str] = []
    for month in months:
        name = _partition_name(month)
        if name in existing:
            continue
        nxt = _next_month(month)
        cursor.execute(
            f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{PARENT}" '
            f"FOR VALUES FROM ('{month.isoformat()} 00:00:00+00') "
            f"TO ('{nxt.isoformat()} 00:00:00+00')",
        )
        created.append(name)
        emit(logger, logging.INFO, "partition_created", service="lightning", partition=name)
    return created


# -- async API (archiver boot + scheduled maintenance + janitor) --------------


def _ensure_partitions_sync(months_ahead: int) -> list[str]:
    with connection.cursor() as cursor:
        return create_month_partitions(cursor, months_from(current_month_utc(), months_ahead))


async def ensure_partitions(months_ahead: int = 2) -> list[str]:
    """Pre-create the current month + ``months_ahead`` partitions (idempotent)."""
    return await sync_to_async(_ensure_partitions_sync)(months_ahead)


def _drop_partitions_before_sync(cutoff_date: date) -> list[str]:
    dropped: list[str] = []
    with connection.cursor() as cursor:
        for name in sorted(attached_partitions(cursor)):
            if name == DEFAULT_PARTITION:
                continue  # never drop the safety net
            month = _parse_month(name)
            if month is None:
                continue  # unrecognised child — leave it alone
            # Drop only if the partition's whole range is older than the cutoff:
            # its exclusive upper bound (first day of the next month) <= cutoff.
            if _next_month(month) <= cutoff_date:
                cursor.execute(f'DROP TABLE IF EXISTS "{name}"')
                dropped.append(name)
                emit(logger, logging.INFO, "partition_dropped", service="lightning", partition=name)
    return dropped


async def drop_partitions_before(cutoff_date: date) -> list[str]:
    """Drop month partitions whose entire range is older than ``cutoff_date``.

    Never touches the ``DEFAULT`` partition. Returns the names dropped.
    """
    return await sync_to_async(_drop_partitions_before_sync)(cutoff_date)


def _partition_count_sync() -> int:
    with connection.cursor() as cursor:
        return len(attached_partitions(cursor) - {DEFAULT_PARTITION})


async def partition_count() -> int:
    """Number of attached monthly partitions, excluding ``DEFAULT`` (for /metrics)."""
    return await sync_to_async(_partition_count_sync)()
