"""Provider registry & selection."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from radar import providers
from radar.providers.meteofrance import MeteoFranceProvider
from radar.providers.rainviewer import RainViewerProvider


def test_registry_resolves_rainviewer():
    provider = providers.get_provider("rainviewer")
    assert isinstance(provider, RainViewerProvider)
    assert provider.name == "rainviewer"


def test_get_active_provider_defaults_to_rainviewer():
    with override_settings(RADAR_PROVIDER="rainviewer"):
        assert isinstance(providers.get_active_provider(), RainViewerProvider)


def test_provider_instance_is_cached():
    assert providers.get_provider("rainviewer") is providers.get_provider("rainviewer")


def test_unknown_provider_name_errors():
    with pytest.raises(ImproperlyConfigured):
        providers.get_provider("nope")


# -- enabled set --------------------------------------------------------------


def test_enabled_providers_single_when_meteofrance_off():
    with override_settings(METEOFRANCE_ENABLED=False):
        assert providers.enabled_providers() == ["rainviewer"]


def test_enabled_providers_includes_meteofrance_when_on():
    with override_settings(METEOFRANCE_ENABLED=True):
        assert providers.enabled_providers() == ["rainviewer", "meteofrance"]


def test_meteofrance_disabled_is_improperly_configured():
    # Not enabled ⇒ fail fast (not usable), never a silent fallback.
    with override_settings(METEOFRANCE_ENABLED=False), pytest.raises(ImproperlyConfigured):
        providers.get_provider("meteofrance")


def test_meteofrance_resolves_when_enabled():
    with override_settings(METEOFRANCE_ENABLED=True):
        provider = providers.get_provider("meteofrance")
        assert isinstance(provider, MeteoFranceProvider)
        assert provider.name == "meteofrance"


# -- frame_interval -----------------------------------------------------------


def test_rainviewer_frame_interval_is_frame_interval_setting():
    assert providers.get_provider("rainviewer").frame_interval == settings.FRAME_INTERVAL


def test_meteofrance_frame_interval_is_its_own_setting():
    with override_settings(METEOFRANCE_ENABLED=True, METEOFRANCE_FRAME_INTERVAL=300):
        assert providers.get_provider("meteofrance").frame_interval == 300
