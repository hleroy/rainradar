"""The forecast job: find the newest run, render its lead times, publish it atomically.

Runs in the archiver only, as its own scheduler job (``poll_forecast``,
``max_instances=1``). It is the fifth failure domain: everything it does is
contained here and in ``_safe``, and its only reads from the observation side are the
Météo-France provider's token holder and newest coverage — both passive.

One tick:

1. **Which run?** Runs are on a fixed clock and are published ~9 minutes late
   (``specs/forecast-nowcast.md`` §8.5), so the candidates are the newest run that can
   plausibly exist and the one before it — skipping any not newer than the run already
   published. Outside the minute or so a run spends arriving, a tick costs nothing.
2. **The first lead is the probe.** A 404 there means "not yet" (a ~600-byte answer)
   and the tick moves on; nothing is published, nothing counts as failed.
3. **Every lead, then publish.** The remaining lead times download concurrently; each
   is decoded strictly, masked to the newest observed radar coverage, rendered, and
   written tile by tile with a per-frame ``.done`` marker. Only once every frame is
   done is the run's ``run.json`` written and the Redis pointer moved — so a
   half-written run is never advertised, and a retry re-fetches only missing frames.
4. **Prune**, every tick and whatever it did: keep the newest ``FORECAST_RETAIN_RUNS``
   complete runs, so a client mid-animation keeps its tiles, plus any partial run the
   job may still finish; delete everything else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.conf import settings

from radar import cache
from radar import storage
from radar.forecast.base import ForecastUnavailable
from radar.forecast.base import PayloadRejected
from radar.forecast.base import RunNotReady
from radar.forecast.piaf import PiafSource
from radar.forecast.render import render_forecast_frame
from radar.logging_json import emit

if TYPE_CHECKING:
    import httpx

    from radar.forecast.base import ForecastSource
    from radar.providers.meteofrance_render import Coverage

logger = logging.getLogger("radar.forecast")

STATUS_IDLE = "idle"  # nothing newer than the published run can exist yet
STATUS_NO_COVERAGE = "no_coverage"  # no recent observed coverage to mask with
STATUS_NOT_READY = "not_ready"  # the expected run is not published upstream yet
STATUS_FAILED = "failed"
STATUS_PUBLISHED = "published"
STATUS_COOLING_DOWN = "cooling_down"  # the candidate failed moments ago

# run -> wall-clock time before which a failed run is not retried. Without it a run
# that keeps failing (a rejected lead, a 4xx) would be re-downloaded every tick for
# the ~10 minutes it stays a candidate. Wall clock, like the run times it guards.
_failed_until: dict[int, float] = {}

# run -> the coverage its frames are masked to, pinned when its first frame is rendered.
# A run that takes several ticks would otherwise mix masks frame by frame — the coverage
# edge shifting mid-animation — and its record would name only the last. Pins live only
# while their run is a candidate, so at most two ~2 MB masks are held.
_run_coverage: dict[int, Coverage] = {}


def lead_seconds() -> list[int]:
    """The lead times fetched per run: first lead to horizon + first lead (13 by default)."""
    first = settings.FORECAST_FIRST_LEAD_SECONDS
    last = first + settings.FORECAST_HORIZON_SECONDS
    return list(range(first, last + 1, settings.FORECAST_LEAD_STEP_SECONDS))


def _newest_plausible_run(now: float, source: ForecastSource) -> int:
    interval = source.run_interval
    return int((now - source.publication_delay) // interval) * interval


def candidate_runs(now: float, source: ForecastSource, published: int) -> list[int]:
    """Runs worth asking for, newest first: never one already published or older."""
    newest = _newest_plausible_run(now, source)
    return [run for run in (newest, newest - source.run_interval) if run > published]


async def published_run() -> int:
    """The run currently advertised, rebuilding the pointer from disk after a flush."""
    record = await cache.get_forecast_current()
    if record is not None:
        return int(record["run"])
    record = await sync_to_async(_newest_complete_record)()
    if record is None:
        return 0
    await cache.set_forecast_current(record)
    return int(record["run"])


def _coverage_for_run(run_ts: int, latest: Coverage) -> Coverage:
    """The coverage to mask every frame of ``run_ts`` to (sync: may touch the disk).

    Until a frame is on disk the run follows the newest coverage; from its first frame
    on it keeps that one. Frames found on disk without a pin predate an archiver
    restart and their mask is unknown, so the run starts over rather than mixing.
    """
    has_frames = storage.forecast_run_dir(run_ts).exists()
    pinned = _run_coverage.get(run_ts)
    if pinned is not None and has_frames:
        return pinned
    if has_frames:
        storage.remove_forecast_run(run_ts)
    _run_coverage[run_ts] = latest
    return latest


def _newest_complete_record() -> dict | None:
    for run in reversed(storage.forecast_runs()):
        record = storage.read_forecast_run(run)
        if record is not None:
            return record
    return None


# -- one frame ----------------------------------------------------------------


def _store_frame(run_ts: int, valid_ts: int, tiles: dict) -> int:
    """Write a rendered frame's non-empty tiles, then its ``.done`` marker (sync)."""
    written = 0
    for (z, x, y), png in tiles.items():
        if png is not None:
            storage.write_forecast_tile(run_ts, valid_ts, z, x, y, png)
            written += 1
    storage.mark_forecast_frame_done(run_ts, valid_ts, written)
    return written


