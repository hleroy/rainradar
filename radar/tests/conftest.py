"""Shared fixtures for the radar test suite."""

from __future__ import annotations

import contextlib

import pytest
from django.conf import settings

# Sample weather-maps.json shaped exactly like the live RainViewer response
#: host + radar.past (oldest->newest), nowcast/satellite ignored. The
# `path` is the opaque hex token the live API actually returns (e.g.
# /v2/radar/1a2b3c4d5e6f), NOT the epoch — keep it that way so the suite exercises
# the real shape (an epoch-only fixture once hid a frame-parsing regression).
SAMPLE_WEATHER_MAPS = {
    "version": "2.0",
    "generated": 1683797100,
    "host": "https://tilecache.rainviewer.com",
    "radar": {
        "past": [
            {"time": 1683790200, "path": "/v2/radar/1a2b3c4d5e6f"},
            {"time": 1683790800, "path": "/v2/radar/2b3c4d5e6f70"},
            {"time": 1683791400, "path": "/v2/radar/3c4d5e6f7081"},
        ],
        "nowcast": [{"time": 1683797400, "path": "/v2/radar/9f9e9d9c9b9a"}],
    },
    "satellite": {"infrared": []},
}

# 1x1 transparent PNG.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000154a24f0e0000000049454e44ae42"
    "6082",
)


@pytest.fixture
def sample_weather_maps() -> dict:
    return SAMPLE_WEATHER_MAPS


@pytest.fixture
def png_bytes() -> bytes:
    return PNG_BYTES


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset provider singletons, the Redis cache and the render caches between tests."""
    import redis as sync_redis  # noqa: PLC0415

    from radar import cache  # noqa: PLC0415
    from radar import providers  # noqa: PLC0415
    from radar.providers import meteofrance_render  # noqa: PLC0415
    from radar.providers import rainviewer  # noqa: PLC0415

    def _reset():
        providers._instances.clear()
        cache._client = None
        # Process-wide and keyed by GridGeo, so a test's synthetic grid would otherwise
        # leak index arrays into the next one. Every module that renders tiles needs
        # this, not just test_meteofrance_render.
        meteofrance_render._index_cache.clear()
        meteofrance_render._transformers.clear()
        # The 429 cooldown is a process-wide monotonic deadline: a test that trips it
        # would otherwise make every later upstream test fail fast.
        rainviewer.reset_cooldown()

    _reset()
    with contextlib.suppress(Exception):
        client = sync_redis.from_url(settings.REDIS_URL)
        client.flushdb()
        client.close()
    yield
    _reset()
