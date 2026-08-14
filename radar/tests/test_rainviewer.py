"""RainViewer provider — mocked HTTP."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx
from django.conf import settings

from radar.providers import rainviewer
from radar.providers.base import FramesUnavailable
from radar.providers.base import RateLimited
from radar.providers.base import TileUpstreamError
from radar.providers.rainviewer import RainViewerProvider

EXPECTED_TIMESTAMPS = [1683790200, 1683790800, 1683791400]


async def _noop(*_args, **_kwargs) -> None:
    """No-op replacement for asyncio.sleep so retry tests don't actually wait."""


@respx.mock
async def test_get_frames_parses_sample(sample_weather_maps):
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(200, json=sample_weather_maps),
    )
    provider = RainViewerProvider()

    frames = await provider.get_frames()

    assert [f.timestamp for f in frames] == EXPECTED_TIMESTAMPS  # oldest -> newest
    assert frames[0].ref == "/v2/radar/1a2b3c4d5e6f"
    assert provider._host == "https://tilecache.rainviewer.com"


@respx.mock
async def test_malicious_host_is_rejected(sample_weather_maps):
    # A tampered/compromised upstream must not steer our server-side fetch.
    tampered = {**sample_weather_maps, "host": "http://169.254.169.254"}
    respx.get(settings.RAINVIEWER_API_URL).mock(return_value=httpx.Response(200, json=tampered))
    provider = RainViewerProvider()

    await provider.get_frames()

    assert provider._host == settings.RAINVIEWER_TILE_HOST  # fell back, not the attacker host


@respx.mock
async def test_non_https_host_is_rejected(sample_weather_maps):
    tampered = {**sample_weather_maps, "host": "http://tilecache.rainviewer.com"}
    respx.get(settings.RAINVIEWER_API_URL).mock(return_value=httpx.Response(200, json=tampered))
    provider = RainViewerProvider()

    await provider.get_frames()

    assert provider._host == settings.RAINVIEWER_TILE_HOST  # http:// dropped


@respx.mock
async def test_hex_token_frame_path_is_accepted():
    # RainViewer's live API returns an opaque hex token, not the epoch — these are
    # legitimate and must be kept (regression: an epoch-only regex blanked the map).
    real = {
        "host": "https://tilecache.rainviewer.com",
        "radar": {
            "past": [
                {"time": 1782154200, "path": "/v2/radar/0f678d99e485"},
                {"time": 1782154800, "path": "/v2/radar/98027abb2d3f"},
            ],
        },
    }
    respx.get(settings.RAINVIEWER_API_URL).mock(return_value=httpx.Response(200, json=real))
    provider = RainViewerProvider()

    frames = await provider.get_frames()

    assert [f.timestamp for f in frames] == [1782154200, 1782154800]
    assert frames[0].ref == "/v2/radar/0f678d99e485"


@respx.mock
async def test_malformed_frame_path_is_skipped():
    tampered = {
        "host": "https://tilecache.rainviewer.com",
        "radar": {
            "past": [
                {"time": 1683790200, "path": "/v2/radar/1683790200"},
                {"time": 1683790800, "path": "/../../etc/passwd"},
            ],
        },
    }
    respx.get(settings.RAINVIEWER_API_URL).mock(return_value=httpx.Response(200, json=tampered))
    provider = RainViewerProvider()

    frames = await provider.get_frames()

    assert [f.timestamp for f in frames] == [1683790200]  # bad path dropped


def test_tile_url_is_exact():
    provider = RainViewerProvider()
    provider._host = "https://tilecache.rainviewer.com"
    url = provider.tile_url(1683790200, 5, 16, 11)
    assert url == "https://tilecache.rainviewer.com/v2/radar/1683790200/256/5/16/11/2/1_1.png"


@respx.mock
async def test_get_tile_returns_bytes(png_bytes):
    provider = RainViewerProvider()
    url = provider.tile_url(1683790200, 5, 16, 11)
    respx.get(url).mock(return_value=httpx.Response(200, content=png_bytes))

    assert await provider.get_tile(1683790200, 5, 16, 11) == png_bytes


@respx.mock
async def test_get_tile_404_returns_none():
    provider = RainViewerProvider()
    url = provider.tile_url(1683790200, 5, 16, 11)
    respx.get(url).mock(return_value=httpx.Response(404))

    assert await provider.get_tile(1683790200, 5, 16, 11) is None


@respx.mock
async def test_frames_retry_on_5xx(monkeypatch):
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    route = respx.get(settings.RAINVIEWER_API_URL).mock(return_value=httpx.Response(500))
    provider = RainViewerProvider()

    with pytest.raises(FramesUnavailable):
        await provider.get_frames()

    assert route.call_count == 4  # 1 initial + 3 retries