async def _build_frame(  # noqa: PLR0913, PLR0917 — the frame's identity plus its inputs
    source: ForecastSource,
    client: httpx.AsyncClient,
    run_ts: int,
    valid_ts: int,
    coverage: Coverage,
    totals: dict[str, int],
) -> None:
    if await sync_to_async(storage.forecast_frame_done)(run_ts, valid_ts):
        return  # done on an earlier tick: no download, no render
    message = await source.download(run_ts, valid_ts, client=client)
    totals["bytes"] += len(message)
    field = await asyncio.to_thread(source.decode, message, run_ts, valid_ts)
    tiles = await asyncio.to_thread(render_forecast_frame, field, coverage)
    # Awaited before the +=: `totals[k] += await …` reads the old total before the
    # await, so concurrent leads would overwrite each other's counts.
    written = await sync_to_async(_store_frame)(run_ts, valid_ts, tiles)
    totals["tiles"] += written


async def _build_run(
    source: ForecastSource,
    client: httpx.AsyncClient,
    run_ts: int,
    coverage: Coverage,
    totals: dict[str, int],
) -> list[int]:
    """Render every lead of one run to disk; returns its valid times, oldest first."""
    valids = [run_ts + lead for lead in lead_seconds()]
    # The first lead alone: it is the probe, and a 404 here must cost nothing more.
    await _build_frame(source, client, run_ts, valids[0], coverage, totals)

    sem = asyncio.Semaphore(settings.FORECAST_DOWNLOAD_CONCURRENCY)

    async def guarded(valid_ts: int) -> None:
        async with sem:
            await _build_frame(source, client, run_ts, valid_ts, coverage, totals)

    results = await asyncio.gather(*(guarded(v) for v in valids[1:]), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return valids


# -- publish + prune ----------------------------------------------------------


def _prune(now: float, source: ForecastSource, published: int) -> tuple[int, int]:
    """Delete every run that neither a client nor the job can still use (sync).

    Kept: the newest ``FORECAST_RETAIN_RUNS`` complete runs (a client mid-animation
    still holds their URLs), the published run, and any run still in the candidate
    window, which the job may yet finish. A partial run older than that window is
    abandoned: :func:`candidate_runs` never offers it again. Pruning only after a
    publish would let runs that keep failing part-way pile up while nothing gets
    published — on the volume the archive shares, and outside its storage gauge.
    """
    runs = storage.forecast_runs()
    complete = [r for r in runs if storage.read_forecast_run(r) is not None]
    keep = set(complete[-settings.FORECAST_RETAIN_RUNS :]) | {published}
    oldest_candidate = _newest_plausible_run(now, source) - source.run_interval
    removed = freed = 0
    for run in runs:
        if run in keep or run >= oldest_candidate:
            continue
        freed += storage.remove_forecast_run(run)
        removed += 1
    return removed, freed


async def _prune_safely(now: float, source: ForecastSource, published: int) -> None:
    """Prune, logging rather than raising: a disk error must not mask the tick's own."""
    try:
        removed, freed = await sync_to_async(_prune)(now, source, published)
    except OSError:
        logger.exception("forecast prune failed")
        return
    if removed:
        emit(logger, logging.INFO, "forecast_pruned", runs=removed, freed_bytes=freed)


async def _publish(run_ts: int, valids: list[int], coverage: Coverage, source: ForecastSource):
    record = {
        "run": run_ts,
        "frames": valids,
        "coverage_ts": coverage.ts,
        "published_at": int(time.time()),
        "source": source.name,
    }
    await sync_to_async(storage.write_forecast_run)(run_ts, record)  # disk first…
    await cache.set_forecast_current(record)  # …then advertise
    return record


# -- the job ------------------------------------------------------------------


async def poll_forecast(
    provider,
    source: ForecastSource | None = None,
    *,
    now: float | None = None,
) -> dict:
    """One forecast tick. ``provider`` is the archiver's Météo-France provider."""
    source = source if source is not None else PiafSource(provider.auth)
    now = time.time() if now is None else now
    current = await published_run()
    published = current
    try:
        result = await _tick(provider, source, now, current)
        if result["status"] == STATUS_PUBLISHED:
            published = result["run"]
        return result
    finally:
        # Every tick, even one that failed or crashed: a run that keeps failing
        # part-way must not leave its frames behind for good.
        await _prune_safely(now, source, published)


def _retryable(candidates: list[int], now: float) -> list[int]:
    """The candidates not cooling down, after expiring the job's per-run memory.

    Drops cooldowns that have run out and the coverage pins of runs that are no longer
    candidates (published, or out of the window: never built again).
    """
    for run in [r for r in _run_coverage if r not in candidates]:
        del _run_coverage[run]
    for run in [r for r, until in _failed_until.items() if until <= now]:
        del _failed_until[run]
    return [run for run in candidates if run not in _failed_until]


async def _tick(provider, source: ForecastSource, now: float, current: int) -> dict:
    t0 = time.monotonic()
    candidates = candidate_runs(now, source, current)
    retryable = _retryable(candidates, now)
    if not candidates:
        return {"status": STATUS_IDLE, "run": current}
    if not retryable:
        return {"status": STATUS_COOLING_DOWN, "run": current}
    candidates = retryable

    latest = provider.latest_coverage()
    if latest is None or now - latest.ts > settings.FORECAST_COVERAGE_MAX_AGE:
        # No forecast is better than one masked to the wrong coverage, or none at all.
        return {"status": STATUS_NO_COVERAGE, "run": current}

    totals = {"bytes": 0, "tiles": 0}
    status, run_ts, valids, coverage = STATUS_NOT_READY, None, [], latest
    async with source.client() as client:
        for candidate in candidates:
            try:
                coverage = await sync_to_async(_coverage_for_run)(candidate, latest)
                valids = await _build_run(source, client, candidate, coverage, totals)
            except RunNotReady:
                await cache.incr_forecast("not_ready")
                continue
            except ForecastUnavailable as exc:
                counter = "rejected" if isinstance(exc, PayloadRejected) else "fetch_failed"
                await cache.incr_forecast(counter)
                emit(
                    logger,
                    logging.WARNING,
                    "forecast_run_failed",
                    run=candidate,
                    reason=counter,
                    error=str(exc)[:200],
                )
                _failed_until[candidate] = now + settings.FORECAST_FAILURE_COOLDOWN
                status, run_ts = STATUS_FAILED, candidate
                break
            except Exception as exc:  # noqa: BLE001 — anything else (a full disk, a bug)
                # Treated like a failed run rather than left to _safe: cooled down, so
                # it is not retried every tick, and counted, so it reaches /metrics.
                # The traceback is logged once per attempt, not once per tick.
                await cache.incr_forecast("crashed")
                emit(
                    logger,
                    logging.ERROR,
                    "forecast_run_failed",
                    run=candidate,
                    reason="crashed",
                    error=repr(exc)[:200],
                    exc_info=True,
                )
                _failed_until[candidate] = now + settings.FORECAST_FAILURE_COOLDOWN
                status, run_ts = STATUS_FAILED, candidate
                break
            status, run_ts = STATUS_PUBLISHED, candidate
            break

    if totals["bytes"]:
        await cache.incr_forecast("bytes", totals["bytes"])
    if status != STATUS_PUBLISHED:
        return {"status": status, "run": run_ts or current, "bytes": totals["bytes"]}

    record = await _publish(run_ts, valids, coverage, source)
    _run_coverage.pop(run_ts, None)
    await cache.incr_forecast("runs_published")
    emit(
        logger,
        logging.INFO,
        "forecast_published",
        run=run_ts,
        frames=len(valids),
        lag_s=record["published_at"] - run_ts,
        coverage_age_s=int(now - coverage.ts),
        bytes=totals["bytes"],
        tiles=totals["tiles"],
        duration_ms=round((time.monotonic() - t0) * 1000.0, 1),
    )
    return {
        "status": STATUS_PUBLISHED,
        "run": run_ts,
        "frames": valids,
        "bytes": totals["bytes"],
        "tiles": totals["tiles"],
    }
