"""The storm-alert push evaluator — archiver-only, single-tasked.

Subscribes to the existing ``lightning:strikes`` Redis pub/sub channel (exactly like
the SSE view) and, for each ``PushSubscription``, applies the same two-tier / re-arm
logic as ``frontend/js/alerts.js`` and sends a localized Web Push. It never touches
the ingest hot path — a crash here is caught by ``run_archiver``'s ``_supervise`` and
restarted, affecting neither radar nor lightning. Runs in the single-replica archiver
only, so the per-subscription throttle check-then-set in Redis is race-free.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from django.conf import settings

from radar import cache
from radar.alerts import FRESH_S
from radar.alerts import REARM_S
from radar.alerts import TIERS
from radar.alerts import bearing8
from radar.alerts import copy
from radar.alerts import haversine_km
from radar.alerts import webpush
from radar.logging_json import emit
from radar.models import PushSubscription

logger = logging.getLogger("radar.alerts")

_THROTTLE_TTL = 2 * REARM_S  # a tier's Redis hash self-cleans after two quiet windows


async def _load_subs() -> list[PushSubscription]:
    return [s async for s in PushSubscription.objects.all()]


async def _prune(sub: PushSubscription, subs: list[PushSubscription]) -> None:
    """A dead endpoint (404/410): delete the row + drop it from the working set."""
    with contextlib.suppress(Exception):
        await PushSubscription.objects.filter(pk=sub.pk).adelete()
    with contextlib.suppress(ValueError):
        subs.remove(sub)
    await cache.incr_push_pruned()
    emit(logger, logging.INFO, "push_pruned", service="alerts", subscription_id=sub.pk)


async def _dispatch(
    pending: list[tuple[PushSubscription, dict]],
    subs: list[PushSubscription],
) -> None:
    """Send a strike's queued notifications concurrently (bounded in webpush.send)."""
    results = await asyncio.gather(*(webpush.send(sub, payload) for sub, payload in pending))
    sent = 0
    failed = 0
    for (sub, _payload), result in zip(pending, results, strict=True):
        if result == "gone":
            await _prune(sub, subs)
        elif result == "failed":
            failed += 1
        else:
            sent += 1
    if sent:
        await cache.incr_push_sent(sent)
        emit(logger, logging.INFO, "push_sent", service="alerts", count=sent)
    if failed:
        await cache.incr_push_failed(failed)
        emit(logger, logging.WARNING, "push_failed", service="alerts", count=failed)


async def handle_strike(raw: bytes, subs: list[PushSubscription]) -> None:
    """Evaluate one pub/sub strike against every subscription."""
    try:
        s = json.loads(raw)
        lat = float(s["lat"])
        lon = float(s["lon"])
        t = int(s["time"])
    except ValueError, TypeError, KeyError:
        return  # malformed event → skip; the next one is independent
    if time.time() - t > FRESH_S:
        return  # stale (e.g. a pub/sub backlog) — never notify late

    pending: list[tuple[PushSubscription, dict]] = []
    for sub in subs:
        dist = haversine_km(sub.lat, sub.lon, lat, lon)
        if dist > TIERS[-1][1]:  # outside the widest ring — cheap reject, no Redis touch
            continue
        throttle = await cache.get_alert_throttle(sub.id)
        dir8 = None
        for tier_id, radius in TIERS:  # inner first, then outer (mirrors the frontend)
            if dist > radius:
                continue
            # Armed-check BEFORE refreshing the quiet timer (order is load-bearing).
            if t - throttle[tier_id] > REARM_S:
                if dir8 is None:
                    dir8 = bearing8(sub.lat, sub.lon, lat, lon)
                text = copy.render(sub.locale, tier_id, dist, dir8)
                pending.append(
                    (sub, {**text, "tag": "rainradar-alert", "ts": t}),
                )
            await cache.set_alert_throttle(sub.id, tier_id, t, _THROTTLE_TTL)

    if pending:
        await _dispatch(pending, subs)


async def run_evaluator(*, stop: asyncio.Event | None = None) -> None:
    """Consume ``lightning:strikes`` and push proximity alerts. Runs forever.

    ``stop`` lets tests end the loop deterministically; in production the task is
    supervised (restarted on unexpected crash) by ``run_archiver``.
    """
    subs = await _load_subs()
    last_refresh = time.monotonic()
    pubsub = cache.pubsub()
    await pubsub.subscribe(cache.LIGHTNING_CHANNEL)
    emit(logger, logging.INFO, "alerts_evaluator_started", service="alerts", subs=len(subs))
    try:
        while stop is None or not stop.is_set():
            message = await pubsub.get_message(timeout=1.0)
            # Periodically refresh the working set so new subscribers are picked up.
            if time.monotonic() - last_refresh >= settings.PUSH_SUBS_REFRESH_SECONDS:
                subs = await _load_subs()
                last_refresh = time.monotonic()
            if message is None or message.get("type") != "message":
                continue
            await handle_strike(message["data"], subs)
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(cache.LIGHTNING_CHANNEL)
            await pubsub.aclose()
