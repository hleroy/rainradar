"""Management commands: run_archiver guard/scheduling + seed_archive.

``asyncio_mode = auto`` runs the ``async def`` tests on the event loop; the sync
tests drive the commands exactly as the CLI does (``handle`` → ``asyncio.run``).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from io import StringIO
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings

from radar.lightning import ingest as lightning_ingest
from radar.lightning import partitions as lightning_partitions
from radar.management.commands import run_archiver
from radar.models import RadarFrame

pytestmark = pytest.mark.django_db(transaction=True)

TILE_RE = re.compile(r"https://tilecache\.rainviewer\.com/.+\.png$")


@pytest.fixture
def tile_root(tmp_path):
    with override_settings(TILE_ROOT=str(tmp_path)):
        yield tmp_path


@pytest.fixture(autouse=True)
def fake_storage_gauge(monkeypatch):
    """The bootstrap fires the full-rescan gauge as a background task; stub it so a
    task outliving a test's ``override_settings`` never walks the real TILE_ROOT."""
    gauge = AsyncMock(return_value=0)
    monkeypatch.setattr(run_archiver.archiver, "update_storage_gauge", gauge)
    return gauge


def test_run_archiver_guard_disabled_exits_cleanly():
    out = StringIO()
    with override_settings(ARCHIVER_ENABLED=False):
        call_command("run_archiver", stdout=out)
    assert "not starting" in out.getvalue().lower()


async def test_safe_isolates_job_failures():
    async def good():
        return 42

    async def bad():
        raise RuntimeError("boom")

    assert await run_archiver._safe(good, "good") == 42
    assert await run_archiver._safe(bad, "bad") is None  # swallowed


async def test_wait_for_db_returns_when_ready():
    # DB is up in the test container -> returns immediately.
    await run_archiver.Command()._wait_for_db()


