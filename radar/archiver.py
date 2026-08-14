"""Archiver logic — poll, persist, gap detection, retention.

Pure, importable async functions so tests drive them directly without
APScheduler (the scheduler bootstrap lives in
``management/commands/run_archiver.py``). All upstream access goes through
``get_active_provider()`` — the archiver never imports a provider directly.

All time math is **UTC** and explicit (``datetime.now(tz=UTC)``,
``time.gmtime`` via :mod:`radar.storage`), independent of Django's display
``TIME_ZONE``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Q

from radar import cache
from radar import storage
from radar import tiles
from radar.logging_json import emit
from radar.models import ArchiveGap
from radar.models import PushSubscription
from radar.models import RadarFrame
from radar.providers import get_active_provider
from radar.providers.base import FramesUnavailable
from radar.providers.base import RateLimited
from radar.providers.base import TileUpstreamError

logger = logging.getLogger("radar.archiver")

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

GAP_REASON_UNREACHABLE = "upstream unreachable"
GAP_REASON_AGED_OUT = "archiver outage exceeded the upstream backfill window"


def _matrix() -> list[tuple[int, int, int]]:
    """The computed (z, x, y) tile matrix, sorted for deterministic iteration."""
    return sorted(
        tiles.tile_matrix(settings.RADAR_BBOX, settings.RADAR_ZOOM_MIN, settings.RADAR_ZOOM_MAX),
    )


# -- one frame ----------------------------------------------------------------


def _frame_status(*, missing_count: int, tile_count: int) -> str:
    """`ok` (nothing errored), `partial` (some tiles landed), or `failed` (none did)."""
    if not missing_count:
        return STATUS_OK
    return STATUS_FAILED if tile_count == 0 else STATUS_PARTIAL


async def _known_empty(provider_name: str, ts: int) -> set[tuple[int, int, int]]:
    """Tiles a previous run already learned this frame has nothing to draw for.

    A published frame is immutable upstream, so a tile that came back empty once is
    empty forever — and "no file on disk" alone cannot tell "empty" from "never
    fetched". Without this, every retry of a partial frame re-fetches all of its
    empty tiles from scratch.
    """
    prior = (
        await RadarFrame.objects.filter(provider=provider_name, timestamp=ts)
        .values_list("empty", flat=True)
        .afirst()
    )
    return {(e["z"], e["x"], e["y"]) for e in prior or []}


async def archive_frame(provider, ts: int) -> RadarFrame:
    """Fetch + persist every matrix tile for one frame, then upsert its row.

    Idempotent: tiles already on disk — and tiles already known to be empty — are
    skipped (no upstream fetch, no double count), so a re-run after a partial
    archive only retries what actually failed.

    Aborts the remaining tiles as soon as upstream reports a rate limit. Grinding
    the rest of the matrix into a 429 wall only deepens the limit, and every tile
    would fail anyway; the untried tiles are recorded as missing so the next poll
    retries them once the cooldown has lapsed. The caller reads
    ``frame.rate_limited`` to stop the backfill loop.
    """
    matrix = _matrix()
    date = storage.utc_date(ts)
    existing = 0
    written = 0
    bytes_written = 0
    missing: list[dict[str, int]] = []
    empty: set[tuple[int, int, int]] = await _known_empty(provider.name, ts)
    sem = asyncio.Semaphore(settings.TILE_FETCH_CONCURRENCY)
    throttled = asyncio.Event()

    async def fetch_one(client, z: int, x: int, y: int) -> None:
        nonlocal existing, written, bytes_written
        if (z, x, y) in empty:
            return  # known empty — nothing to fetch, nothing to write
        if await sync_to_async(storage.tile_exists)(provider.name, ts, z, x, y, date=date):
            existing += 1
            return
        async with sem:
            # Re-checked inside the semaphore too: tiles queue here, and one that
            # was admitted before the limit tripped must not still go out after.
            if throttled.is_set():
                missing.append({"z": z, "x": x, "y": y})
                return
            try:
                data = await provider.get_tile(ts, z, x, y, client=client)
            except RateLimited:
                throttled.set()
                missing.append({"z": z, "x": x, "y": y})
                return
            except TileUpstreamError:
                missing.append({"z": z, "x": x, "y": y})
                return
        if data is None:
            empty.add((z, x, y))  # empty region (404) — store nothing, remember it
            return
        await sync_to_async(storage.write_tile)(provider.name, ts, z, x, y, data, date=date)
        written += 1
        bytes_written += len(data)

    # One pooled client per frame: the 62 tile fetches reuse keep-alive instead
    # of re-handshaking TLS per tile.
    async with provider.tile_client() as client:
        await asyncio.gather(*(fetch_one(client, z, x, y) for (z, x, y) in matrix))

    tile_count = existing + written
    status = _frame_status(missing_count=len(missing), tile_count=tile_count)

    # Keyed on (provider, timestamp): both providers can emit the same epoch, so the
    # timestamp alone is no longer unique.
    frame, _created = await RadarFrame.objects.aupdate_or_create(
        provider=provider.name,
        timestamp=ts,
        defaults={
            "tile_count": tile_count,
            "status": status,
            "missing": missing,
            "empty": [{"z": z, "x": x, "y": y} for (z, x, y) in sorted(empty)],
        },
    )
    # Transient (not persisted): bytes written this run, for the storage gauge, and
    # whether upstream throttled us — poll_radar stops the backfill on that.
    frame.bytes_written = bytes_written
    frame.rate_limited = throttled.is_set()
    # Only fully-archived frames join the "done" set, so partial/failed frames
    # are retried on the next poll (the existing-file check keeps that cheap).
    if status == STATUS_OK:
        await cache.add_archived(provider.name, ts)

    emit(
        logger,
        logging.WARNING if missing else logging.INFO,
        "frame_archived",
        provider=provider.name,
        ts=ts,
        tiles_written=written,
        missing=len(missing),
        status=status,
        rate_limited=throttled.is_set(),
    )
    return frame


# -- gaps ---------------------------------------------------------------------


async def _ongoing_gap_exists(provider_name: str) -> bool:
    return await ArchiveGap.objects.filter(
        service="radar",
        provider=provider_name,
        gap_end__isnull=True,
    ).aexists()


async def open_ongoing_gap(provider_name: str, reason: str, detail: dict | None = None) -> None:
    """Open an ongoing (unbounded) gap for a provider if none is already open."""
    if await _ongoing_gap_exists(provider_name):
        return  # don't stack duplicate ongoing gaps
    now = datetime.now(tz=UTC)
    await ArchiveGap.objects.acreate(
        service="radar",
        provider=provider_name,
        gap_start=now,
        gap_end=None,
        reason=reason,
        detail=detail or {},
    )
    emit(
        logger,
        logging.ERROR,
        "gap_opened",
        provider=provider_name,
        gap_start=now.isoformat(),
        reason=reason,
    )


async def close_ongoing_gaps(provider_name: str) -> int:
    """Close any open ongoing gap for a provider on a successful poll."""
    now = datetime.now(tz=UTC)
    closed = 0
    async for gap in ArchiveGap.objects.filter(
        service="radar",
        provider=provider_name,
        gap_end__isnull=True,
    ):
        gap.gap_end = now
        await gap.asave(update_fields=["gap_end"])
        emit(
            logger,
            logging.INFO,
            "gap_closed",
            provider=provider_name,
            gap_start=gap.gap_start.isoformat(),
            gap_end=now.isoformat(),
            reason=gap.reason,
        )
        closed += 1
    return closed


async def detect_aged_out_gap(
    provider_name: str,
    prev_max: int | None,
    oldest_up: int,
    interval: int,
) -> None:
    """Record an unrecoverable gap when frames aged out of the upstream window.

    Frames between ``prev_max`` and ``oldest_up`` were never archived and have now
    fallen out of the upstream's backfill window — a closed, unrecoverable gap.
    ``interval`` is the provider's frame cadence (``provider.frame_interval``).
    """
    if prev_max is None or oldest_up <= prev_max + interval * 1.5:
        return
    gap_start = datetime.fromtimestamp(prev_max + interval, tz=UTC)
    gap_end = datetime.fromtimestamp(oldest_up, tz=UTC)
    # Don't stack an aged-out gap on top of one already covering this window —
    # e.g. an ongoing gap (unreachable→recovered) closed over the same outage.
    overlap = (
        await ArchiveGap.objects.filter(
            service="radar",
            provider=provider_name,
            gap_start__lte=gap_end,
        )
        .filter(Q(gap_end__isnull=True) | Q(gap_end__gte=gap_start))
        .aexists()
    )
    if overlap:
        return
    await ArchiveGap.objects.aupdate_or_create(
        service="radar",
        provider=provider_name,
        gap_start=gap_start,
        defaults={
            "gap_end": gap_end,
            "reason": GAP_REASON_AGED_OUT,
            "detail": {
                "expected_interval": interval,
                "prev_max": prev_max,
                "oldest_up": oldest_up,
            },
        },
    )
    emit(
        logger,
        logging.ERROR,
        "gap_opened",
        provider=provider_name,
        gap_start=gap_start.isoformat(),
        gap_end=gap_end.isoformat(),
        reason=GAP_REASON_AGED_OUT,
    )
    emit(
        logger,
        logging.INFO,
        "gap_closed",
        provider=provider_name,
        gap_start=gap_start.isoformat(),
        gap_end=gap_end.isoformat(),
        reason=GAP_REASON_AGED_OUT,
    )


# -- archived-set cold start --------------------------------------------------


@sync_to_async
def _ok_frame_timestamps(provider_name: str) -> list[int]:
    return list(
        RadarFrame.objects.filter(provider=provider_name, status=STATUS_OK).values_list(
            "timestamp",
            flat=True,
        ),
    )


# -- the poll -----------------------------------------------------------------


async def poll_radar(provider=None) -> dict:
    """One poll cycle: backfill new frames, detect/close gaps, update gauges."""
    provider = provider or get_active_provider()
    t0 = time.monotonic()
    emit(logger, logging.INFO, "poll_start", provider=provider.name)

    # 1. Live ~2h past window.
    try:
        frames = await provider.get_frames()
    except FramesUnavailable:
        consecutive = await _bump_failures(provider.name)
        emit(
            logger,
            logging.WARNING,
            "fetch_failed",
            provider=provider.name,
            target="frames",
            consecutive=consecutive,
        )
        # Only open a gap once the outage is sustained, so a single flaky poll
        # doesn't churn open/close gap rows.
        if consecutive >= settings.GAP_OPEN_AFTER_FAILURES:
            await open_ongoing_gap(provider.name, GAP_REASON_UNREACHABLE, {"target": "frames"})
        return {"frames_seen": 0, "archived": 0, "tiles_written": 0, "ok": False}

    await _reset_failures(provider.name)
    await close_ongoing_gaps(provider.name)

    # 2. Archived set (rebuilt from DB on cold start / after a Redis flush).
    archived = await cache.get_archived(provider.name)
    cold_start = not archived
    if cold_start:
        archived = set(await _ok_frame_timestamps(provider.name))
        await cache.replace_archived(provider.name, archived)

    prev_max = max(archived) if archived else None

    # 3. Backfill anything not yet fully archived (oldest first).
    missing_ts = sorted(f.timestamp for f in frames if f.timestamp not in archived)
    tiles_written = 0
    bytes_written = 0
    archived_now = 0
    for index, ts in enumerate(missing_ts):
        try:
            frame = await archive_frame(provider, ts)
        except Exception:
            emit(logger, logging.ERROR, "frame_failed", provider=provider.name, ts=ts)
            logger.exception("archive_frame(%s, %s) crashed", provider.name, ts)
            continue
        tiles_written += frame.tile_count
        bytes_written += frame.bytes_written
        archived_now += 1
        if cold_start:
            emit(logger, logging.INFO, "backfill_recovered", provider=provider.name, ts=ts)
        # Upstream is throttling us. A cold start has a whole window of frames
        # queued here, and every one of them would fail the same way while making
        # the limit worse — so give up the rest of this poll and let the next one
        # resume, by which time the provider's cooldown has lapsed. Nothing is
        # lost: unarchived frames stay out of the archived set and are retried.
        if getattr(frame, "rate_limited", False):
            emit(
                logger,
                logging.WARNING,
                "poll_throttled",
                provider=provider.name,
                ts=ts,
                frames_deferred=len(missing_ts) - index - 1,
            )
            break

    # 4. Gap detection for frames that aged out before we ever saw them.
    if frames:
        await detect_aged_out_gap(
            provider.name,
            prev_max,
            min(f.timestamp for f in frames),
            provider.frame_interval,
        )

    # 5. Gauges. Increment by the bytes this poll wrote (O(1)); the absolute
    #    value is reconciled by a full rescan at startup and in the daily janitor.
    await cache.incr_tile_dir_bytes(bytes_written)
    now_epoch = int(datetime.now(tz=UTC).timestamp())
    await cache.set_last_poll(provider.name, now_epoch)

    duration_ms = int((time.monotonic() - t0) * 1000)
    emit(
        logger,
        logging.INFO,
        "poll_complete",
        provider=provider.name,
        frames_seen=len(frames),
        archived=archived_now,
        tiles_written=tiles_written,
        duration_ms=duration_ms,
    )
    return {
        "frames_seen": len(frames),
        "archived": archived_now,
        "tiles_written": tiles_written,
        "ok": True,
    }


# -- consecutive-failure counter ----------------------------------------------


def _failures_key(provider_name: str) -> str:
    return f"radar:{provider_name}:consec_failures"


async def _bump_failures(provider_name: str) -> int:
    return int(await cache.get_client().incr(_failures_key(provider_name)))


async def _reset_failures(provider_name: str) -> None:
    await cache.get_client().delete(_failures_key(provider_name))


# -- storage gauge ------------------------------------------------------------


async def update_storage_gauge() -> int:
    size = await sync_to_async(storage.tile_tree_bytes)()
    await cache.set_tile_dir_bytes(size)
    return size


# -- retention janitor --------------------------------------------------------


def _purge_old_day_dirs(cutoff_date) -> tuple[int, int]:
    """Delete whole day dirs older than ``cutoff_date`` under EVERY provider.

    Iterates every provider subtree present on disk (not just the active one) so a
    provider turned off after use is still pruned. Returns (days, bytes).
    """
    deleted_days = 0
    freed = 0
    for provider_name in storage.provider_dirs():
        for d in storage.day_dirs(provider_name):
            try:
                day = datetime.strptime(d.name, "%Y-%m-%d").replace(tzinfo=UTC).date()
            except ValueError:
                continue
            if day < cutoff_date:
                freed += storage.dir_size(d)
                shutil.rmtree(d, ignore_errors=True)
                deleted_days += 1
    return deleted_days, freed


async def retention_janitor() -> dict:
    """Prune tiles + rows older than ``RETENTION_DAYS``. Safe as a no-op."""
    t0 = time.monotonic()
    now = datetime.now(tz=UTC)
    cutoff_date = (now - timedelta(days=settings.RETENTION_DAYS)).date()

    deleted_days, freed = await sync_to_async(_purge_old_day_dirs)(cutoff_date)

    # Rows whose UTC date is strictly before the cutoff day (ts < cutoff midnight).
    cutoff_midnight = datetime(cutoff_date.year, cutoff_date.month, cutoff_date.day, tzinfo=UTC)
    cutoff_ts = int(cutoff_midnight.timestamp())
    # Drop each provider's archived-set entries for its purged timestamps (timestamps
    # collide across providers, so this must be provider-scoped). Scope to the
    # providers actually present in the purged rows — not just the enabled ones — so a
    # provider turned off after use still has its Redis archived set pruned.
    purged_providers = [
        name
        async for name in RadarFrame.objects.filter(timestamp__lt=cutoff_ts)
        .values_list("provider", flat=True)
        .distinct()
    ]
    for provider_name in purged_providers:
        purged_ts = [
            ts
            async for ts in RadarFrame.objects.filter(
                provider=provider_name,
                timestamp__lt=cutoff_ts,
            ).values_list("timestamp", flat=True)
        ]
        await cache.remove_archived(provider_name, purged_ts)
    # Then delete all aged-out rows in one shot (any provider, incl. now-disabled ones).
    deleted_frames, _ = await RadarFrame.objects.filter(timestamp__lt=cutoff_ts).adelete()

    # Lightning shares this daily run: drop whole month partitions older
    # than the lightning horizon. Isolated — a lightning failure here must never
    # fail radar retention, so it is caught and logged, not raised.
    lightning_dropped = await _prune_lightning_partitions(now)

    # Storm alerts share this daily run: prune push subscriptions not
    # refreshed within PUSH_STALE_DAYS. Isolated like the lightning clause — a failure
    # here must never fail radar retention, so it is caught and logged, not raised.
    subscriptions_pruned = await _prune_stale_subscriptions(now)

    # Daily full-rescan reconcile of the storage gauge (corrects incremental drift
    # and any fallback-view writes the per-poll increment didn't see).
    await update_storage_gauge()

    duration_ms = int((time.monotonic() - t0) * 1000)
    emit(
        logger,
        logging.INFO,
        "retention_run",
        deleted_days=deleted_days,
        deleted_frames=deleted_frames,
        freed_bytes=freed,
        lightning_partitions_dropped=lightning_dropped,
        subscriptions_pruned=subscriptions_pruned,
        duration_ms=duration_ms,
    )
    return {
        "deleted_days": deleted_days,
        "deleted_frames": deleted_frames,
        "freed_bytes": freed,
        "lightning_partitions_dropped": lightning_dropped,
        "subscriptions_pruned": subscriptions_pruned,
    }


async def _prune_stale_subscriptions(now: datetime) -> int:
    """Delete push subscriptions untouched past the horizon, isolated from radar."""
    if not settings.PUSH_ALERTS_ENABLED:
        return 0
    cutoff = now - timedelta(days=settings.PUSH_STALE_DAYS)
    try:
        deleted, _ = await PushSubscription.objects.filter(last_seen_at__lt=cutoff).adelete()
    except Exception:
        emit(logger, logging.ERROR, "push_retention_failed", service="alerts")
        logger.exception("push subscription retention failed")
        return 0
    return deleted


async def _prune_lightning_partitions(now: datetime) -> int:
    """Drop lightning month partitions past the horizon, isolated from radar."""
    if not settings.LIGHTNING_ENABLED:
        return 0
    cutoff_date = (now - timedelta(days=settings.LIGHTNING_RETENTION_DAYS)).date()
    try:
        from radar.lightning import partitions as lightning_partitions  # noqa: PLC0415

        dropped = await lightning_partitions.drop_partitions_before(cutoff_date)
    except Exception:
        emit(logger, logging.ERROR, "lightning_retention_failed", service="lightning")
        logger.exception("lightning partition retention failed")
        return 0
    return len(dropped)
