"""Forecast serving contracts: the ``forecast`` advert, the tile route, Nginx and the SW.

Phase 3 of ``specs/forecast-nowcast.md``. The advert is additive to
``/api/radar/frames`` and is the only way the browser learns about a forecast, so its
shape and its staleness rules are pinned here; so is the rule that a broken forecast
never breaks the radar timeline. Tiles come from disk only — nothing here, or in the
view, can reach PIAF.
"""

from __future__ import annotations

import re
import time

import pytest
from django.conf import settings
from django.test import override_settings

from config.settings import base as base_settings
from radar import cache
from radar import storage
from radar.forecast import advert as advert_mod
from radar.models import RadarFrame

pytestmark = pytest.mark.django_db(transaction=True)

STEP = 300
Z, X, Y = 5, 16, 11  # inside the France tile matrix
FORECAST_ON = {"METEOFRANCE_ENABLED": True, "FORECAST_ENABLED": True}


@pytest.fixture(autouse=True)
def _roots(tmp_path):
    with override_settings(TILE_ROOT=str(tmp_path / "tiles"), FORECAST_ROOT=str(tmp_path / "fc")):
        yield


def _clock() -> tuple[int, int]:
    """``(now, newest observed frame)``, the observation on the 5-minute grid."""
    now = int(time.time())
    return now, now - now % STEP


def _record(run: int, *, leads: range = range(600, 4201, STEP)) -> dict:
    return {
        "run": run,
        "frames": [run + lead for lead in leads],
        "coverage_ts": run,
        "published_at": run + 545,
        "source": "piaf",
    }


async def _observe(*timestamps: int, provider: str = "meteofrance") -> None:
    for ts in timestamps:
        await RadarFrame.objects.acreate(
            timestamp=ts,
            provider=provider,
            tile_count=5,
            status="ok",
            missing=[],
        )


async def _forecast(async_client, query: str = "?provider=meteofrance") -> dict:
    resp = await async_client.get(f"/api/radar/frames{query}")
    assert resp.status_code == 200
    return resp.json()["forecast"]


def _valids(advert: dict) -> list[int]:
    return [f["timestamp"] for f in advert["frames"]]


# -- the advert -------------------------------------------------------------------


@override_settings(METEOFRANCE_ENABLED=True)
async def test_advert_is_present_and_empty_while_the_flag_is_off(async_client):
    _, obs = _clock()
    await _observe(obs)
    await cache.set_forecast_current(_record(obs - STEP))  # ignored: the flag wins
    assert await _forecast(async_client) == {
        "enabled": False,
        "provider": "meteofrance",
        "run": None,
        "frames": [],
        "attribution": advert_mod.PiafSource.attribution(),
        "tile_base": None,
    }


async def test_advert_present_on_the_default_provider_too(async_client):
    """Radar-only and RainViewer callers get the key as well; they ignore it."""
    _, obs = _clock()
    await _observe(obs, provider="rainviewer")
    forecast = await _forecast(async_client, "")
    assert forecast["enabled"] is False
    assert forecast["frames"] == []


@override_settings(**FORECAST_ON)
async def test_advert_shows_the_hour_after_the_newest_observation(async_client):
    """Newest observation R+5 (the usual case): frames R+10 … R+65, i.e. 60 minutes."""
    _, obs = _clock()
    run = obs - STEP
    await _observe(obs - STEP, obs)
    await cache.set_forecast_current(_record(run))
    forecast = await _forecast(async_client)
    assert forecast["enabled"] is True
    assert forecast["provider"] == "meteofrance"
    assert forecast["run"] == run
    assert forecast["tile_base"] == f"/forecast/{run}"
    assert "Météo-France" in forecast["attribution"]
    assert "Licence Ouverte" in forecast["attribution"]
    assert _valids(forecast) == list(range(obs + STEP, obs + 3600 + 1, STEP))
    assert len(forecast["frames"]) == 12


@override_settings(**FORECAST_ON)
async def test_advert_drops_frames_already_measured(async_client):
    """Newest observation R+10: R+10 is measured, so the forecast starts at R+15."""
    _, obs = _clock()
    run = obs - 2 * STEP
    await _observe(obs)
    await cache.set_forecast_current(_record(run))
    valids = _valids(await _forecast(async_client))
    assert valids[0] == obs + STEP
    assert valids[-1] == run + 4200  # the run's last lead, exactly the horizon
    assert len(valids) == 12


