"""Redis helpers and key schema.

Redis no longer stores tile bytes (disk is the tile store). It now holds
only small, fully reconstructible state — so persistence (RDB/AOF) stays off:

    radar:{provider}:frames_json   -> raw weather-maps.json body   (TTL 60s)
    radar:{provider}:frames_live   -> assembled /api/radar/frames live response (short TTL)
    radar:{provider}:archived      -> SET of archived epoch seconds (no TTL)
    radar:range_json               -> /api/radar/range bounds JSON  (TTL 60s)
    radar:tile_dir_bytes           -> archiver-maintained size gauge (no TTL)

Keys stay provider-namespaced where provider-specific so switching providers
never serves stale state from another source.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from django.conf import settings

if TYPE_CHECKING:
    from collections.abc import Iterable

_client: aioredis.Redis | None = None
_client_loop: asyncio.AbstractEventLoop | None = None

STATS_KEY = "stats:json"
METRICS_KEY = "metrics:text"
STORAGE_GAUGE_KEY = "radar:tile_dir_bytes"
# Global last-poll gauge, kept updated by every provider's poll (last-writer-wins)
# for dashboard continuity; the per-provider key (last_poll_key) is authoritative.
LAST_POLL_KEY = "radar:last_poll_ts"

# -- lightning ----------------------------------------------------------------
# All ephemeral/reconstructible: pub/sub is transient, the recent buffer is a
# capped list, the gauges/counters are rebuilt by the ingester at startup.
LIGHTNING_CHANNEL = "lightning:strikes"
LIGHTNING_RECENT_KEY = "lightning:recent"
LIGHTNING_WS_CONNECTED_KEY = "lightning:ws_connected"
LIGHTNING_WS_SINCE_KEY = "lightning:ws_connected_since"
LIGHTNING_RECONNECTS_KEY = "lightning:reconnects"
LIGHTNING_QUEUE_DROPPED_KEY = "lightning:queue_dropped"
LIGHTNING_STRIKES_TOTAL_KEY = "lightning:strikes_total"
LIGHTNING_LAST_STRIKE_KEY = "lightning:last_strike_ts"

# -- storm-alert push ---------------------------------------------------------
# Per-subscription per-tier throttle: a small hash {outer, inner} of the last
# in-ring strike epoch, TTL'd so it self-cleans after a subscription goes quiet.
# Counters are plain integers, rebuilt implicitly (best-effort operational stats).
ALERT_THROTTLE_PREFIX = "alerts:throttle:"
ALERT_PUSH_SENT_KEY = "alerts:push_sent"
ALERT_PUSH_FAILED_KEY = "alerts:push_failed"
ALERT_PUSH_PRUNED_KEY = "alerts:push_pruned"


def get_client() -> aioredis.Redis:
    """Return an async Redis client bound to the running event loop.

    An aioredis client/connection is tied to the loop it was created on. Under
    uvicorn there is a single persistent loop, so the client is effectively a
    singleton. Django's WSGI ``runserver`` creates a fresh loop per request, so
    we rebuild the client whenever the running loop changes (otherwise reusing a
    client from a closed loop raises "Event loop is closed").
    """
    global _client, _client_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client = aioredis.from_url(settings.REDIS_URL, decode_responses=False)
        _client_loop = loop
    return _client


# -- frames JSON cache --------------------------------------------------------


def frames_key(provider: str) -> str:
    return f"radar:{provider}:frames_json"


def frames_live_key(provider: str) -> str:
    """Key for the assembled /api/radar/frames live-window response.

    Provider-namespaced like frames_json so a provider switch never serves
    another source's cached window.
    """
    return f"radar:{provider}:frames_live"


async def get_bytes(key: str) -> bytes | None:
    return await get_client().get(key)


async def set_bytes(key: str, value: bytes, ttl: int) -> None:
    await get_client().set(key, value, ex=ttl)


# -- archived set -------------------------------------------------------------


def archived_key(provider: str) -> str:
    return f"radar:{provider}:archived"


async def add_archived(provider: str, ts: int) -> None:
    await get_client().sadd(archived_key(provider), ts)


async def is_archived(provider: str, ts: int) -> bool:
    return bool(await get_client().sismember(archived_key(provider), ts))


async def get_archived(provider: str) -> set[int]:
    members = await get_client().smembers(archived_key(provider))
    return {int(m) for m in members}


async def archived_count(provider: str) -> int:
    return int(await get_client().scard(archived_key(provider)))


async def replace_archived(provider: str, timestamps: Iterable[int]) -> None:
    """Rebuild the archived set from a known-good source (cold-start rescan)."""
    ts_list = list(timestamps)
    client = get_client()
    key = archived_key(provider)
    async with client.pipeline(transaction=True) as pipe:
        pipe.delete(key)
        if ts_list:
            pipe.sadd(key, *ts_list)
        await pipe.execute()


async def remove_archived(provider: str, timestamps: Iterable[int]) -> None:
    """Drop timestamps from the archived set (retention prune)."""
    ts_list = list(timestamps)
    if not ts_list:
        return
    await get_client().srem(archived_key(provider), *ts_list)


# -- /api/radar/range cache (per-provider) ------------------------------------


def range_key(provider: str) -> str:
    return f"radar:{provider}:range_json"


async def get_range_cache(provider: str) -> bytes | None:
    return await get_client().get(range_key(provider))


async def set_range_cache(provider: str, value: bytes, ttl: int) -> None:
    await get_client().set(range_key(provider), value, ex=ttl)


# -- /api/stats cache ---------------------------------------------------------


async def get_stats_json() -> bytes | None:
    return await get_client().get(STATS_KEY)


async def set_stats_json(value: bytes, ttl: int) -> None:
    await get_client().set(STATS_KEY, value, ex=ttl)


# -- /metrics exposition cache ------------------------------------------------
# Shields Postgres from the per-scrape aggregate queries (notably the unbounded
# COUNT(*) over the partitioned lightning_strike table) — /metrics is public, so
# scrape cadence is not under our control.


async def get_metrics_text() -> bytes | None:
    return await get_client().get(METRICS_KEY)


async def set_metrics_text(value: bytes, ttl: int) -> None:
    await get_client().set(METRICS_KEY, value, ex=ttl)


# -- storage-size gauge -------------------------------------------------------


async def set_tile_dir_bytes(n: int) -> None:
    await get_client().set(STORAGE_GAUGE_KEY, n)


async def incr_tile_dir_bytes(n: int) -> int:
    """Add ``n`` bytes to the storage gauge (per-poll increment).

    The absolute value is reconciled by a full rescan at archiver startup and in
    the daily janitor; between those, polls only add the bytes they wrote.
    """
    if n == 0:
        return await get_tile_dir_bytes()
    return int(await get_client().incrby(STORAGE_GAUGE_KEY, n))


async def get_tile_dir_bytes() -> int:
    raw = await get_client().get(STORAGE_GAUGE_KEY)
    return int(raw) if raw is not None else 0


def last_poll_key(provider: str) -> str:
    return f"radar:{provider}:last_poll_ts"


async def set_last_poll(provider: str, ts: int) -> None:
    """Record a provider's last successful poll epoch (per-provider + global).

    The global ``LAST_POLL_KEY`` is kept updated (last-writer-wins across providers)
    so the existing unlabelled dashboard gauge keeps working.
    """
    client = get_client()
    async with client.pipeline(transaction=False) as pipe:
        pipe.set(last_poll_key(provider), ts)
        pipe.set(LAST_POLL_KEY, ts)
        await pipe.execute()


async def get_last_poll(provider: str | None = None) -> int:
    """Last-poll epoch: per-provider when ``provider`` is given, else the global gauge."""
    key = last_poll_key(provider) if provider else LAST_POLL_KEY
    raw = await get_client().get(key)
    return int(raw) if raw is not None else 0


# -- lightning pub/sub + recent buffer ----------------------------------------


def pubsub():
    """A pubsub object on the loop-bound client (mirror get_client's loop rule)."""
    return get_client().pubsub()


async def publish_strike(strike_json: bytes) -> None:
    """Publish one strike to the live SSE channel (fire-and-forget)."""
    await get_client().publish(LIGHTNING_CHANNEL, strike_json)


async def push_recent(strike_json: bytes) -> None:
    """Append to the capped recent buffer (LPUSH newest-first + LTRIM)."""
    client = get_client()
    async with client.pipeline(transaction=False) as pipe:
        pipe.lpush(LIGHTNING_RECENT_KEY, strike_json)
        pipe.ltrim(LIGHTNING_RECENT_KEY, 0, settings.LIGHTNING_RECENT_MAX - 1)
        await pipe.execute()


async def fanout_strikes(payloads: list[bytes], newest_ts: int) -> None:
    """Fan out one persisted batch in a single Redis round-trip.

    Batches the per-strike PUBLISHes (order preserved, oldest->newest), one
    LPUSH of the whole batch (multi-value LPUSH inserts left-to-right, so the
    newest strike ends up at the head — same layout as per-strike pushes), one
    LTRIM, and the counters. Replaces 2N+2 sequential round-trips per batch,
    which mattered during storms when the writer must keep the queue drained.
    """
    if not payloads:
        return
    client = get_client()
    async with client.pipeline(transaction=False) as pipe:
        for payload in payloads:
            pipe.publish(LIGHTNING_CHANNEL, payload)
        pipe.lpush(LIGHTNING_RECENT_KEY, *payloads)
        pipe.ltrim(LIGHTNING_RECENT_KEY, 0, settings.LIGHTNING_RECENT_MAX - 1)
        pipe.incrby(LIGHTNING_STRIKES_TOTAL_KEY, len(payloads))
        pipe.set(LIGHTNING_LAST_STRIKE_KEY, newest_ts)
        await pipe.execute()


async def recent_strikes() -> list[bytes]:
    """The recent buffer oldest->newest (the list is stored newest-first)."""
    items = await get_client().lrange(LIGHTNING_RECENT_KEY, 0, -1)
    items.reverse()
    return items


# -- lightning gauges / counters ----------------------------------------------


async def set_ws_connected(connected: bool, since: int | None = None) -> None:  # noqa: FBT001
    client = get_client()
    await client.set(LIGHTNING_WS_CONNECTED_KEY, 1 if connected else 0)
    if connected and since is not None:
        await client.set(LIGHTNING_WS_SINCE_KEY, since)


async def get_ws_connected() -> int:
    raw = await get_client().get(LIGHTNING_WS_CONNECTED_KEY)
    return int(raw) if raw is not None else 0


async def get_ws_connected_since() -> int:
    raw = await get_client().get(LIGHTNING_WS_SINCE_KEY)
    return int(raw) if raw is not None else 0


async def incr_reconnects() -> int:
    return int(await get_client().incr(LIGHTNING_RECONNECTS_KEY))


async def get_reconnects() -> int:
    raw = await get_client().get(LIGHTNING_RECONNECTS_KEY)
    return int(raw) if raw is not None else 0


async def incr_queue_dropped(n: int) -> int:
    return int(await get_client().incrby(LIGHTNING_QUEUE_DROPPED_KEY, n))


async def get_queue_dropped() -> int:
    raw = await get_client().get(LIGHTNING_QUEUE_DROPPED_KEY)
    return int(raw) if raw is not None else 0


async def incr_strikes_total(n: int) -> int:
    return int(await get_client().incrby(LIGHTNING_STRIKES_TOTAL_KEY, n))


async def get_strikes_total() -> int:
    raw = await get_client().get(LIGHTNING_STRIKES_TOTAL_KEY)
    return int(raw) if raw is not None else 0


async def set_last_strike_ts(ts: int) -> None:
    await get_client().set(LIGHTNING_LAST_STRIKE_KEY, ts)


async def get_last_strike_ts() -> int:
    raw = await get_client().get(LIGHTNING_LAST_STRIKE_KEY)
    return int(raw) if raw is not None else 0


# -- storm-alert push throttle + counters -------------------------------------


def _throttle_key(sub_id: int) -> str:
    return f"{ALERT_THROTTLE_PREFIX}{sub_id}"


async def get_alert_throttle(sub_id: int) -> dict[str, int]:
    """The {outer, inner} last-in-ring-strike epochs for one subscription (0 if unset)."""
    raw = await get_client().hgetall(_throttle_key(sub_id))
    decoded = {(k.decode() if isinstance(k, bytes) else k): v for k, v in raw.items()}
    return {tier: int(decoded.get(tier, 0) or 0) for tier in ("outer", "inner")}


async def set_alert_throttle(sub_id: int, tier: str, ts: int, ttl: int) -> None:
    """Record a tier's latest in-ring strike epoch and (re)arm the hash's TTL."""
    key = _throttle_key(sub_id)
    client = get_client()
    async with client.pipeline(transaction=False) as pipe:
        pipe.hset(key, tier, ts)
        pipe.expire(key, ttl)
        await pipe.execute()


async def incr_push_sent(n: int = 1) -> int:
    return int(await get_client().incrby(ALERT_PUSH_SENT_KEY, n))


async def get_push_sent() -> int:
    raw = await get_client().get(ALERT_PUSH_SENT_KEY)
    return int(raw) if raw is not None else 0


async def incr_push_failed(n: int = 1) -> int:
    return int(await get_client().incrby(ALERT_PUSH_FAILED_KEY, n))


async def get_push_failed() -> int:
    raw = await get_client().get(ALERT_PUSH_FAILED_KEY)
    return int(raw) if raw is not None else 0


async def incr_push_pruned(n: int = 1) -> int:
    return int(await get_client().incrby(ALERT_PUSH_PRUNED_KEY, n))


async def get_push_pruned() -> int:
    raw = await get_client().get(ALERT_PUSH_PRUNED_KEY)
    return int(raw) if raw is not None else 0


# -- readiness ----------------------------------------------------------------


async def ping() -> bool:
    """True if Redis answers PING; False on any connection error (readiness)."""
    try:
        return bool(await get_client().ping())
    except Exception:  # noqa: BLE001 — readiness probe must never raise
        return False
