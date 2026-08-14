"""Lightning ingest — the WS read loop + the batch writer.

Two cooperating asyncio tasks on the archiver loop, started only when
``LIGHTNING_ENABLED``. The decoupling is the core resilience
requirement: **the WS read loop must never block on persistence.**

    Blitzortung WS ─► asyncio.Queue(maxsize=N) ─► batch writer task
      (read+decode+        (bounded buffer;          (bulk INSERT to Postgres
       bbox filter)         drop-oldest on overflow)   + Redis publish + recent)

The logic lives in importable async functions so tests drive them directly with
a fake :class:`~radar.lightning.base.LightningSource` and an in-memory queue
 — exactly as ``radar.archiver`` keeps poll logic out of the scheduler
bootstrap. Nothing here imports radar code beyond the shared seams.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import random
import time
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from django.conf import settings

from radar import cache
from radar.lightning.base import LightningSourceError
from radar.logging_json import emit
from radar.models import LightningStrike

if TYPE_CHECKING:
    from radar.lightning.base import LightningSource
    from radar.lightning.base import Strike

logger = logging.getLogger("radar.lightning")

# Emit a (rate-limited) queue_overflow WARNING at most once per this many drops,
# so a storm doesn't flood the logs while still surfacing sustained overflow.
_OVERFLOW_LOG_EVERY = 1000


# -- pure helpers (unit-tested directly) --------------------------------------


def in_bbox(strike: Strike, bbox) -> bool:
    """``S <= lat <= N and W <= lon <= E`` for ``bbox = [S, N, W, E]``."""
    south, north, west, east = bbox
    return south <= strike.lat <= north and west <= strike.lon <= east


def strike_json(strike: Strike) -> bytes:
    """The on-the-wire JSON for SSE ``data:`` + the recent buffer."""
    return json.dumps(
        {
            "lat": strike.lat,
            "lon": strike.lon,
            "time": strike.struck_at,  # epoch seconds, sub-second float, UTC
            "intensity": strike.intensity,
        },
    ).encode()


def _utc_dt(struck_at: float) -> datetime:
    """Epoch seconds -> tz-aware UTC datetime for the partitioned table."""
    return datetime.fromtimestamp(struck_at, tz=UTC)


async def enqueue_drop_oldest(queue: asyncio.Queue, strike: Strike) -> bool:
    """Put ``strike`` on ``queue``; if full, discard the OLDEST first.

    Never ``await queue.put`` — a slow writer must not stall the WS read. Returns
    True iff a strike was dropped (the caller counts/rate-limits the log).
    """
    dropped = False
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()  # discard oldest
            dropped = True
            await cache.incr_queue_dropped(1)
    queue.put_nowait(strike)
    return dropped


# -- batch writer -------------------------------------------------------------


async def persist_and_fanout(strikes: list[Strike]) -> int:
    """Bulk-insert one batch, then publish + buffer + count it.

    A DB write failure drops **only this batch** (logged ``batch_failed``) and
    returns 0 — the caller keeps running so a Postgres blip never kills ingest.
    """
    if not strikes:
        return 0
    rows = [
        LightningStrike(
            struck_at=_utc_dt(s.struck_at),
            lat=s.lat,
            lon=s.lon,
            intensity=s.intensity,
        )
        for s in strikes
    ]
    try:
        await LightningStrike.objects.abulk_create(rows)
    except Exception:
        emit(logger, logging.ERROR, "batch_failed", service="lightning", n=len(strikes))
        logger.exception("lightning batch write of %d strikes failed", len(strikes))
        return 0

    # Fan-out for live SSE + late-joiner replay: still one PUBLISH per strike
    # (subscribers see individual events), but batched with the recent-buffer
    # push and the counters into a single pipelined round-trip.
    newest = max(s.struck_at for s in strikes)
    await cache.fanout_strikes([strike_json(s) for s in strikes], int(newest))
    emit(logger, logging.INFO, "batch_written", service="lightning", n=len(strikes))
    return len(strikes)


async def run_writer(queue: asyncio.Queue, *, stop: asyncio.Event | None = None) -> None:
    """Drain ``queue`` into batches, flushing on size or interval."""
    batch_size = settings.LIGHTNING_BATCH_SIZE
    interval = settings.LIGHTNING_BATCH_INTERVAL
    buf: list[Strike] = []
    while stop is None or not stop.is_set():
        try:
            strike = await asyncio.wait_for(queue.get(), timeout=interval)
            buf.append(strike)
            if len(buf) < batch_size:
                continue  # keep filling until size or the next interval tick
        except TimeoutError:
            pass  # interval elapsed — flush whatever we have
        except asyncio.CancelledError:
            raise
        if buf:
            await persist_and_fanout(buf)
            buf = []
    if buf:  # final flush on clean stop
        await persist_and_fanout(buf)


# -- WS read loop / supervisor ------------------------------------------------


async def run_ingest(  # noqa: PLR0913 — injectable bbox/urls/sleep/max_cycles keep it testable
    source: LightningSource,
    queue: asyncio.Queue,
    *,
    bbox=None,
    urls=None,
    sleep=asyncio.sleep,
    max_cycles: int | None = None,
) -> None:
    """Hold a persistent WS connection, bbox-filter, enqueue; reconnect forever.

    ``max_cycles`` bounds the reconnect loop for tests (None = forever in prod).
    ``sleep`` is injectable so tests can assert backoff without real waits.
    """
    bbox = bbox if bbox is not None else settings.LIGHTNING_BBOX
    urls = urls if urls is not None else settings.BLITZORTUNG_WS_URLS
    url_cycle = itertools.cycle(urls)
    backoff = settings.LIGHTNING_BACKOFF_MIN
    dropped_since_log = 0
    cycles = 0

    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        url = next(url_cycle)
        try:
            async with source.connect(url) as strikes:
                await cache.set_ws_connected(True, since=int(time.time()))
                backoff = settings.LIGHTNING_BACKOFF_MIN  # reset on a good connect
                emit(logger, logging.INFO, "ws_connected", service="lightning", url=url)
                async for strike in strikes:
                    if not in_bbox(strike, bbox):
                        continue
                    if await enqueue_drop_oldest(queue, strike):
                        dropped_since_log += 1
                        if dropped_since_log >= _OVERFLOW_LOG_EVERY:
                            emit(
                                logger,
                                logging.WARNING,
                                "queue_overflow",
                                service="lightning",
                                dropped=dropped_since_log,
                            )
                            dropped_since_log = 0
        except asyncio.CancelledError:
            raise  # clean shutdown
        except LightningSourceError as exc:
            await cache.set_ws_connected(False)
            await cache.incr_reconnects()
            emit(
                logger,
                logging.WARNING,
                "ws_disconnected",
                service="lightning",
                url=url,
                error=str(exc),
            )
        except Exception:
            await cache.set_ws_connected(False)
            await cache.incr_reconnects()
            emit(logger, logging.ERROR, "ws_error", service="lightning", url=url)
            logger.exception("lightning ws loop error on %s", url)

        if max_cycles is None or cycles < max_cycles:
            await sleep(backoff + random.uniform(0, backoff * 0.1))  # noqa: S311 — jitter, not crypto
            backoff = min(backoff * 2, settings.LIGHTNING_BACKOFF_MAX)  # capped exponential

    await cache.set_ws_connected(False)
