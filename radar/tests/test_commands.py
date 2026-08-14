"""Management commands: run_archiver guard/scheduling + seed_archive.

``asyncio_mode = auto`` runs the ``async def`` tests on the event loop; the sync
tests drive the commands exactly as the CLI does (``handle`` → ``asyncio.run``).
"""

from __future__ import annotations

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
    assert len(created) == 2  # supervised ingest + writer tasks
    ensure_mock.assert_awaited()  # partitions ensured before ingest


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
