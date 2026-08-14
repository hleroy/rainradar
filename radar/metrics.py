"""Prometheus text exposition for ``GET /metrics``.

Hand-rolled ``name value`` lines (no Prometheus client dependency), computed on
request from cheap ``radar_frame`` / ``archive_gap`` queries plus the
archiver-maintained Redis gauges (storage bytes, last poll).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from django.conf import settings
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


async def render() -> str:
    """Assemble the Prometheus exposition text."""
    day_ago = datetime.now(tz=UTC) - timedelta(hours=24)

    total = await RadarFrame.objects.acount()
    frames_24h = await RadarFrame.objects.filter(collected_at__gte=day_ago).acount()
    partial = await RadarFrame.objects.filter(status="partial").acount()
    failed = await RadarFrame.objects.filter(status="failed").acount()
    gaps_total = await ArchiveGap.objects.filter(service="radar").acount()
    gap_open = await ArchiveGap.objects.filter(service="radar", gap_end__isnull=True).aexists()
    bounds = await RadarFrame.objects.aaggregate(lo=Min("timestamp"), hi=Max("timestamp"))
    earliest = bounds["lo"] or 0
    latest = bounds["hi"] or 0

    tile_bytes = await cache.get_tile_dir_bytes()
    last_poll = await cache.get_last_poll()
    capacity = settings.STORAGE_CAPACITY_BYTES
    used_ratio = (tile_bytes / capacity) if capacity else 0.0

    # Per-provider archived counts + last-poll epochs. The unlabelled series
    # above stay for dashboard continuity; these add a {provider="…"} dimension.
    provider_names = enabled_providers()
    frames_by_provider: dict[str, int] = {}
    poll_by_provider: dict[str, int] = {}
    for name in provider_names:
        frames_by_provider[name] = await RadarFrame.objects.filter(provider=name).acount()
        poll_by_provider[name] = await cache.get_last_poll(name)

    lightning = await _lightning_series(day_ago)

    lines = [
        "# HELP radar_frames_total Total archived radar frames.",
        "# TYPE radar_frames_total gauge",
        f"radar_frames_total {total}",
        "# HELP radar_frames_24h Frames archived in the last 24h.",
        "# TYPE radar_frames_24h gauge",
        f"radar_frames_24h {frames_24h}",
        "# HELP radar_frames_partial_total Frames with status=partial.",
        "# TYPE radar_frames_partial_total gauge",
        f"radar_frames_partial_total {partial}",
        "# HELP radar_frames_failed_total Frames with status=failed.",
        "# TYPE radar_frames_failed_total gauge",
        f"radar_frames_failed_total {failed}",
        "# HELP radar_archive_gaps_total All-time gap rows.",
        "# TYPE radar_archive_gaps_total gauge",
        f"radar_archive_gaps_total {gaps_total}",
        "# HELP radar_archive_gap_open 1 if any gap is currently open.",
        "# TYPE radar_archive_gap_open gauge",
        f"radar_archive_gap_open {1 if gap_open else 0}",
        "# HELP radar_tile_dir_bytes Tile archive size on disk (bytes).",
        "# TYPE radar_tile_dir_bytes gauge",
        f"radar_tile_dir_bytes {tile_bytes}",
        "# HELP radar_storage_used_ratio tile_dir_bytes / STORAGE_CAPACITY_BYTES.",
        "# TYPE radar_storage_used_ratio gauge",
        f"radar_storage_used_ratio {used_ratio:.6f}",
        "# HELP radar_last_poll_timestamp Epoch of the last successful poll.",
        "# TYPE radar_last_poll_timestamp gauge",
        f"radar_last_poll_timestamp {last_poll}",
        *(
            f'radar_last_poll_timestamp{{provider="{name}"}} {poll_by_provider[name]}'
            for name in provider_names
        ),
        "# HELP radar_archive_earliest_timestamp Oldest archived frame epoch (0 if empty).",
        "# TYPE radar_archive_earliest_timestamp gauge",
        f"radar_archive_earliest_timestamp {earliest}",
        "# HELP radar_archive_latest_timestamp Newest archived frame epoch (0 if empty).",
        "# TYPE radar_archive_latest_timestamp gauge",
        f"radar_archive_latest_timestamp {latest}",
        "# HELP radar_frames_archived_total Archived frames per provider.",
        "# TYPE radar_frames_archived_total gauge",
        *(
            f'radar_frames_archived_total{{provider="{name}"}} {frames_by_provider[name]}'
            for name in provider_names
        ),
    ]
    lines += lightning
    lines += await _alert_series()
    return "\n".join(lines) + "\n"


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
    archived_total = await LightningStrike.objects.acount()
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
        "# HELP lightning_archived_total Total rows in lightning_strike.",
        "# TYPE lightning_archived_total gauge",
        f"lightning_archived_total {archived_total}",
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
        "# HELP lightning_partitions_total Attached monthly partitions (excl. default).",
        "# TYPE lightning_partitions_total gauge",
        f"lightning_partitions_total {partitions_total}",
    ]
