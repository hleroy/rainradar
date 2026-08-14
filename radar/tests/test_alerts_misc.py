"""Push metrics, janitor pruning, and archiver gating."""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from django.test import override_settings

from radar import archiver
from radar.models import PushSubscription

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def _sub(endpoint):
    return await PushSubscription.objects.acreate(
        endpoint=endpoint,
        p256dh="k",
        auth="a",
        lat=45.0,
        lon=3.0,
    )


# -- metrics ------------------------------------------------------------------


async def test_metrics_includes_alert_series(async_client):
    resp = await async_client.get("/metrics")
    body = resp.content.decode()
    for name in (
        "alerts_subscriptions",
        "alerts_push_sent_total",
        "alerts_push_failed_total",
        "alerts_push_pruned_total",
    ):
        assert name in body


# -- janitor prune ------------------------------------------------------------


async def test_janitor_prunes_stale_subscriptions():
    now = datetime.now(tz=UTC)
    fresh = await _sub("https://fcm.googleapis.com/fresh")
    stale = await _sub("https://fcm.googleapis.com/stale")
    # aupdate bypasses auto_now, letting us backdate last_seen_at past the horizon.
    old = now - timedelta(days=90)
    await PushSubscription.objects.filter(pk=stale.pk).aupdate(last_seen_at=old)
    with override_settings(PUSH_ALERTS_ENABLED=True, PUSH_STALE_DAYS=60):
        deleted = await archiver._prune_stale_subscriptions(now)
    assert deleted == 1
    assert await PushSubscription.objects.filter(pk=fresh.pk).aexists()
    assert not await PushSubscription.objects.filter(pk=stale.pk).aexists()


async def test_janitor_prune_noop_when_push_off():
    await _sub("https://fcm.googleapis.com/x")
    with override_settings(PUSH_ALERTS_ENABLED=False):
        assert await archiver._prune_stale_subscriptions(datetime.now(tz=UTC)) == 0
    assert await PushSubscription.objects.acount() == 1  # untouched


# -- archiver gating ----------------------------------------------------------


async def test_start_alerts_noop_when_flags_off():
    from radar.management.commands.run_archiver import Command  # noqa: PLC0415

    cmd = Command()
    with override_settings(PUSH_ALERTS_ENABLED=False, LIGHTNING_ENABLED=True):
        await cmd._start_alerts()
    assert not hasattr(cmd, "_alerts_task")


async def test_start_alerts_starts_when_both_flags_on(monkeypatch):
    from radar.alerts import evaluator  # noqa: PLC0415
    from radar.management.commands.run_archiver import Command  # noqa: PLC0415

    async def _noop() -> None:
        return None

    monkeypatch.setattr(evaluator, "run_evaluator", _noop)
    cmd = Command()
    with override_settings(PUSH_ALERTS_ENABLED=True, LIGHTNING_ENABLED=True):
        await cmd._start_alerts()
    assert cmd._alerts_task is not None
    await asyncio.wait_for(cmd._alerts_task, timeout=5)
