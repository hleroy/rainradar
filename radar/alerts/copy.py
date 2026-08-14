"""Localized push notification text.

Single source of truth for notification copy: the **frontend i18n files**
(``frontend/i18n/{fr,en}.json``, present in both containers since they share the
image). The server composes the final strings and the ``sw.js`` ``push`` handler
shows them verbatim, so foreground (``alerts.js``) and background notifications
read identically. Loaded once and validated at first use — a missing key raises
loudly rather than shipping a broken notification.
"""

from __future__ import annotations

import json
from functools import lru_cache

from django.conf import settings

_LOCALES = ("en", "fr")
# Keys every locale must define for a notification to render.
_REQUIRED = (
    "alert.notify.outer.title",
    "alert.notify.inner.title",
    "alert.notify.body",
    *(f"alert.dir.{d}" for d in ("n", "ne", "e", "se", "s", "sw", "w", "nw")),
)


@lru_cache(maxsize=1)
def _dicts() -> dict[str, dict[str, str]]:
    """Load + validate both locale dicts once (cached for the process)."""
    out: dict[str, dict[str, str]] = {}
    for loc in _LOCALES:
        path = settings.FRONTEND_DIR / "i18n" / f"{loc}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        missing = [k for k in _REQUIRED if k not in data]
        if missing:
            msg = f"i18n {loc}.json missing push-alert keys: {missing}"
            raise KeyError(msg)
        out[loc] = data
    return out


def render(locale: str, tier: str, dist_km: float, dir8: str) -> dict[str, str]:
    """Compose one notification's ``{title, body}`` in ``locale`` (falls back to en).

    Mirrors ``alerts.js``: whole-km distance (min 1) and the localized 8-wind
    direction word substituted into the shared body template.
    """
    d = _dicts()
    strings = d.get(locale) or d["en"]
    dist = max(1, round(dist_km))
    body = (
        strings["alert.notify.body"]
        .replace("{dist}", str(dist))
        .replace("{dir}", strings[f"alert.dir.{dir8}"])
    )
    return {"title": strings[f"alert.notify.{tier}.title"], "body": body}
