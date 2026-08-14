"""Archive statistics for the About dialog's ``GET /api/stats``.

Re-shapes counts the app already gathers for ``/metrics`` (``radar/metrics.py``)
into the pinned JSON shape. Read-only and best-effort: every individual
read is guarded so a single failing piece becomes ``None`` (rendered ``null``)
while the rest still returns — the view never 5xxs and never touches the archiver
or the lightning failure domain. All epoch fields are UTC seconds (ints) or null.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db.models import Max
from django.db.models import Min

from radar import cache
from radar.models import LightningStrike
from radar.models import RadarFrame
from radar.providers import enabled_providers


async def _safe(coro_factory, default: Any = None) -> Any:
    """Await ``coro_factory()`` and return its result, or ``default`` on any error.

    A Redis or Postgres hiccup yields partial data (``null`` for the missing
    piece) at HTTP 200 — never a 5xx that would look like an app outage.
    """
    try:
        return await coro_factory()
    except Exception:  # noqa: BLE001 — best-effort stats: degrade to default
        return default


async def gather_stats() -> dict:
    """Assemble the stats payload from already-tracked counts/gauges."""
    now = datetime.now(tz=UTC)
    day_ago = now - timedelta(hours=24)

    bounds = await _safe(
        lambda: RadarFrame.objects.aaggregate(lo=Min("timestamp"), hi=Max("timestamp")),
        default={"lo": None, "hi": None},
    )
    earliest = bounds.get("lo")
    latest = bounds.get("hi")

    frames_total = await _safe(RadarFrame.objects.acount)

    # Per-provider archive breakdown for the About dialog. Best-effort like
    # the rest — a failing piece degrades to null, never a 5xx.
    providers_stats = []
    for name in enabled_providers():
        pb = await _safe(
            lambda n=name: RadarFrame.objects.filter(provider=n).aaggregate(
                lo=Min("timestamp"),
                hi=Max("timestamp"),
            ),
            default={"lo": None, "hi": None},
        )
        pframes = await _safe(lambda n=name: RadarFrame.objects.filter(provider=n).acount())
        providers_stats.append(
            {"name": name, "frames": pframes, "oldest": pb.get("lo"), "newest": pb.get("hi")},
        )

    archived_total = await _safe(LightningStrike.objects.acount)
    strikes_24h = await _safe(
        lambda: LightningStrike.objects.filter(struck_at__gte=day_ago).acount(),
    )
    ws_connected = await _safe(cache.get_ws_connected)
    last_strike = await _safe(lambda: cache.get_bytes(cache.LIGHTNING_LAST_STRIKE_KEY))
    storage_bytes = await _safe(lambda: cache.get_bytes(cache.STORAGE_GAUGE_KEY))

    return {
        "radar": {
            "frames_total": frames_total,
            "earliest": earliest,
            "latest": latest,
            "retention_days": settings.RETENTION_DAYS,
            # Per-provider archive counts; the About dialog lists these.
            "providers": providers_stats,
        },
        "lightning": {
            "enabled": settings.LIGHTNING_ENABLED,
            "archived_total": archived_total,
            "strikes_24h": strikes_24h,
            "ws_connected": bool(ws_connected) if ws_connected is not None else None,
            "last_strike": int(last_strike) if last_strike is not None else None,
        },
        "storage": {
            "bytes": int(storage_bytes) if storage_bytes is not None else None,
        },
        "live": {
            "last_frame": latest,
        },
        "generated_at": int(now.timestamp()),
    }
