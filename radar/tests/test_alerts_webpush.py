"""The pywebpush send boundary.

``respx`` mocks httpx, not pywebpush's ``requests`` — so we monkeypatch the ``webpush``
symbol inside the module. No test ever performs real push traffic.
"""

from __future__ import annotations

import pytest

from radar.alerts import webpush

pytestmark = pytest.mark.asyncio


class _Sub:
    endpoint = "https://fcm.googleapis.com/fcm/send/x"
    p256dh = "kkk"
    auth = "aaa"


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


def _raise(status):
    def _inner(**_kw):
        raise webpush.WebPushException("boom", response=_Resp(status))

    return _inner


async def test_send_ok(monkeypatch):
    monkeypatch.setattr(webpush, "webpush", lambda **_kw: None)
    assert await webpush.send(_Sub(), {"title": "t", "body": "b"}) == "ok"


@pytest.mark.parametrize("status", [404, 410])
async def test_send_gone_on_404_410(monkeypatch, status):
    monkeypatch.setattr(webpush, "webpush", _raise(status))
    assert await webpush.send(_Sub(), {"title": "t"}) == "gone"


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_send_failed_on_other_status(monkeypatch, status):
    monkeypatch.setattr(webpush, "webpush", _raise(status))
    assert await webpush.send(_Sub(), {"title": "t"}) == "failed"


async def test_send_failed_on_generic_exception(monkeypatch):
    def boom(**_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(webpush, "webpush", boom)
    assert await webpush.send(_Sub(), {"title": "t"}) == "failed"


async def test_send_failed_on_timeout(monkeypatch):
    import time  # noqa: PLC0415

    monkeypatch.setattr(webpush, "webpush", lambda **_kw: time.sleep(2))
    from django.test import override_settings  # noqa: PLC0415

    with override_settings(PUSH_SEND_TIMEOUT=0.05):
        assert await webpush.send(_Sub(), {"title": "t"}) == "failed"