@respx.mock
async def test_frames_read_timeout_handled(monkeypatch):
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    route = respx.get(settings.RAINVIEWER_API_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    provider = RainViewerProvider()

    with pytest.raises(FramesUnavailable):
        await provider.get_frames()

    assert route.call_count == 4


@respx.mock
async def test_tile_retry_on_5xx(monkeypatch):
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    provider = RainViewerProvider()
    url = provider.tile_url(1683790200, 5, 16, 11)
    route = respx.get(url).mock(return_value=httpx.Response(500))

    with pytest.raises(TileUpstreamError):
        await provider.get_tile(1683790200, 5, 16, 11)

    assert route.call_count == 3  # 1 initial + 2 retries


@respx.mock
async def test_tile_429_does_not_retry(monkeypatch):
    # Retrying into a rate limit is what deepens it. One request, then RateLimited.
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    provider = RainViewerProvider()
    url = provider.tile_url(1683790200, 5, 16, 11)
    route = respx.get(url).mock(return_value=httpx.Response(429))

    with pytest.raises(RateLimited):
        await provider.get_tile(1683790200, 5, 16, 11)

    assert route.call_count == 1  # no retry budget spent on a throttle


@respx.mock
async def test_429_cooldown_blocks_later_tiles_without_upstream_calls(monkeypatch):
    # The load-bearing anti-hammering property: once upstream says 429, the whole
    # process stops calling it until the cooldown lapses — the on-demand view path
    # and the archiver's remaining tiles alike.
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    provider = RainViewerProvider()
    route = respx.get(url__regex=r"https://tilecache\.rainviewer\.com/.+\.png$").mock(
        return_value=httpx.Response(429),
    )

    with pytest.raises(RateLimited):
        await provider.get_tile(1683790200, 5, 16, 11)
    for n in range(20):
        with pytest.raises(RateLimited):
            await provider.get_tile(1683790200, 5, 16, n)

    assert route.call_count == 1  # 20 further tiles, zero further upstream requests
    assert rainviewer.cooldown_remaining() > 0


@respx.mock
async def test_429_honours_retry_after_in_full(monkeypatch):
    # Unlike an in-request sleep, the cooldown refuses rather than waits — so a long
    # Retry-After costs no held connection and is honoured as sent, not clamped to a
    # few seconds. Only the hard ceiling applies.
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    provider = RainViewerProvider()
    url = provider.tile_url(1683790200, 5, 16, 11)
    respx.get(url).mock(return_value=httpx.Response(429, headers={"Retry-After": "120"}))

    with pytest.raises(RateLimited) as excinfo:
        await provider.get_tile(1683790200, 5, 16, 11)

    assert excinfo.value.retry_after == 120.0
    assert rainviewer.cooldown_remaining() > 100  # not clamped down to seconds


@respx.mock
async def test_429_cooldown_is_capped(monkeypatch):
    # ...but a hostile or buggy Retry-After can't park us for hours.
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    provider = RainViewerProvider()
    url = provider.tile_url(1683790200, 5, 16, 11)
    respx.get(url).mock(return_value=httpx.Response(429, headers={"Retry-After": "99999"}))

    with pytest.raises(RateLimited):
        await provider.get_tile(1683790200, 5, 16, 11)

    assert rainviewer.cooldown_remaining() <= settings.UPSTREAM_RATE_LIMIT_COOLDOWN_MAX


@respx.mock
async def test_frames_429_is_a_failure_not_a_body(monkeypatch):
    # A 429 body is an error page. Returning it as frames JSON would blow up in
    # _parse_frames with an exception poll_radar does not catch.
    monkeypatch.setattr(rainviewer.asyncio, "sleep", _noop)
    respx.get(settings.RAINVIEWER_API_URL).mock(
        return_value=httpx.Response(429, text="rate limit exceeded"),
    )
    provider = RainViewerProvider()

    with pytest.raises(FramesUnavailable):
        await provider.get_frames()

    assert rainviewer.cooldown_remaining() > 0


def test_tile_backoff_uses_standard_progression():
    delay = rainviewer._tile_backoff(0)
    assert (
        rainviewer._TILE_BACKOFFS[0]
        <= delay
        <= (rainviewer._TILE_BACKOFFS[0] + rainviewer._BACKOFF_JITTER)
    )


async def test_upstream_requests_are_paced(monkeypatch):
    # Concurrency caps simultaneity, not rate: four slots recycled over keep-alive
    # still emit hundreds of requests a minute. This is the actual rate ceiling.
    monkeypatch.setattr(settings, "UPSTREAM_TILE_MIN_INTERVAL", 0.02)
    monkeypatch.setattr(settings, "UPSTREAM_TILE_CONCURRENCY", 8)
    monkeypatch.setattr(rainviewer, "_gate", None)
    monkeypatch.setattr(rainviewer, "_gate_loop", None)

    starts: list[float] = []

    class _Probe:
        async def get(self, _url):
            starts.append(time.monotonic())
            return httpx.Response(200, content=b"png")

    provider = RainViewerProvider()
    probe = _Probe()
    await asyncio.gather(
        *(provider.get_tile(1683790200, 5, 16, n, client=probe) for n in range(10)),
    )

    assert len(starts) == 10
    # 10 requests spaced 0.02s apart cannot complete faster than ~9 intervals, no
    # matter that all 10 were free to run concurrently.
    assert starts[-1] - starts[0] >= 0.02 * 9 * 0.8


async def test_upstream_concurrency_is_capped(monkeypatch):
    # The process-wide gate must bound simultaneous upstream fetches regardless of
    # how many tiles are requested at once (the cold-cache burst that 429s us).
    monkeypatch.setattr(settings, "UPSTREAM_TILE_CONCURRENCY", 2)
    monkeypatch.setattr(rainviewer, "_gate", None)
    monkeypatch.setattr(rainviewer, "_gate_loop", None)

    state = {"current": 0, "peak": 0}

    class _Probe:
        async def get(self, _url):
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            await asyncio.sleep(0.02)  # overlap window for would-be concurrent fetches
            state["current"] -= 1
            return httpx.Response(200, content=b"png")

    provider = RainViewerProvider()
    probe = _Probe()
    await asyncio.gather(
        *(provider.get_tile(1683790200, 5, 16, n, client=probe) for n in range(8)),
    )

    assert state["peak"] <= 2
