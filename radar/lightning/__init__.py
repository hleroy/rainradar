"""The isolated lightning subsystem + source registry.

A separate package, separate asyncio tasks, separate failure domain. **No radar
code imports lightning code and vice-versa**, except the shared seams
``radar.cache``, ``radar.logging_json``, ``radar.models.LightningStrike`` and
settings. A WS drop, parse error, queue overflow or DB hiccup anywhere in here
can never raise into the radar poll loop or scheduler.

``get_active_source()`` returns the source named by ``LIGHTNING_SOURCE``
(default ``blitzortung``), mirroring ``radar.providers.get_active_provider``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from radar.lightning.blitzortung import BlitzortungSource

if TYPE_CHECKING:
    from radar.lightning.base import LightningSource

_REGISTRY = {
    "blitzortung": BlitzortungSource,
}

_instances: dict[str, LightningSource] = {}


def get_source(name: str) -> LightningSource:
    """Resolve a lightning source by name. Unknown -> ImproperlyConfigured."""
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        valid = ", ".join(sorted(_REGISTRY))
        msg = f"Unknown LIGHTNING_SOURCE {name!r}. Valid choices: {valid}."
        raise ImproperlyConfigured(msg) from exc
    instance = _instances.get(name)
    if instance is None:
        instance = cls()
        _instances[name] = instance
    return instance


def get_active_source() -> LightningSource:
    """Return the source instance selected by ``settings.LIGHTNING_SOURCE``."""
    return get_source(settings.LIGHTNING_SOURCE)
