"""Lightning ingest: bbox filter, backpressure, batch writer, reconnect.

No real WebSocket — a fake :class:`~radar.lightning.base.LightningSource` yields
scripted strikes (and one that raises :class:`LightningSourceError` to exercise
reconnect). Redis is real (the dockerized suite; conftest flushes it per test);
the batch insert hits real Postgres via the DEFAULT/monthly partitions.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest
from django.conf import settings

from radar import cache
from radar.lightning.base import LightningSourceError
from radar.lightning.base import Strike
from radar.lightning.ingest import enqueue_drop_oldest
from radar.lightning.ingest import in_bbox
from radar.lightning.ingest import persist_and_fanout
from radar.lightning.ingest import run_ingest
from radar.lightning.ingest import run_writer
from radar.lightning.ingest import strike_json
from radar.models import LightningStrike


def _strike(lat=45.0, lon=3.0, t=1718960400.5, intensity=1) -> Strike:
    return Strike(struck_at=t, lat=lat, lon=lon, intensity=intensity)


class FakeSource:
    """Scripted source. Each item in ``rounds`` is one connection's worth of work:

    a list of strikes to yield (then the connection "drops" with
    LightningSourceError), or an Exception to raise immediately.
    """

    name = "fake"

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.connect_urls: list[str] = []

    def attribution(self) -> str:
        return "fake"

    @contextlib.asynccontextmanager
    async def connect(self, url):
        self.connect_urls.append(url)
        round_ = self._rounds.pop(0) if self._rounds else LightningSourceError("exhausted")
        yield self._iter(round_)

    async def _iter(self, round_):
        if isinstance(round_, Exception):
            raise round_
        for strike in round_:
            yield strike
        # a graceful end is still a disconnect per the source contract
        raise LightningSourceError("stream ended")


async def _wait_until(predicate, limit=2.0):
    deadline = asyncio.get_running_loop().time() + limit
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not met in time")
        await asyncio.sleep(0.01)


# -- pure helpers -------------------------------------------------------------


def test_in_bbox():
    bbox = [42.0, 51.5, -6.0, 9.0]
    assert in_bbox(_strike(lat=45.0, lon=3.0), bbox)
    assert not in_bbox(_strike(lat=10.0, lon=3.0), bbox)  # south of France
    assert not in_bbox(_strike(lat=45.0, lon=40.0), bbox)  # east of bbox


def test_strike_json_shape():
    import json  # noqa: PLC0415

    obj = json.loads(strike_json(_strike(lat=45.1, lon=3.2, t=1718960400.512, intensity=7)))
    assert obj == {"lat": 45.1, "lon": 3.2, "time": 1718960400.512, "intensity": 7}


# -- backpressure -------------------------------------------------------------


async def test_enqueue_drop_oldest_drops_oldest_and_counts():
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    oldest = _strike(t=1.0)
    await enqueue_drop_oldest(queue, oldest)
    await enqueue_drop_oldest(queue, _strike(t=2.0))
    assert queue.full()
    newest = _strike(t=3.0)
    dropped = await enqueue_drop_oldest(queue, newest)  # never awaits put

    assert dropped is True
    assert queue.qsize() == 2  # still bounded
    assert await cache.get_queue_dropped() == 1
    # the oldest is gone; the newest is present
    remaining = [queue.get_nowait(), queue.get_nowait()]
    assert oldest not in remaining
    assert newest in remaining


# -- batch writer -------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_persist_and_fanout_writes_publishes_counts():
    # The table is managed=False (not truncated between tests / --reuse-db runs),
    # so clear this test's lat band first to keep the count deterministic.
    await LightningStrike.objects.filter(lat__gte=12.3, lat__lte=12.4).adelete()
    strikes = [_strike(lat=12.34, t=1718960400 + i, intensity=i) for i in range(3)]
    pubsub = cache.pubsub()
    await pubsub.subscribe(cache.LIGHTNING_CHANNEL)
    await pubsub.get_message(timeout=1)  # consume the subscribe confirmation

    written = await persist_and_fanout(strikes)

    assert written == 3
    landed = await LightningStrike.objects.filter(lat__gte=12.3, lat__lte=12.4).acount()
    assert landed == 3
    assert await cache.get_strikes_total() == 3
    assert await cache.get_last_strike_ts() == 1718960402
    assert len(await cache.recent_strikes()) == 3
    # each strike was published live, oldest->newest order preserved by the writer
    msg = await pubsub.get_message(timeout=1)
    assert msg is not None
    assert msg["type"] == "message"
    await pubsub.unsubscribe(cache.LIGHTNING_CHANNEL)
    await pubsub.aclose()


@pytest.mark.django_db(transaction=True)
async def test_persist_and_fanout_db_error_drops_only_this_batch(monkeypatch):
    monkeypatch.setattr(
        LightningStrike.objects,
        "abulk_create",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    written = await persist_and_fanout([_strike()])
    assert written == 0  # swallowed, not raised
    assert await cache.get_strikes_total() == 0  # counters untouched on failure
    assert await cache.recent_strikes() == []


@pytest.mark.django_db(transaction=True)
async def test_run_writer_flushes_on_size(monkeypatch):
    monkeypatch.setattr(settings, "LIGHTNING_BATCH_SIZE", 3)
    monkeypatch.setattr(settings, "LIGHTNING_BATCH_INTERVAL", 0.05)
    abulk = AsyncMock()
    monkeypatch.setattr(LightningStrike.objects, "abulk_create", abulk)

    queue: asyncio.Queue = asyncio.Queue()
    for i in range(3):
        queue.put_nowait(_strike(t=1718960400 + i))
    stop = asyncio.Event()
    task = asyncio.create_task(run_writer(queue, stop=stop))
    await _wait_until(lambda: abulk.await_count >= 1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    abulk.assert_awaited_once()
    assert len(abulk.await_args.args[0]) == 3  # the whole batch in one bulk insert


@pytest.mark.django_db(transaction=True)
async def test_run_writer_flushes_on_interval(monkeypatch):
    monkeypatch.setattr(settings, "LIGHTNING_BATCH_SIZE", 100)  # never reached
    monkeypatch.setattr(settings, "LIGHTNING_BATCH_INTERVAL", 0.05)
    abulk = AsyncMock()
    monkeypatch.setattr(LightningStrike.objects, "abulk_create", abulk)

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(_strike())
    queue.put_nowait(_strike())
    stop = asyncio.Event()
    task = asyncio.create_task(run_writer(queue, stop=stop))
    await _wait_until(lambda: abulk.await_count >= 1)  # flushed by the interval tick
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert len(abulk.await_args.args[0]) == 2


@pytest.mark.django_db(transaction=True)
async def test_run_writer_continues_after_db_error(monkeypatch):
    monkeypatch.setattr(settings, "LIGHTNING_BATCH_SIZE", 2)
    monkeypatch.setattr(settings, "LIGHTNING_BATCH_INTERVAL", 0.05)
    abulk = AsyncMock(side_effect=[RuntimeError("boom"), None])
    monkeypatch.setattr(LightningStrike.objects, "abulk_create", abulk)

    queue: asyncio.Queue = asyncio.Queue()
    for i in range(4):  # two batches of two
        queue.put_nowait(_strike(t=1718960400 + i))
    stop = asyncio.Event()
    task = asyncio.create_task(run_writer(queue, stop=stop))
    await _wait_until(lambda: abulk.await_count >= 2)  # second batch written after the failure
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert abulk.await_count == 2
    assert await cache.get_strikes_total() == 2  # only the surviving batch counted


# -- WS read loop / supervisor ------------------------------------------------


async def test_run_ingest_bbox_filters_before_queue():
    source = FakeSource([[_strike(lat=45.0, lon=3.0), _strike(lat=10.0, lon=3.0)]])
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    await run_ingest(source, queue, sleep=AsyncMock(), max_cycles=1)

    assert queue.qsize() == 1  # the out-of-bbox strike was dropped pre-queue
    assert in_bbox(queue.get_nowait(), settings.LIGHTNING_BBOX)
    assert await cache.get_ws_connected() == 0  # cleared on exit


async def test_run_ingest_drops_oldest_under_overflow():
    source = FakeSource([[_strike(t=float(i)) for i in range(5)]])
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    await run_ingest(source, queue, sleep=AsyncMock(), max_cycles=1)

    assert queue.full()
    assert await cache.get_queue_dropped() == 3  # 5 in, capacity 2 -> 3 dropped


async def test_run_ingest_reconnects_with_backoff_on_error():
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    source = FakeSource([LightningSourceError("down"), LightningSourceError("down again")])
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    await run_ingest(source, queue, sleep=fake_sleep, max_cycles=2)

    assert await cache.get_reconnects() == 2  # both failed connects counted
    assert len(sleeps) == 1  # backoff between cycle 1 and 2 (none after the last)
    assert sleeps[0] >= settings.LIGHTNING_BACKOFF_MIN
    assert len(source.connect_urls) == 2  # rotated through the failover list


async def test_run_ingest_recovers_after_a_drop():
    # First connection drops; second yields a strike that lands on the queue.
    source = FakeSource([LightningSourceError("down"), [_strike(lat=45.0, lon=3.0)]])
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    await run_ingest(source, queue, sleep=AsyncMock(), max_cycles=2)

    assert queue.qsize() == 1
    assert await cache.get_reconnects() >= 1
