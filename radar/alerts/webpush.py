"""The single Web Push send boundary.

The **only** module that imports ``pywebpush`` and the only one that touches the
network. ``pywebpush`` is synchronous (``requests``-based), so every send runs in
``asyncio.to_thread`` behind a bounded, loop-scoped semaphore and a per-send
timeout — a slow or hung push service can never stall the evaluator loop.

``send`` classifies the outcome into three strings the evaluator acts on:
``"ok"`` (delivered), ``"gone"`` (404/410 → the subscription is dead, prune it),
``"failed"`` (anything else → count it, keep the subscription). It never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging

from django.conf import settings
from pywebpush import WebPushException
from pywebpush import webpush

logger = logging.getLogger("radar.alerts")

_GONE_STATUSES = frozenset({404, 410})

_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """A concurrency limiter bound to the running loop (rebuilt if the loop changes)."""
    global _semaphore, _semaphore_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(settings.PUSH_SEND_CONCURRENCY)
        _semaphore_loop = loop
    return _semaphore


def _send_sync(subscription_info: dict, data: str) -> None:
    """Blocking pywebpush call — runs in a worker thread (never on the event loop)."""
    webpush(
        subscription_info=subscription_info,
        data=data,
        vapid_private_key=settings.VAPID_PRIVATE_KEY,
        vapid_claims={"sub": settings.VAPID_SUBJECT},
        ttl=settings.PUSH_TTL_SECONDS,
    )


async def send(subscription, payload: dict) -> str:
    """Send one push. Returns ``"ok"`` / ``"gone"`` / ``"failed"``; never raises."""
    subscription_info = {
        "endpoint": subscription.endpoint,
        "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
    }
    data = json.dumps(payload)
    try:
        async with _get_semaphore():
            await asyncio.wait_for(
                asyncio.to_thread(_send_sync, subscription_info, data),
                timeout=settings.PUSH_SEND_TIMEOUT,
            )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in _GONE_STATUSES:
            return "gone"
        logger.warning("web push failed: status=%s", status)
        return "failed"
    except Exception:  # incl. TimeoutError; a send must never crash the loop
        logger.warning("web push errored", exc_info=True)
        return "failed"
    return "ok"