@override_settings(**FORECAST_ON, FORECAST_MAX_RUN_AGE=1200)
async def test_advert_drops_a_run_past_its_maximum_age(async_client):
    now, obs = _clock()
    run = now - 1201
    run -= run % STEP
    await _observe(obs)
    await cache.set_forecast_current(_record(run))
    forecast = await _forecast(async_client)
    assert forecast["enabled"] is True
    assert forecast["run"] is None
    assert forecast["frames"] == []
    assert forecast["tile_base"] is None


@override_settings(**FORECAST_ON)
async def test_advert_drops_a_forecast_that_would_leave_a_hole(async_client):
    """Observation stuck at R: the run starts at R+10, so R+5 would be a hole."""
    _, obs = _clock()
    await _observe(obs)
    await cache.set_forecast_current(_record(obs))
    assert (await _forecast(async_client))["frames"] == []


@override_settings(**FORECAST_ON)
async def test_advert_has_no_frames_for_another_provider(async_client):
    """The forecast extends the Météo-France observation only (§5 item 1)."""
    _, obs = _clock()
    await _observe(obs, provider="rainviewer")
    await _observe(obs)
    await cache.set_forecast_current(_record(obs - STEP))
    forecast = await _forecast(async_client, "?provider=rainviewer")
    assert forecast["enabled"] is True
    assert forecast["frames"] == []


@override_settings(**FORECAST_ON)
async def test_advert_has_no_frames_for_a_historical_range(async_client):
    _, obs = _clock()
    await _observe(obs)
    await cache.set_forecast_current(_record(obs - STEP))
    forecast = await _forecast(async_client, f"?provider=meteofrance&from={obs - 3600}&to={obs}")
    assert forecast["enabled"] is True
    assert forecast["frames"] == []


@override_settings(**FORECAST_ON)
async def test_advert_without_a_published_run(async_client):
    _, obs = _clock()
    await _observe(obs)
    assert (await _forecast(async_client))["frames"] == []


@override_settings(**FORECAST_ON)
async def test_a_broken_forecast_never_breaks_the_radar_timeline(async_client, monkeypatch):
    _, obs = _clock()
    await _observe(obs)

    async def _redis_down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(cache, "get_forecast_current", _redis_down)
    resp = await async_client.get("/api/radar/frames?provider=meteofrance")
    assert resp.status_code == 200
    body = resp.json()
    assert [f["timestamp"] for f in body["frames"]] == [obs]
    assert body["forecast"]["frames"] == []


@override_settings(**FORECAST_ON)
@pytest.mark.parametrize("record", [{"frames": []}, {"run": "x", "frames": []}, {"run": 1}])
async def test_a_mangled_pointer_is_no_forecast(async_client, record):
    _, obs = _clock()
    await _observe(obs)
    await cache.set_forecast_current(record)
    assert (await _forecast(async_client))["frames"] == []


@override_settings(FORECAST_MAX_RUN_AGE=1200, FORECAST_HORIZON_SECONDS=3600)
def test_advertised_frames_rules():
    run = 1_794_395_700
    record = _record(run)
    # Usual case, and the run at exactly its maximum age.
    assert len(advert_mod.advertised_frames(record, run + 300, run + 600)) == 12
    assert advert_mod.advertised_frames(record, run + 300, run + 1200) != []
    assert advert_mod.advertised_frames(record, run + 300, run + 1201) == []
    # A missed run: the observation moved on, the horizon shrinks but stays contiguous.
    assert advert_mod.advertised_frames(record, run + 900, run + 1100)[0] == run + 1200
    # The observation has caught up with the whole run.
    assert advert_mod.advertised_frames(record, run + 4200, run + 1000) == []


# -- the tile route ---------------------------------------------------------------


def _tile_url(run: int, valid: int, z: int = Z, x: int = X, y: int = Y) -> str:
    return f"/forecast/{run}/{valid}/{z}/{x}/{y}.png"


