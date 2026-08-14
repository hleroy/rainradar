"""``manage.py run_archiver`` — the archiver scheduler bootstrap.

Builds an APScheduler ``AsyncIOScheduler`` on the asyncio loop with two jobs —
the radar poll (every ``POLL_INTERVAL``) and the retention janitor (daily at
``JANITOR_HOUR`` UTC) — then runs forever. This is the **only** place
APScheduler runs; it refuses to start unless ``ARCHIVER_ENABLED`` is true, so
the ``django`` web container never schedules anything. Never scale this beyond
one replica (duplicate schedulers ⇒ duplicate fetches).
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand

from radar import archiver
from radar import storage
from radar.logging_json import emit
from radar.models import RadarFrame
from radar.providers import enabled_providers
from radar.providers import get_provider

logger = logging.getLogger("radar.archiver")

# Allow a poll to start late (e.g. the previous one ran long) without misfiring.
_MISFIRE_GRACE = 120
_DB_WAIT_ATTEMPTS = 60
_DB_WAIT_DELAY = 2.0


class Command(BaseCommand):
    help = "Run the radar archiver (poll + retention). Single replica only."

    def handle(self, *_args, **_options) -> None:
        if not settings.ARCHIVER_ENABLED:
            # Guard: the web container leaves this false and exits cleanly.
            self.stdout.write("ARCHIVER_ENABLED is false — not starting the scheduler.")
            return
        asyncio.run(self._run())

    async def _run(self) -> None:
        await self._wait_for_db()

        # One-time, idempotent fold of the legacy tile layout under rainviewer/,
        # before the scheduler starts and before anything reads a tile.
        moved = await sync_to_async(storage.migrate_legacy_layout)()
        emit(logger, logging.INFO, "legacy_layout_migrated", moved=moved)

        # Set the storage gauge's absolute baseline once (full rescan) so the
        # per-poll increments start from the real on-disk size after a restart.
        await _safe(archiver.update_storage_gauge, "update_storage_gauge")

        # Catch up immediately on startup (post-restart backfill self-heals
        # sub-window outages), then schedule the recurring jobs.
        await _safe(archiver.poll_radar, "poll_radar")

        # Météo-France is a fourth failure domain: its own provider
        # instance, its own isolated poll job. Resolve it once so the startup
        # catch-up and the interval job share one instance (single-flight memo + auth).
        mf_provider = get_provider("meteofrance") if settings.METEOFRANCE_ENABLED else None
        if mf_provider is not None:
            await _safe(lambda: archiver.poll_radar(mf_provider), "poll_radar_meteofrance")

        # Lightning ingester: only here, only when LIGHTNING_ENABLED, so
        # exactly one WS consumer exists (never scale this container). It runs as
        # independent supervised tasks on this same loop — a WS/parse/DB failure in
        # lightning can never block or crash the radar poll/scheduler.
        await self._start_lightning()

        # Storm-alert push evaluator: only here, only when BOTH push and
        # lightning are enabled, so exactly one evaluator exists (never scale this
        # container — duplicate evaluators ⇒ duplicate notifications). A third failure
        # domain: supervised like lightning, it can never affect radar or ingestion.
        await self._start_alerts()

        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            _safe,
            IntervalTrigger(seconds=settings.POLL_INTERVAL),
            args=(archiver.poll_radar, "poll_radar"),
            id="poll_radar",
            misfire_grace_time=_MISFIRE_GRACE,
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            _safe,
            CronTrigger(hour=settings.JANITOR_HOUR, minute=0, timezone="UTC"),
            args=(archiver.retention_janitor, "retention_janitor"),
            id="retention_janitor",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        if mf_provider is not None:
            # Second isolated poll job: a Météo-France failure here can
            # never touch the RainViewer job above — separate job, its own _safe.
            scheduler.add_job(
                _safe,
                IntervalTrigger(seconds=settings.METEOFRANCE_POLL_INTERVAL),
                args=(lambda: archiver.poll_radar(mf_provider), "poll_radar_meteofrance"),
                id="poll_radar_meteofrance",
                misfire_grace_time=_MISFIRE_GRACE,
                coalesce=True,
                max_instances=1,
            )
        if settings.LIGHTNING_ENABLED:
            # Pre-create months N+1/N+2 daily so an insert never falls through to
            # the DEFAULT partition on the 1st. Idempotent.
            from radar.lightning import partitions as lightning_partitions  # noqa: PLC0415

            scheduler.add_job(
                _safe,
                CronTrigger(hour=settings.JANITOR_HOUR, minute=30, timezone="UTC"),
                args=(lambda: lightning_partitions.ensure_partitions(2), "partition_maint"),
                id="partition_maint",
                misfire_grace_time=3600,
                coalesce=True,
                max_instances=1,
            )
        scheduler.start()
        emit(
            logger,
            logging.INFO,
            "archiver_started",
            poll_interval=settings.POLL_INTERVAL,
            janitor_hour=settings.JANITOR_HOUR,
            providers=enabled_providers(),
        )
        # Run forever; the scheduler drives the jobs on this loop.
        await asyncio.Event().wait()

    async def _start_lightning(self) -> None:
        """Boot the lightning ingester as supervised tasks, if enabled."""
        if not settings.LIGHTNING_ENABLED:
            return
        from radar.lightning import get_active_source  # noqa: PLC0415
        from radar.lightning import ingest as lightning_ingest  # noqa: PLC0415
        from radar.lightning import partitions as lightning_partitions  # noqa: PLC0415

        # Ensure month partitions exist before the first insert (idempotent).
        await _safe(lambda: lightning_partitions.ensure_partitions(2), "ensure_partitions")

        queue: asyncio.Queue = asyncio.Queue(maxsize=settings.LIGHTNING_QUEUE_MAXSIZE)
        source = get_active_source()
        # Decoupled tasks: WS read+filter+enqueue, and a batch writer draining the
        # queue. _supervise restarts either on an unexpected crash. Keep
        # strong references so the loop never garbage-collects a live task.
        self._lightning_tasks = [
            asyncio.create_task(_supervise(lightning_ingest.run_ingest, source, queue)),
            asyncio.create_task(_supervise(lightning_ingest.run_writer, queue)),
        ]
        emit(logger, logging.INFO, "lightning_started", service="lightning", source=source.name)

    async def _start_alerts(self) -> None:
        """Boot the push evaluator as a supervised task, if enabled."""
        if not (settings.PUSH_ALERTS_ENABLED and settings.LIGHTNING_ENABLED):
            return
        from radar.alerts import evaluator as alerts_evaluator  # noqa: PLC0415

        # Strong reference so the loop never garbage-collects the live task.
        self._alerts_task = asyncio.create_task(_supervise(alerts_evaluator.run_evaluator))
        emit(logger, logging.INFO, "alerts_started", service="alerts")

    async def _wait_for_db(self) -> None:
        """Block until migrations have created the archive tables."""
        for attempt in range(_DB_WAIT_ATTEMPTS):
            try:
                await RadarFrame.objects.aexists()
            except Exception:  # noqa: BLE001 — DB/table not ready yet; keep waiting
                if attempt == 0:
                    self.stdout.write("Waiting for the database (migrations)…")
                await asyncio.sleep(_DB_WAIT_DELAY)
            else:
                return
        msg = "Database not ready after waiting; aborting archiver startup."
        raise RuntimeError(msg)


async def _safe(job, name: str):
    """Run a job, isolating its failures so the scheduler keeps running."""
    try:
        return await job()
    except Exception:
        emit(logger, logging.ERROR, "job_crashed", job=name)
        logger.exception("archiver job %s crashed", name)
        return None


async def _supervise(coro_fn, *args) -> None:
    """Keep a long-lived lightning task alive across unexpected crashes.

    The task functions already loop forever with broad inner handling; this is the
    belt-and-suspenders outer net: log an unexpected escape and restart, but exit
    cleanly on cancellation (archiver shutdown). A clean return is not restarted.
    """
    while True:
        try:
            await coro_fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            name = getattr(coro_fn, "__name__", "?")
            emit(logger, logging.ERROR, "lightning_task_crashed", service="lightning")
            logger.exception("lightning task %s crashed; restarting", name)
            await asyncio.sleep(1.0)
        else:
            return  # clean completion (e.g. stop set) — don't restart
