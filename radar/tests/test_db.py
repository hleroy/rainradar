"""Archiver DB connection recovery after Postgres drops the connection.

The archiver is one long-lived process outside Django's request cycle, so nothing
ever discards its connection: after Postgres terminates it (a restart, a deploy
swap), every ORM call fails with "the connection is closed" until the process
restarts. These tests kill the real backend with ``pg_terminate_backend`` — the
same ``AdminShutdown`` production saw — and check each error path recovers.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async
from django.db import OperationalError
from django.db import connection

from radar.db import discard_unusable_connections
from radar.lightning.base import Strike
from radar.lightning.ingest import persist_and_fanout
from radar.management.commands import run_archiver
from radar.models import LightningStrike
from radar.models import RadarFrame

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
async def _no_dead_connection_leaks():
    # Async ORM calls share one long-lived worker thread, and Django connections are
    # per-thread: a test that leaves its connection terminated would hand it to
    # every later test in the process.
    yield
    await discard_unusable_connections()


@sync_to_async
def _terminate_own_backend() -> None:
    """Have Postgres kill this connection, as a server restart would."""
    with pytest.raises(OperationalError), connection.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(pg_backend_pid())")


async def test_a_terminated_connection_stays_broken_on_its_own():
    await RadarFrame.objects.aexists()
    await _terminate_own_backend()
    for _ in range(2):  # not a one-off: every later query fails the same way
        with pytest.raises(OperationalError):
            await RadarFrame.objects.aexists()


@sync_to_async
def _raw_connection():
    return connection.connection


def _discarded(caplog) -> list[str]:
    return [
        r.alias for r in caplog.records if getattr(r, "event", None) == "db_connection_discarded"
    ]


async def test_discard_reconnects_after_the_server_dropped_the_connection(caplog):
    await RadarFrame.objects.aexists()
    await _terminate_own_backend()
    with caplog.at_level(logging.WARNING, logger="radar.db"):
        await discard_unusable_connections()
    assert _discarded(caplog) == ["default"]
    assert await RadarFrame.objects.aexists() is False  # reconnected, query works


async def test_discard_leaves_a_healthy_connection_alone(caplog):
    await RadarFrame.objects.aexists()
    before = await _raw_connection()
    with caplog.at_level(logging.WARNING, logger="radar.db"):
        await discard_unusable_connections()
    assert _discarded(caplog) == []
    assert await _raw_connection() is before  # same socket, not reopened


async def test_strike_writer_recovers_after_one_dropped_batch():
    # The writer swallows DB errors by design, so it must recover on its own:
    # one batch is lost to the dead connection, the next one is written.
    # The table is managed=False (never truncated), so use a lat band of our own.
    band = LightningStrike.objects.filter(lat__gte=13.6, lat__lte=13.7)
    await band.adelete()
    await _terminate_own_backend()
    strikes = [Strike(struck_at=1718960400.5, lat=13.65, lon=3.0, intensity=1)]
    assert await persist_and_fanout(strikes) == 0
    assert await persist_and_fanout(strikes) == 1
    assert await band.acount() == 1


async def test_safe_job_recovers_on_its_next_run():
    await RadarFrame.objects.aexists()
    await _terminate_own_backend()
    assert await run_archiver._safe(RadarFrame.objects.acount, "probe") is None  # crashed
    assert await run_archiver._safe(RadarFrame.objects.acount, "probe") == 0  # recovered


async def test_supervised_task_recovers_instead_of_crash_looping(monkeypatch):
    # The production failure: run_evaluator crash-looped once a second on a dead
    # connection. After one crash the restart must find a working connection.
    monkeypatch.setattr("asyncio.sleep", AsyncMock())  # skip _supervise's 1 s restart delay
    calls = 0

    async def task():
        nonlocal calls
        calls += 1
        if calls > 5:  # crash-looping: bail out so the assert below fails, not hangs
            return
        await RadarFrame.objects.aexists()  # raises while the connection is dead

    await RadarFrame.objects.aexists()
    await _terminate_own_backend()
    await run_archiver._supervise(task)  # returns once the task completes cleanly
    assert calls == 2
