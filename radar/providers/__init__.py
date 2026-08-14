"""Provider registry and selection.

Both radar providers can be live concurrently server-side.
``enabled_providers()`` is the source of truth for which are usable right now;
``get_provider`` refuses a name that is not enabled (fail fast).
``get_active_provider()`` returns the instance named by ``RADAR_PROVIDER`` — the
**default** provider used when a request carries no ``?provider=`` (default
``rainviewer``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from radar.providers.meteofrance import MeteoFranceProvider
from radar.providers.rainviewer import RainViewerProvider

if TYPE_CHECKING:
    from radar.providers.base import RadarProvider

_REGISTRY = {
    "rainviewer": RainViewerProvider,
    "meteofrance": MeteoFranceProvider,
}

_instances: dict[str, RadarProvider] = {}


def enabled_providers() -> list[str]:
    """Names of the providers usable right now, default provider first.

    RainViewer is always on; Météo-France joins only when ``METEOFRANCE_ENABLED``.
    The order is stable and drives the frames ``providers`` advert.
    """
    names = ["rainviewer"]
    if settings.METEOFRANCE_ENABLED:
        names.append("meteofrance")
    return names


def get_provider(name: str) -> RadarProvider:
    """Resolve an *enabled* provider by name.

    A name that is unknown or not currently enabled raises ``ImproperlyConfigured``
    — views translate an invalid ``?provider=`` to HTTP 400 before ever
    calling this. Instances are cached per name.
    """
    if name not in enabled_providers():
        valid = ", ".join(enabled_providers())
        msg = f"Radar provider {name!r} is not enabled. Enabled: {valid}."
        raise ImproperlyConfigured(msg)
    instance = _instances.get(name)
    if instance is None:
        instance = _REGISTRY[name]()
        _instances[name] = instance
    return instance


def get_active_provider() -> RadarProvider:
    """Return the default provider instance (``settings.RADAR_PROVIDER``)."""
    return get_provider(settings.RADAR_PROVIDER)
