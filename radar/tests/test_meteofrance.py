"""Météo-France provider — auth, frames, single-flight tiles.

Upstream HTTP mocked with respx; no real network. This file covers the OAuth2
token cache; render-pipeline tests live in test_meteofrance_render.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime

import httpx
import numpy as np
import pytest
import respx
from django.conf import settings
from django.test import override_settings

from radar import tiles
from radar.providers import meteofrance as mf_module
from radar.providers import meteofrance_auth
from radar.providers.base import FramesUnavailable
from radar.providers.meteofrance import MeteoFranceProvider
from radar.providers.meteofrance_auth import AuthError
from radar.providers.meteofrance_auth import MeteoFranceAuth
from radar.tests.test_bufr_decode import FIXTURE as BUFR_FIXTURE
from radar.tests.test_meteofrance_render import RAW_3MMH
from radar.tests.test_meteofrance_render import build_odim
from radar.tests.test_meteofrance_render import rain_h5

pytestmark = pytest.mark.asyncio

TOKEN_URL = "https://portail-api.meteofrance.fr/token"
API_BASE = "https://public-api.meteofrance.fr/public/DPRadar/v1"
DESC_URL = f"{API_BASE}/mosaiques/METROPOLE/observations/LAME_D_EAU"
PRODUCT_URL = f"{DESC_URL}/produit?maille=500"
VALIDITY = "2026-07-19T11:35:00Z"
VALIDITY_TS = int(datetime(2026, 7, 19, 11, 35, tzinfo=UTC).timestamp())

_MF = override_settings(
    METEOFRANCE_ENABLED=True,
    METEOFRANCE_APPLICATION_ID="dGVzdDpibG9i",  # base64("test:blob") — never asserted/logged
    METEOFRANCE_TOKEN_URL=TOKEN_URL,
    METEOFRANCE_API_BASE_URL=API_BASE,
    METEOFRANCE_ZONE="METROPOLE",
    METEOFRANCE_OBSERVATION="LAME_D_EAU",
    METEOFRANCE_MAILLE=500,
)


def _desc_json(validity=VALIDITY, product_href=None, observation="LAME_D_EAU", maille=500):
    """A DPRadar observation catalog. Both products share this shape, hence the params."""
    produit = f"/mosaiques/METROPOLE/observations/{observation}/produit"
    href = product_href or f"{produit}?maille={maille}"
    decoy = 1000 if maille != 1000 else 500  # a wrong-maille link — must be skipped
    return {
        "title": observation,
        "attribution": "Source : Météo-France",
        "links": [
            {"href": f"{produit}?maille={decoy}", "validity_time": validity},
            {"href": href, "rel": "item", "validity_time": validity},
        ],
    }


def _matrix():
    return sorted(
        tiles.tile_matrix(
            tuple(settings.RADAR_BBOX),
            settings.RADAR_ZOOM_MIN,
            settings.RADAR_ZOOM_MAX,
        ),
    )


async def _noop(*_a, **_k) -> None:
    pass


def _token_route(token="tok-abc", expires_in=3600, status=200):
    body = {"access_token": token, "token_type": "Bearer", "expires_in": expires_in}
    return respx.post(TOKEN_URL).mock(return_value=httpx.Response(status, json=body))


@_MF
@respx.mock
async def test_token_cached_until_expiry():
    route = _token_route(token="tok-1")
    auth = MeteoFranceAuth()
    assert await auth.get_token() == "tok-1"
    assert await auth.get_token() == "tok-1"
    assert route.call_count == 1  # second call served from cache


@_MF
@respx.mock
async def test_expiry_triggers_refresh():
    # TTL below the 60 s safety margin ⇒ the token is never considered valid ⇒
    # every call refreshes (proves the refresh-before-expiry path).
    route = _token_route(expires_in=30)
    auth = MeteoFranceAuth()
    await auth.get_token()
    await auth.get_token()
    assert route.call_count == 2


@_MF
@respx.mock
async def test_single_flight_under_concurrency():
    """N concurrent get_token() on a cold cache ⇒ exactly one token POST."""
    route = _token_route()
    auth = MeteoFranceAuth()
    tokens = await asyncio.gather(*(auth.get_token() for _ in range(32)))
    assert set(tokens) == {"tok-abc"}
    assert route.call_count == 1


@_MF
@respx.mock
async def test_invalidate_forces_refresh():
    route = _token_route()
    auth = MeteoFranceAuth()
    await auth.get_token()
    await auth.invalidate()
    await auth.get_token()
    assert route.call_count == 2


@_MF
@respx.mock
async def test_exhaustion_raises_auth_error(monkeypatch):
    monkeypatch.setattr(meteofrance_auth.asyncio, "sleep", _noop)
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(500))
    auth = MeteoFranceAuth()
    with pytest.raises(AuthError):
        await auth.get_token()
    assert route.call_count == len(meteofrance_auth._BACKOFFS) + 1  # all attempts used


@_MF
@respx.mock
async def test_client_error_fails_fast_without_retry(monkeypatch):
    # A 401 (bad/expired credentials) won't fix on retry — one attempt, then AuthError.
    monkeypatch.setattr(meteofrance_auth.asyncio, "sleep", _noop)
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))
    auth = MeteoFranceAuth()
    with pytest.raises(AuthError):
        await auth.get_token()
    assert route.call_count == 1


@_MF
@respx.mock
async def test_malformed_token_body_raises_auth_error():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"no_token": True}))
    auth = MeteoFranceAuth()
    with pytest.raises(AuthError):
        await auth.get_token()


# -- frames: latest-only description parse -------------------------------------


@_MF
@respx.mock
async def test_get_frames_returns_single_frame():
    _token_route()
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, json=_desc_json()))
    provider = MeteoFranceProvider()
    frames = await provider.get_frames()
    assert len(frames) == 1
    assert frames[0].timestamp == VALIDITY_TS
    assert frames[0].ref == PRODUCT_URL  # absolute, validated product URL


@_MF
@respx.mock
async def test_get_frames_rejects_foreign_href_ssrf():
    _token_route()
    evil = "http://evil.example.com/mosaiques/produit?maille=500"
    respx.get(DESC_URL).mock(
        return_value=httpx.Response(200, json=_desc_json(product_href=evil)),
    )
    provider = MeteoFranceProvider()
    # The only maille=500 link was outside the API base ⇒ SSRF-dropped, leaving no
    # usable product. An unusable catalog surfaces as FramesUnavailable (not a silent
    # []), so the poll counts a failure instead of archiving nothing.
    with pytest.raises(FramesUnavailable):
        await provider.get_frames()
    assert provider._product_ref is None  # the foreign href was never adopted


@_MF
@respx.mock
async def test_get_frames_no_product_link_raises_frames_unavailable():
    """A 200 catalog with no maille=500 product link ⇒ FramesUnavailable (schema drift).

    Regression for the "healthy poll, archives nothing" blind spot: without the raise,
    poll_radar would reset the failure counter and open no gap on a sustained outage.
    """
    _token_route()
    no_product = {
        "title": "LAME_D_EAU",
        "links": [
            # Only a wrong-maille link is present — nothing matches maille=500.
            {
                "href": "/mosaiques/METROPOLE/observations/LAME_D_EAU/produit?maille=1000",
                "validity_time": VALIDITY,
            },
        ],
    }
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, json=no_product))
    provider = MeteoFranceProvider()
    with pytest.raises(FramesUnavailable):
        await provider.get_frames()


@_MF
@respx.mock
async def test_get_frames_unparseable_body_raises_frames_unavailable():
    """A 200 catalog whose body is not valid JSON ⇒ FramesUnavailable, not a silent []."""
    _token_route()
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, content=b"<html>oops"))
    provider = MeteoFranceProvider()
    with pytest.raises(FramesUnavailable):
        await provider.get_frames()


@_MF
@respx.mock
async def test_get_frames_401_then_refresh_and_succeed():
    _token_route()
    route = respx.get(DESC_URL).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=_desc_json()),
        ],
    )
    provider = MeteoFranceProvider()
    frames = await provider.get_frames()
    assert len(frames) == 1
    assert route.call_count == 2  # 401 → invalidate → one forced retry


@_MF
@respx.mock
async def test_get_frames_exhaustion_raises_frames_unavailable(monkeypatch):
    monkeypatch.setattr(meteofrance_auth.asyncio, "sleep", _noop)
    from radar.providers import meteofrance as mf_mod  # noqa: PLC0415

    monkeypatch.setattr(mf_mod.asyncio, "sleep", _noop)
    _token_route()
    respx.get(DESC_URL).mock(return_value=httpx.Response(500))
    provider = MeteoFranceProvider()
    with pytest.raises(FramesUnavailable):
        await provider.get_frames()


# -- tiles: single-flight render (load-bearing) -------------------------------


@_MF
@respx.mock
async def test_62_concurrent_get_tile_triggers_one_download():
    """The load-bearing regression: 62 concurrent get_tile ⇒ one product download."""
    _token_route()
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, json=_desc_json()))
    data = np.full((256, 256), RAW_3MMH, dtype=np.uint16)  # all-rain grid over France
    h5_bytes, _ = build_odim(data=data)
    product = respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, content=h5_bytes))

    provider = MeteoFranceProvider()
    frames = await provider.get_frames()
    ts = frames[0].timestamp
    results = await asyncio.gather(*(provider.get_tile(ts, z, x, y) for (z, x, y) in _matrix()))

    assert product.call_count == 1  # single-flight: one download for all 62 tiles
    assert len(results) == 62
    # Some France tiles overlap the rain grid and render to real PNG bytes.
    painted = [r for r in results if r is not None]
    assert painted, "expected at least one non-empty tile over the all-rain grid"
    assert all(r.startswith(b"\x89PNG") for r in painted)


@_MF
@respx.mock
async def test_get_tile_none_for_non_latest_ts():
    _token_route()
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, json=_desc_json()))
    product = respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, content=build_odim()[0]))
    provider = MeteoFranceProvider()
    frames = await provider.get_frames()
    stale = frames[0].timestamp - settings.METEOFRANCE_FRAME_INTERVAL
    assert await provider.get_tile(stale, 5, 16, 11) is None  # aged-out ⇒ no fetch
    assert product.call_count == 0


@_MF
@respx.mock
async def test_get_tile_download_failure_raises_tile_upstream_error(monkeypatch):
    from radar.providers import meteofrance as mf_mod  # noqa: PLC0415

    monkeypatch.setattr(mf_mod.asyncio, "sleep", _noop)
    _token_route()
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, json=_desc_json()))
    respx.get(PRODUCT_URL).mock(return_value=httpx.Response(500))
    provider = MeteoFranceProvider()
    frames = await provider.get_frames()
    from radar.providers.base import TileUpstreamError  # noqa: PLC0415

    with pytest.raises(TileUpstreamError):
        await provider.get_tile(frames[0].timestamp, 5, 16, 11)


@_MF
@override_settings(METEOFRANCE_FAILURE_COOLDOWN=0)
@respx.mock
async def test_failed_frame_is_retried_once_the_cooldown_lapses(monkeypatch):
    """A memoized failure must not poison the frame: a later call re-fetches.

    Cooldown zeroed to stand in for "the next poll, a minute later" — the frame is
    still upstream's latest, and a transient outage must be recoverable.
    """
    from radar.providers import meteofrance as mf_mod  # noqa: PLC0415
    from radar.providers.base import TileUpstreamError  # noqa: PLC0415

    monkeypatch.setattr(mf_mod.asyncio, "sleep", _noop)
    _token_route()
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, json=_desc_json()))
    data = np.full((256, 256), RAW_3MMH, dtype=np.uint16)  # all-rain grid over France
    h5_bytes, _ = build_odim(data=data)
    # First frame download exhausts on 500s; the second attempt succeeds.
    product = respx.get(PRODUCT_URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, content=h5_bytes),
        ],
    )
    provider = MeteoFranceProvider()
    frames = await provider.get_frames()
    ts = frames[0].timestamp

    with pytest.raises(TileUpstreamError):
        await provider.get_tile(ts, 5, 16, 11)
    assert ts not in provider._tasks  # failed task evicted, not memoized

    # Same ts retries (does not re-raise the cached failure) and renders.
    tile = await provider.get_tile(ts, 5, 16, 11)
    assert tile is not None
    assert tile.startswith(b"\x89PNG")
    assert product.call_count == 4  # 3 failed retries + 1 successful re-fetch


@_MF
@respx.mock
async def test_failure_inside_the_cooldown_does_not_refetch(monkeypatch):
    """Callers arriving just after a failure share it instead of re-downloading.

    The archiver admits a frame's 62 tiles through a semaphore, so they reach the
    memo in waves. Evicting a failed task immediately made every wave start a fresh
    download with its own retry budget; the cooldown collapses them onto one outcome.
    """
    from radar.providers import meteofrance as mf_mod  # noqa: PLC0415
    from radar.providers.base import TileUpstreamError  # noqa: PLC0415

    monkeypatch.setattr(mf_mod.asyncio, "sleep", _noop)
    _token_route()
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, json=_desc_json()))
    product = respx.get(PRODUCT_URL).mock(return_value=httpx.Response(500))

    provider = MeteoFranceProvider()
    frames = await provider.get_frames()
    ts = frames[0].timestamp

    with pytest.raises(TileUpstreamError):
        await provider.get_tile(ts, 5, 16, 11)
    after_first = product.call_count
    assert after_first == 3  # one full retry budget

    # Ten later waves, all inside the cooldown: no further upstream traffic at all.
    for n in range(10):
        with pytest.raises(TileUpstreamError):
            await provider.get_tile(ts, 5, 16, n)

    assert product.call_count == after_first


# -- the REFLECTIVITE wash -----------------------------------------------------
#
# The wash is a *second* product fetched per frame. These cover the invariant that
# changes (one download becomes two), and — more importantly — that the wash can fail
# in every way it likes without costing us the rain frame.

REFL_DESC_URL = f"{API_BASE}/mosaiques/METROPOLE/observations/REFLECTIVITE"
REFL_PRODUCT_URL = f"{REFL_DESC_URL}/produit?maille=1000"

# The pinned BUFR fixture's own section-1 nominal time (see test_bufr_decode). The wash
# tests advertise it as *both* catalogs' validity_time, because the provider now refuses
# to composite a mosaic that describes a different instant from the rain frame.
WASH_VALIDITY = "2026-07-20T19:15:00Z"
# 75 minutes later — far enough past METEOFRANCE_REFLECTIVITY_MAX_SKEW that pairing the
# fixture with a frame at this instant is unambiguously stale, not a one-frame wobble.
LATER_VALIDITY = "2026-07-20T20:30:00Z"

# The wash settings are the rain settings plus three, so inherit rather than retype.
_MF_WASH = override_settings(
    **_MF.options,
    METEOFRANCE_REFLECTIVITY_ENABLED=True,
    METEOFRANCE_REFLECTIVITY_OBSERVATION="REFLECTIVITE",
    METEOFRANCE_REFLECTIVITY_MAILLE=1000,
)


def _refl_desc_json(validity=WASH_VALIDITY):
    """The REFLECTIVITE catalog — same shape as the rain one, published at maille=1000."""
    return _desc_json(validity, observation="REFLECTIVITE", maille=1000)


def _bufr_fixture() -> bytes:
    return BUFR_FIXTURE.read_bytes()


async def _render_all(provider) -> list:
    frames = await provider.get_frames()
    ts = frames[0].timestamp
    return await asyncio.gather(*(provider.get_tile(ts, z, x, y) for (z, x, y) in _matrix()))


@pytest.fixture
def no_backoff(monkeypatch):
    """Zero the retry sleeps, keeping the attempt counts.

    The wash tests exercise retry-exhausting failure paths; left alone they spend ~8 s
    of the suite in real ``asyncio.sleep``. The tuples' *lengths* set the attempt counts,
    so only the values are zeroed.
    """
    monkeypatch.setattr(mf_module, "_CATALOG_BACKOFFS", (0.0,) * len(mf_module._CATALOG_BACKOFFS))
    monkeypatch.setattr(mf_module, "_PRODUCT_BACKOFFS", (0.0,) * len(mf_module._PRODUCT_BACKOFFS))
    monkeypatch.setattr(mf_module, "_BACKOFF_JITTER", 0.0)


def _rain_routes(validity=WASH_VALIDITY):
    """LAME_D_EAU catalog + rain product, both healthy, at one validity.

    Returns the product route. The default validity is the pinned BUFR fixture's own
    instant, so rain and wash describe one moment unless a test deliberately skews them.
    The token route is left to the caller — only one test asserts on its call count, and
    registering it twice would make that assertion ambiguous.
    """
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, json=_desc_json(validity=validity)))
    return respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, content=rain_h5()))


async def _rain_only_baseline() -> list:
    """The rain-only render of the same frame — what every wash failure falls back to."""
    with override_settings(METEOFRANCE_REFLECTIVITY_ENABLED=False):
        return await _render_all(MeteoFranceProvider())


@_MF_WASH
@respx.mock
async def test_62_concurrent_get_tile_triggers_exactly_two_downloads():
    """The one-download invariant becomes two — one per product, never per tile.

    Both arms sit inside the same single-flight memo, so 62 concurrent tile requests
    still trigger one fetch of each product rather than 62 of either.
    """
    _token_route()
    rain = _rain_routes()
    respx.get(REFL_DESC_URL).mock(return_value=httpx.Response(200, json=_refl_desc_json()))
    wash = respx.get(REFL_PRODUCT_URL).mock(
        return_value=httpx.Response(200, content=_bufr_fixture())
    )

    results = await _render_all(MeteoFranceProvider())

    assert rain.call_count == 1
    assert wash.call_count == 1
    assert len(results) == 62
    painted = [r for r in results if r is not None]
    assert painted
    assert all(r.startswith(b"\x89PNG") for r in painted)
    # The wash must actually have reached the pixels. Without this the test would keep
    # passing if every skew/deadline guard below started silently dropping it.
    assert results != await _rain_only_baseline()


@_MF
@respx.mock
async def test_reflectivity_disabled_never_touches_the_second_catalog():
    """Flag off ⇒ rain only: one download, and the wash catalog is never called."""
    _token_route()
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, json=_desc_json()))
    rain = respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, content=rain_h5()))
    refl = respx.get(REFL_DESC_URL).mock(return_value=httpx.Response(200, json=_refl_desc_json()))

    results = await _render_all(MeteoFranceProvider())

    assert rain.call_count == 1
    assert refl.call_count == 0
    assert len(results) == 62


@_MF_WASH
@respx.mock
@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param("catalog", id="reflectivity-catalog-500"),
        pytest.param("product", id="reflectivity-product-500"),
        pytest.param("garbage", id="reflectivity-payload-undecodable"),
    ],
)
async def test_reflectivity_failure_degrades_to_rain_only(break_it, no_backoff):
    """Every way the wash can fail must leave the rain frame intact.

    Rain is the accurate product and the wash only an atmospheric hint, so a
    reflectivity outage, a 4xx, or an unparseable payload must all produce exactly the
    rain-only tiles rather than raising and losing the frame.
    """
    _token_route()
    rain = _rain_routes()

    if break_it == "catalog":
        respx.get(REFL_DESC_URL).mock(return_value=httpx.Response(500))
    else:
        respx.get(REFL_DESC_URL).mock(return_value=httpx.Response(200, json=_refl_desc_json()))
        respx.get(REFL_PRODUCT_URL).mock(
            httpx.Response(500)
            if break_it == "product"
            else httpx.Response(200, content=b"not a bufr message at all")
        )

    with_wash = await _render_all(MeteoFranceProvider())
    baseline = await _rain_only_baseline()

    # Exactly one rain download per provider instance — a wash failure must not make
    # the rain arm retry, which `>= 1` would have let through.
    assert rain.call_count == 2
    assert with_wash == baseline


@_MF_WASH
@respx.mock
@pytest.mark.parametrize(
    ("catalog_validity", "expect_product_fetch"),
    [
        # Upstream stalled: the catalog itself stops advancing. Caught before the
        # download, so we don't even pay for the 1 MB body.
        pytest.param("2026-07-20T18:00:00Z", False, id="stale-catalog"),
        # Upstream lies: the catalog advertises the rain frame's instant but serves a
        # mosaic whose own nominal time is 75 minutes off. Only the payload check sees it.
        pytest.param(LATER_VALIDITY, True, id="stale-payload"),
    ],
)
async def test_reflectivity_time_skew_drops_the_wash(catalog_validity, expect_product_fetch):
    """A mosaic from another instant must never be composited into the archive.

    The two layers are flattened into one tile, so a stale wash cannot be separated out
    afterwards — it would sit in the 90-day archive as if it were current weather. Both
    the catalog's claim and the message's own section-1 nominal time are checked.

    The rain frame is at 20:30 while the pinned BUFR fixture's nominal time is 19:15, so
    these cases skew the *frame* rather than the fixture.
    """
    _token_route()
    rain = _rain_routes(validity=LATER_VALIDITY)
    respx.get(REFL_DESC_URL).mock(
        return_value=httpx.Response(200, json=_refl_desc_json(validity=catalog_validity))
    )
    wash = respx.get(REFL_PRODUCT_URL).mock(
        return_value=httpx.Response(200, content=_bufr_fixture())
    )

    with_wash = await _render_all(MeteoFranceProvider())
    baseline = await _rain_only_baseline()

    assert rain.call_count == 2
    assert wash.call_count == (1 if expect_product_fetch else 0)
    assert with_wash == baseline


@_MF_WASH
@respx.mock
async def test_slow_reflectivity_cannot_stall_the_frame(no_backoff):
    """Best-effort has to cover latency, not just errors.

    The wash arm owns its own retry budget and the rain arm waits on it, so without a
    deadline a merely sluggish REFLECTIVITE endpoint would hold a frame for minutes —
    past the poll cadence, and past the tile request's own lifetime on the web fallback.
    """
    _token_route()
    rain = _rain_routes()

    # respx only counts *completed* calls, and this one is cancelled in flight — so
    # count attempts here instead, to tell "the deadline cut the arm off" apart from
    # "the arm was never tried".
    attempts = 0

    async def _never_answers(_request):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(60)
        return httpx.Response(200, json=_refl_desc_json())

    respx.get(REFL_DESC_URL).mock(side_effect=_never_answers)

    loop = asyncio.get_running_loop()
    started = loop.time()
    with override_settings(METEOFRANCE_REFLECTIVITY_DEADLINE=0.1):
        with_wash = await _render_all(MeteoFranceProvider())
    elapsed = loop.time() - started

    baseline = await _rain_only_baseline()

    assert attempts == 1, "the wash arm never reached the catalog"
    assert elapsed < 5.0, f"the hung wash arm held the frame for {elapsed:.1f}s"
    assert rain.call_count == 2
    assert with_wash == baseline


@_MF_WASH
@respx.mock
async def test_reflectivity_401_never_invalidates_the_shared_token(no_backoff):
    """The wash arm must not churn credentials the rain arm is using.

    Both arms run concurrently off one MeteoFranceAuth. An application ID subscribed to
    LAME_D_EAU but not REFLECTIVITE gets a 401 on every wash download; invalidating the
    shared token there would re-mint it on every frame forever and could knock the rain
    arm's in-flight token out from under it.
    """
    token = _token_route()
    rain = _rain_routes()
    respx.get(REFL_DESC_URL).mock(return_value=httpx.Response(200, json=_refl_desc_json()))
    wash = respx.get(REFL_PRODUCT_URL).mock(return_value=httpx.Response(401))

    results = await _render_all(MeteoFranceProvider())

    assert token.call_count == 1, "the wash arm's 401 re-minted the shared token"
    assert wash.call_count == 1, "a 401 the wash cannot fix must not be retried"
    assert rain.call_count == 1
    assert len(results) == 62