@override_settings(**FORECAST_ON)
async def test_tile_on_disk_is_served_immutable(async_client, png_bytes):
    storage.write_forecast_tile(100, 700, Z, X, Y, png_bytes)
    resp = await async_client.get(_tile_url(100, 700))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/png"
    assert resp["Cache-Control"] == "public, max-age=31536000, immutable"
    assert b"".join(resp.streaming_content) == png_bytes


@override_settings(**FORECAST_ON)
async def test_missing_tile_of_a_done_frame_is_an_immutable_204(async_client):
    storage.mark_forecast_frame_done(100, 700, 0)
    resp = await async_client.get(_tile_url(100, 700))
    assert resp.status_code == 204
    assert resp["Cache-Control"] == "public, max-age=31536000, immutable"


@override_settings(**FORECAST_ON)
async def test_unknown_or_unfinished_frame_is_an_uncached_404(async_client, png_bytes):
    # A frame still arriving: another tile is there, but no .done marker yet.
    storage.write_forecast_tile(100, 700, Z, X + 1, Y, png_bytes)
    for url in (_tile_url(100, 700), _tile_url(999, 1599)):
        resp = await async_client.get(url)
        assert resp.status_code == 404
        assert resp["Cache-Control"] == "no-store"


@override_settings(**FORECAST_ON)
async def test_tile_off_the_matrix_is_404(async_client):
    storage.mark_forecast_frame_done(100, 700, 0)
    assert (await async_client.get(_tile_url(100, 700, 5, 0, 0))).status_code == 404
    assert (await async_client.get(_tile_url(100, 700, 12, X, Y))).status_code == 404


@override_settings(METEOFRANCE_ENABLED=True, FORECAST_ENABLED=False)
async def test_tile_route_is_dark_while_the_flag_is_off(async_client, png_bytes):
    storage.write_forecast_tile(100, 700, Z, X, Y, png_bytes)
    assert (await async_client.get(_tile_url(100, 700))).status_code == 404


@override_settings(**FORECAST_ON)
async def test_tile_route_never_reaches_upstream(async_client, monkeypatch):
    """A miss is answered from disk: no HTTP client is ever built."""
    import httpx  # noqa: PLC0415

    def _boom(*_a, **_k):
        raise AssertionError("the forecast tile route must never fetch")

    monkeypatch.setattr(httpx.AsyncClient, "send", _boom)
    assert (await async_client.get(_tile_url(100, 700))).status_code == 404


@override_settings(**FORECAST_ON)
async def test_run_markers_are_not_reachable(async_client):
    storage.write_forecast_run(100, _record(100))
    assert (await async_client.get("/forecast/100/run.json")).status_code == 404


# -- Nginx and the service worker -------------------------------------------------


def test_nginx_serves_forecast_tiles_statically_from_forecast_root():
    conf = (settings.BASE_DIR / "compose" / "production" / "nginx" / "default.conf").read_text()
    block = re.search(
        r'location ~ "\^/forecast/\\d\+/\\d\+/\\d\+/\\d\+/\\d\+\\\.png\$" \{(.*?)\n  \}',
        conf,
        re.DOTALL,
    )
    assert block, "no strict /forecast/ tile location in the prod Nginx config"
    body = block.group(1)
    root = re.search(r"root (\S+);", body).group(1)
    # The URL maps 1:1 onto the shipped FORECAST_ROOT (this module overrides the
    # live setting), as /tiles/ maps onto TILE_ROOT.
    assert f"{root}/forecast" == base_settings.FORECAST_ROOT
    assert 'Cache-Control "public, max-age=31536000, immutable"' in body
    assert "try_files $uri @django_tiles;" in body
    # Declared before the terminal `location /`, which 404s everything else.
    assert conf.index("^/forecast/") < conf.index("location / {")


def test_service_worker_never_intercepts_forecast_tiles():
    sw = (settings.FRONTEND_DIR / "sw.js").read_text()
    paths = re.search(r"const DATA_PATHS = \[(.*?)\];", sw)
    assert paths, "DATA_PATHS not found in sw.js"
    assert re.findall(r'"([^"]+)"', paths.group(1)) == ["/api/", "/tiles/", "/forecast/"]
    assert "DATA_PATHS.some((prefix) => url.pathname.startsWith(prefix))) return;" in sw
