"""The push evaluator's tier logic + lifecycle.

Drives ``handle_strike`` with synthetic strikes and real ``PushSubscription`` rows,
with a fake sender (respx doesn't cover pywebpush). Throttle state lives in the real
Redis the dockerized suite provides (flushed between tests by the autouse fixture).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from radar import cache
from radar.alerts import REARM_S
from radar.alerts import evaluator
from radar.models import PushSubscription

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

# A sub at (45.0, 3.0): (45.18, 3.0) is ~20 km away (outer ring only, 10 < d < 30);
# (45.0, 3.0) is 0 km (both rings); (45.6, 3.0) is ~67 km (outside everything).
ANCHOR = (45.0, 3.0)
OUTER_PT = (45.18, 3.0)
FAR_PT = (45.6, 3.0)


class Recorder:
    def __init__(self, result: str = "ok") -> None:
        self.result = result
        self.calls: list = []

    async def send(self, sub, payload):
        self.calls.append((sub, payload))
        return self.result


def _strike(lat, lon, t) -> bytes:
    return json.dumps({"lat": lat, "lon": lon, "time": int(t), "intensity": 1}).encode()


async def _sub(endpoint="https://fcm.googleapis.com/fcm/send/x", locale="en"):
    return await PushSubscription.objects.acreate(
        endpoint=endpoint,
        p256dh="k",
        auth="a",
        lat=ANCHOR[0],
        lon=ANCHOR[1],
        locale=locale,
    )


async def test_first_strike_in_outer_ring_notifies_once(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    subs = [await _sub()]
    await evaluator.handle_strike(_strike(*OUTER_PT, time.time()), subs)
    assert len(rec.calls) == 1
    assert rec.calls[0][1]["title"] == "⚡ Storm approaching"  # outer tier
    assert rec.calls[0][1]["tag"] == "rainradar-alert"


async def test_strike_within_rearm_is_throttled(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    sub = await _sub()
    now = int(time.time())
    await cache.set_alert_throttle(sub.id, "outer", now, 2 * REARM_S)  # a recent outer strike
    await evaluator.handle_strike(_strike(*OUTER_PT, now), [sub])
    assert rec.calls == []


async def test_tier_rearms_after_quiet(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    sub = await _sub()
    now = int(time.time())
    await cache.set_alert_throttle(sub.id, "outer", now - (REARM_S + 100), 2 * REARM_S)
    await evaluator.handle_strike(_strike(*OUTER_PT, now), [sub])
    assert len(rec.calls) == 1


async def test_inner_strike_refreshes_both_tiers(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    sub = await _sub()
    now = int(time.time())
    # A strike overhead (0 km) fires both tiers and refreshes both quiet timers…
    await evaluator.handle_strike(_strike(*ANCHOR, now), [sub])
    assert len(rec.calls) == 2  # inner + outer (same tag → the client shows the last)
    # …so an outer-ring strike moments later is silent (outer was refreshed).
    await evaluator.handle_strike(_strike(*OUTER_PT, now + 10), [sub])
    assert len(rec.calls) == 2


async def test_stale_strike_never_notifies(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    subs = [await _sub()]
    await evaluator.handle_strike(_strike(*OUTER_PT, time.time() - 700), subs)  # > FRESH_S
    assert rec.calls == []


async def test_out_of_ring_strike_never_notifies(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    subs = [await _sub()]
    await evaluator.handle_strike(_strike(*FAR_PT, time.time()), subs)
    assert rec.calls == []


async def test_malformed_payload_skipped(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    subs = [await _sub()]
    await evaluator.handle_strike(b"not json", subs)  # must not raise
    assert rec.calls == []


async def test_gone_result_prunes_row(monkeypatch):
    rec = Recorder("gone")
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    sub = await _sub()
    subs = [sub]
    await evaluator.handle_strike(_strike(*OUTER_PT, time.time()), subs)
    assert await PushSubscription.objects.acount() == 0  # row deleted
    assert sub not in subs  # dropped from the working set
    assert await cache.get_push_pruned() == 1


async def test_failed_result_keeps_row(monkeypatch):
    rec = Recorder("failed")
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    sub = await _sub()
    await evaluator.handle_strike(_strike(*OUTER_PT, time.time()), [sub])
    assert await PushSubscription.objects.acount() == 1  # kept
    assert await cache.get_push_failed() == 1


async def test_run_evaluator_exits_on_stop():
    stop = asyncio.Event()
    stop.set()  # already set → the loop body never runs; it subscribes then returns
    await asyncio.wait_for(evaluator.run_evaluator(stop=stop), timeout=5)


async def test_run_evaluator_processes_published_strike(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("radar.alerts.webpush.send", rec.send)
    await _sub()
    stop = asyncio.Event()
    task = asyncio.create_task(evaluator.run_evaluator(stop=stop))
    try:
        await asyncio.sleep(0.5)  # let it subscribe
        await cache.publish_strike(_strike(*OUTER_PT, time.time()))
        for _ in range(50):
            if rec.calls:
                break
            await asyncio.sleep(0.1)
        assert len(rec.calls) == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
