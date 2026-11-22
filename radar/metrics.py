"""Prometheus text exposition for ``GET /metrics``.

Hand-rolled ``name value`` lines (no Prometheus client dependency), computed from
``radar_frame`` / ``archive_gap`` / ``lightning_strike`` aggregates plus the
archiver-maintained Redis gauges (storage bytes, last poll). The endpoint is public,
so :func:`exposition` caches and single-flights the render.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.db.models import Max
from django.db.models import Min

from radar import cache
from radar.lightning import partitions as lightning_partitions
from radar.models import ArchiveGap
from radar.models import LightningStrike
from radar.models import PushSubscription
from radar.models import RadarFrame
from radar.providers import enabled_providers

CONTENT_TYPE = "text/plain; version=0.0.4"

# -- cached, single-flight exposition ------------------------------------------
# Two layers keep a public, unthrottled-by-us scrape cadence off Postgres:
# - Redis (``METRICS_CACHE_TTL``), shared by every web worker;
# - a per-process memo with the same TTL, so a Redis outage (where every request
#   used to re-render) still renders at most once per TTL per worker.
# And a per-process lock so an expiry under concurrent requests renders ONCE per
# worker instead of once per request (the stampede). Loop-bound like
# ``radar.cache.get_client``: an asyncio primitive belongs to its creating loop, and
# WSGI ``runserver`` makes a fresh loop per request.

_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None
_memo: tuple[float, bytes] | None = None  # (_clock() expiry, exposition)
_clock = time.monotonic  # indirection so tests can move time without touching the loop's clock


def _lock_for_loop() -> asyncio.Lock:
    global _lock, _lock_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def reset_memo() -> None:
    """Drop the per-process memo (tests; the Redis layer is flushed separately)."""
    global _memo  # noqa: PLW0603
    _memo = None


def _fresh_memo() -> bytes | None:
    if _memo is not None and _clock() < _memo[0]:
        return _memo[1]
    return None


async def _from_redis() -> bytes | None:
    try:
        return await cache.get_metrics_text()
    except Exception:  # noqa: BLE001 — Redis hiccup: fall through to the memo/render
        return None


async def exposition() -> bytes:
    """The exposition text, rendered at most once per TTL per worker process."""
    global _memo  # noqa: PLW0603
    if (text := _fresh_memo()) is not None or (text := await _from_redis()) is not None:
        return text
    async with _lock_for_loop():
        # Re-check: a request that held the lock just filled one of the layers.
        if (text := _fresh_memo()) is not None or (text := await _from_redis()) is not None:
            return text
        text = (await render()).encode()
        _memo = (_clock() + settings.METRICS_CACHE_TTL, text)
        with contextlib.suppress(Exception):  # the Redis layer is best-effort
            await cache.set_metrics_text(text, settings.METRICS_CACHE_TTL)
        return text


# Frame statuses, always emitted (zero-filled) so a series never vanishes mid-graph.
_FRAME_STATUSES = ("ok", "partial", "failed")

# Naming rules (Prometheus conventions — keep them when adding a series):
# - ``_total`` only on true counters (monotonic; they reset on a Redis restart, which
#   ``rate()`` handles). Row counts are gauges WITHOUT ``_total``: retention prunes
#   them nightly, and a ``_total`` gauge that drops reads as a counter reset.
# - Every series that differs per radar provider carries ``{provider="…"}`` and has
#   no unlabelled twin, so ``sum()``/``max()`` over the label never double-counts.


def _gauge(name: str, help_text: str, samples: list[tuple[str, object]]) -> list[str]:
    """One gauge family: HELP/TYPE header plus ``(labels, value)`` samples."""
    return [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
        *(f"{name}{labels} {value}" for labels, value in samples),
    ]


def _provider(name: str) -> str:
    return f'{{provider="{name}"}}'


async def render() -> str:
    """Assemble the Prometheus exposition text."""
    day_ago = datetime.now(tz=UTC) - timedelta(hours=24)
    providers = enabled_providers()

    # Scoped to the enabled providers so the series set is stable: a disabled
    # provider's leftover rows (until retention prunes them) never add series.
    frames = RadarFrame.objects.filter(provider__in=providers)
    stored: dict[tuple[str, str], int] = {
        (name, status): 0 for name in providers for status in _FRAME_STATUSES
    }
    async for row in frames.values("provider", "status").annotate(n=Count("id")):
        stored[row["provider"], row["status"]] = row["n"]
    last_24h = dict.fromkeys(providers, 0)
    recent = frames.filter(collected_at__gte=day_ago)
    async for row in recent.values("provider").annotate(n=Count("id")):
        last_24h[row["provider"]] = row["n"]
    bounds = dict.fromkeys(providers, (0, 0))
    async for row in frames.values("provider").annotate(lo=Min("timestamp"), hi=Max("timestamp")):
        bounds[row["provider"]] = (row["lo"] or 0, row["hi"] or 0)
    radar_gaps = ArchiveGap.objects.filter(service="radar", provider__in=providers)
    gaps = dict.fromkeys(providers, 0)
    async for row in radar_gaps.values("provider").annotate(n=Count("id")):
        gaps[row["provider"]] = row["n"]
    open_gaps = dict.fromkeys(providers, 0)
    async for name in radar_gaps.filter(gap_end__isnull=True).values_list("provider", flat=True):
        open_gaps[name] = 1
    last_poll = {name: await cache.get_last_poll(name) for name in providers}

    tile_bytes = await cache.get_tile_dir_bytes()
    capacity = settings.STORAGE_CAPACITY_BYTES
    used_ratio = (tile_bytes / capacity) if capacity else 0.0

    lines = [
        *_gauge(
            "radar_frames_stored",
            "Radar frame rows currently stored, by provider and status (retention prunes).",
            [
                (f'{{provider="{name}",status="{status}"}}', n)
                for (name, status), n in sorted(stored.items())
            ],
        ),
        *_gauge(
            "radar_frames_24h",
            "Frames archived in the last 24h.",
            [(_provider(name), n) for name, n in last_24h.items()],
        ),
        *_gauge(
            "radar_archive_gaps",
            "Gap rows recorded (open and closed).",
            [(_provider(name), n) for name, n in gaps.items()],
        ),
        *_gauge(
            "radar_archive_gap_open",
            "1 if a gap is currently open.",
            [(_provider(name), flag) for name, flag in open_gaps.items()],
        ),
        *_gauge(
            "radar_last_poll_timestamp",
            "Epoch of the last successful poll (0 if none).",
            [(_provider(name), ts) for name, ts in last_poll.items()],
        ),
        *_gauge(
            "radar_archive_earliest_timestamp",
            "Oldest archived frame epoch (0 if empty).",
            [(_provider(name), lo) for name, (lo, _hi) in bounds.items()],
        ),
        *_gauge(
            "radar_archive_latest_timestamp",
            "Newest archived frame epoch (0 if empty).",
            [(_provider(name), hi) for name, (_lo, hi) in bounds.items()],
        ),
        *_gauge(
            "radar_tile_dir_bytes",
            "Tile archive size on disk, all providers (bytes).",
            [("", tile_bytes)],
        ),
        *_gauge(
            "radar_storage_used_ratio",
            "tile_dir_bytes / STORAGE_CAPACITY_BYTES.",
            [("", f"{used_ratio:.6f}")],
        ),
    ]
    lines += await _lightning_series(day_ago)
    lines += await _alert_series()
    if settings.FORECAST_ENABLED:
        lines += await _forecast_series()
    return "\n".join(lines) + "\n"


_FORECAST_COUNTER_HELP = {
    "not_ready": "Probes of a run not published upstream yet (404)",
    "fetch_failed": "Runs that failed on a download or HTTP error",
    "rejected": "Runs rejected for a wrong or undecodable payload",
    "crashed": "Runs that failed on an unexpected error (disk, render, a bug)",
    "bytes": "Bytes downloaded from PIAF",
    "runs_published": "Forecast runs published",
}


async def _forecast_series() -> list[str]:
    """Forecast exposition: the published run's age and lag, plus the job's counters."""
    record = await cache.get_forecast_current() or {}
    run = int(record.get("run", 0))
    published_at = int(record.get("published_at", 0))
    lines = [
        "# HELP forecast_run_timestamp Reference time of the published forecast run (0 if none).",
        "# TYPE forecast_run_timestamp gauge",
        f"forecast_run_timestamp {run}",
        "# HELP forecast_published_timestamp When that run was published (epoch, 0 if none).",
        "# TYPE forecast_published_timestamp gauge",
        f"forecast_published_timestamp {published_at}",
        "# HELP forecast_publication_lag_seconds Run reference time to publication.",
        "# TYPE forecast_publication_lag_seconds gauge",
        f"forecast_publication_lag_seconds {published_at - run if run else 0}",
    ]
    for name, value in (await cache.get_forecast_counters()).items():
        metric = f"forecast_{name}_total"
        lines += [
            f"# HELP {metric} {_FORECAST_COUNTER_HELP[name]} (counter).",
            f"# TYPE {metric} counter",
            f"{metric} {value}",
        ]
    return lines


async def _alert_series() -> list[str]:
    """Storm-alert push exposition: the DB subscription count + Redis counters."""
    subscriptions = await PushSubscription.objects.acount()
    sent = await cache.get_push_sent()
    failed = await cache.get_push_failed()
    pruned = await cache.get_push_pruned()
    return [
        "# HELP alerts_subscriptions Current stored Web Push subscriptions.",
        "# TYPE alerts_subscriptions gauge",
        f"alerts_subscriptions {subscriptions}",
        "# HELP alerts_push_sent_total Push notifications delivered (counter).",
        "# TYPE alerts_push_sent_total counter",
        f"alerts_push_sent_total {sent}",
        "# HELP alerts_push_failed_total Push sends that failed, non-fatally (counter).",
        "# TYPE alerts_push_failed_total counter",
        f"alerts_push_failed_total {failed}",
        "# HELP alerts_push_pruned_total Subscriptions pruned on 404/410 (counter).",
        "# TYPE alerts_push_pruned_total counter",
        f"alerts_push_pruned_total {pruned}",
    ]


async def _lightning_series(day_ago: datetime) -> list[str]:
    """Lightning exposition lines: DB counts + the Redis gauges/counters."""
    strikes_total = await cache.get_strikes_total()
    ws_connected = await cache.get_ws_connected()
    since = await cache.get_ws_connected_since()
    reconnects = await cache.get_reconnects()
    queue_dropped = await cache.get_queue_dropped()
    last_strike = await cache.get_last_strike_ts()
    now_epoch = int(datetime.now(tz=UTC).timestamp())
    ws_uptime = (now_epoch - since) if (ws_connected and since) else 0

    # Cheap DB counts; partition_count is Postgres-only, so degrade to 0 elsewhere.
    strikes_24h = await LightningStrike.objects.filter(struck_at__gte=day_ago).acount()
    strikes_stored = await LightningStrike.objects.acount()
    try:
        partitions_total = await lightning_partitions.partition_count()
    except Exception:  # noqa: BLE001 — non-Postgres / table not ready: report 0
        partitions_total = 0

    return [
        "# HELP lightning_strikes_total Strikes ingested since start (counter).",
        "# TYPE lightning_strikes_total counter",
        f"lightning_strikes_total {strikes_total}",
        "# HELP lightning_strikes_24h Strikes with struck_at in the last 24h.",
        "# TYPE lightning_strikes_24h gauge",
        f"lightning_strikes_24h {strikes_24h}",
        "# HELP lightning_strikes_stored Rows currently in lightning_strike (retention prunes).",
        "# TYPE lightning_strikes_stored gauge",
        f"lightning_strikes_stored {strikes_stored}",
        "# HELP lightning_ws_connected 1 if the Blitzortung WS is currently up.",
        "# TYPE lightning_ws_connected gauge",
        f"lightning_ws_connected {ws_connected}",
        "# HELP lightning_ws_uptime_seconds Seconds since the current WS connect (0 if down).",
        "# TYPE lightning_ws_uptime_seconds gauge",
        f"lightning_ws_uptime_seconds {ws_uptime}",
        "# HELP lightning_ws_reconnects_total WS reconnect attempts (counter).",
        "# TYPE lightning_ws_reconnects_total counter",
        f"lightning_ws_reconnects_total {reconnects}",
        "# HELP lightning_queue_dropped_total Strikes dropped on queue overflow (counter).",
        "# TYPE lightning_queue_dropped_total counter",
        f"lightning_queue_dropped_total {queue_dropped}",
        "# HELP lightning_last_strike_timestamp Epoch of the newest ingested strike (0 if none).",
        "# TYPE lightning_last_strike_timestamp gauge",
        f"lightning_last_strike_timestamp {last_strike}",
        "# HELP lightning_partitions Attached monthly partitions (excl. default).",
        "# TYPE lightning_partitions gauge",
        f"lightning_partitions {partitions_total}",
    ]