async def test_run_schedules_both_jobs(monkeypatch, tile_root):
    jobs: list[str] = []
    state = {"started": False}

    class FakeScheduler:
        def __init__(self, **_kw):
            pass

        def add_job(self, *_a, **kw):
            jobs.append(kw.get("id"))

        def start(self):
            state["started"] = True

    class FakeEvent:
        async def wait(self):
            return

    async def fake_poll(*_a, **_k):
        return {"frames_seen": 0, "archived": 0, "tiles_written": 0, "ok": True}

    monkeypatch.setattr(run_archiver, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(run_archiver.asyncio, "Event", FakeEvent)
    monkeypatch.setattr(run_archiver.archiver, "poll_radar", fake_poll)

    # Explicit LIGHTNING_ENABLED/METEOFRANCE_ENABLED=False so the case is deterministic
    # regardless of the env the test container inherits (.envs/.local/.django may
    # enable either — an enabled Météo-France adds a second poll job).
    with override_settings(
        ARCHIVER_ENABLED=True, LIGHTNING_ENABLED=False, METEOFRANCE_ENABLED=False
    ):
        await run_archiver.Command()._run()

    assert state["started"] is True
    assert set(jobs) == {"poll_radar", "retention_janitor"}  # no partition_maint when off


async def test_run_starts_lightning_when_enabled(monkeypatch, tile_root):
    jobs: list[str] = []
    created: list[object] = []

    class FakeScheduler:
        def __init__(self, **_kw):
            pass

        def add_job(self, *_a, **kw):
            jobs.append(kw.get("id"))

        def start(self):
            pass

    class FakeEvent:
        async def wait(self):
            return

    async def fake_poll(*_a, **_k):
        return {"frames_seen": 0, "archived": 0, "tiles_written": 0, "ok": True}

    def fake_create_task(coro):
        created.append(coro)
        coro.close()  # we only assert it was scheduled; avoid 'never awaited'

    ensure_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(run_archiver, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(run_archiver.asyncio, "Event", FakeEvent)
    monkeypatch.setattr(run_archiver.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(run_archiver.archiver, "poll_radar", fake_poll)
    monkeypatch.setattr(lightning_partitions, "ensure_partitions", ensure_mock)
    monkeypatch.setattr(lightning_ingest, "run_ingest", AsyncMock())
    monkeypatch.setattr(lightning_ingest, "run_writer", AsyncMock())

    # METEOFRANCE_ENABLED=False so an env-enabled provider's extra poll job doesn't
    # break the exact job-set assertion (the enabled case is covered separately).
    with override_settings(
        ARCHIVER_ENABLED=True, LIGHTNING_ENABLED=True, METEOFRANCE_ENABLED=False
    ):
        await run_archiver.Command()._run()

    assert "partition_maint" in jobs  # daily month pre-creation scheduled
    assert {"poll_radar", "retention_janitor", "partition_maint"} == set(jobs)
    assert len(created) == 3  # background storage-gauge rescan + ingest + writer tasks
    ensure_mock.assert_awaited()  # partitions ensured before ingest


async def test_startup_poll_does_not_wait_for_the_storage_rescan(monkeypatch, tile_root):
    """A cold-cache rescan takes minutes after a reboot; Météo-France only exposes its
    latest frame, so gating the first poll on it loses frames for good."""
    released = asyncio.Event()
    calls: list[str] = []

    async def slow_gauge():
        calls.append("gauge_started")
        await released.wait()  # never set: the rescan is still "walking"
        calls.append("gauge_done")
        return 0

    async def fake_poll(*_a, **_k):
        calls.append("poll")
        return {"frames_seen": 0, "archived": 0, "tiles_written": 0, "ok": True}

    class FakeScheduler:
        def __init__(self, **_kw):
            pass

        def add_job(self, *_a, **_kw):
            pass

        def start(self):
            calls.append("scheduler_started")

    class FakeEvent:
        async def wait(self):
            await asyncio.sleep(0)  # let the background gauge task begin

    monkeypatch.setattr(run_archiver.archiver, "update_storage_gauge", slow_gauge)
    monkeypatch.setattr(run_archiver.archiver, "poll_radar", fake_poll)
    monkeypatch.setattr(run_archiver, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(run_archiver.asyncio, "Event", FakeEvent)

    cmd = run_archiver.Command()
    with override_settings(
        ARCHIVER_ENABLED=True, LIGHTNING_ENABLED=False, METEOFRANCE_ENABLED=False
    ):
        await asyncio.wait_for(cmd._run(), timeout=5)

    try:
        assert calls[0] == "poll"  # the catch-up poll ran first, not after the rescan
        assert "scheduler_started" in calls
        assert "gauge_started" in calls
        assert "gauge_done" not in calls
        assert not cmd._gauge_task.done()  # still running in the background
    finally:
        cmd._gauge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cmd._gauge_task


async def test_run_schedules_meteofrance_job_when_enabled(monkeypatch, tile_root):
    """METEOFRANCE_ENABLED ⇒ a second, isolated poll job is scheduled."""
    jobs: list[str] = []

    class FakeScheduler:
        def __init__(self, **_kw):
            pass

        def add_job(self, *_a, **kw):
            jobs.append(kw.get("id"))

        def start(self):
            pass

    class FakeEvent:
        async def wait(self):
            return

    async def fake_poll(*_a, **_k):
        return {"frames_seen": 0, "archived": 0, "tiles_written": 0, "ok": True}

    monkeypatch.setattr(run_archiver, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(run_archiver.asyncio, "Event", FakeEvent)
    monkeypatch.setattr(run_archiver.archiver, "poll_radar", fake_poll)

    with override_settings(
        ARCHIVER_ENABLED=True,
        LIGHTNING_ENABLED=False,
        METEOFRANCE_ENABLED=True,
        METEOFRANCE_APPLICATION_ID="dGVzdA==",
    ):
        await run_archiver.Command()._run()

    assert set(jobs) == {"poll_radar", "retention_janitor", "poll_radar_meteofrance"}


async def _capture_jobs(monkeypatch, **flags) -> dict[str, tuple]:
    """Run the bootstrap with fakes; return each scheduled job's (callable, name) args."""
    jobs: dict[str, tuple] = {}

    class FakeScheduler:
        def __init__(self, **_kw):
            pass

        def add_job(self, *_a, **kw):
            jobs[kw.get("id")] = kw.get("args")

        def start(self):
            pass

    class FakeEvent:
        async def wait(self):
            return

    async def fake_poll(*_a, **_k):
        return {"frames_seen": 0, "archived": 0, "tiles_written": 0, "ok": True}

    monkeypatch.setattr(run_archiver, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(run_archiver.asyncio, "Event", FakeEvent)
    monkeypatch.setattr(run_archiver.archiver, "poll_radar", fake_poll)
    with override_settings(ARCHIVER_ENABLED=True, LIGHTNING_ENABLED=False, **flags):
        await run_archiver.Command()._run()
    return jobs


async def test_run_schedules_the_forecast_job_when_enabled(monkeypatch, tile_root):
    """FORECAST_ENABLED (with METEOFRANCE_ENABLED) ⇒ its own isolated job."""
    jobs = await _capture_jobs(
        monkeypatch,
        METEOFRANCE_ENABLED=True,
        METEOFRANCE_APPLICATION_ID="dGVzdA==",
        FORECAST_ENABLED=True,
    )
    assert set(jobs) == {
        "poll_radar",
        "retention_janitor",
        "poll_radar_meteofrance",
        "poll_forecast",
    }


async def test_no_forecast_job_without_meteofrance(monkeypatch, tile_root):
    jobs = await _capture_jobs(monkeypatch, METEOFRANCE_ENABLED=False, FORECAST_ENABLED=True)
    assert "poll_forecast" not in jobs


async def test_a_crashing_forecast_job_leaves_the_radar_polls_alone(monkeypatch, tile_root):
    """The fifth failure domain: a forecast crash is swallowed by its own _safe."""
    from radar.forecast import job as forecast_job  # noqa: PLC0415

    jobs = await _capture_jobs(
        monkeypatch,
        METEOFRANCE_ENABLED=True,
        METEOFRANCE_APPLICATION_ID="dGVzdA==",
        FORECAST_ENABLED=True,
    )

    async def boom(*_a, **_k):
        raise RuntimeError("PIAF exploded")

    monkeypatch.setattr(forecast_job, "poll_forecast", boom)
    assert await run_archiver._safe(*jobs["poll_forecast"]) is None
    # The radar polls are separate jobs with their own _safe and still run.
    assert (await run_archiver._safe(*jobs["poll_radar"]))["ok"] is True
    assert (await run_archiver._safe(*jobs["poll_radar_meteofrance"]))["ok"] is True


@override_settings(METEOFRANCE_ENABLED=False)  # only RainViewer is mocked below
@respx.mock
def test_seed_archive_backfills_once(tile_root, sample_weather_maps, png_bytes):
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    respx.get(url__regex=TILE_RE).mock(return_value=httpx.Response(200, content=png_bytes))

    out = StringIO()
    call_command("seed_archive", stdout=out)

    assert "archived=3" in out.getvalue()
    assert RadarFrame.objects.count() == 3
