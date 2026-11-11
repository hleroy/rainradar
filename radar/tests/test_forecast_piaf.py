"""PIAF client — request shape and the status mapping measured in Phase 0. No network.

The mapping is the point (``specs/forecast-nowcast.md`` §8.5): 404 is "not published
yet" and must never count as a failure; a 401 means another mint revoked our token, so
invalidate and retry once; a 200 must be GRIB and decode to exactly what was asked.
"""

from __future__ import annotations

import asyncio
import gzip
from urllib.parse import parse_qsl
from urllib.parse import urlsplit

import httpx
import pytest
import respx
from django.conf import settings
from django.test import override_settings

from radar.forecast import piaf
from radar.forecast.base import ForecastUnavailable
from radar.forecast.base import PayloadRejected
from radar.forecast.base import RunNotReady
from radar.providers.meteofrance_auth import AuthError
from radar.providers.meteofrance_auth import MeteoFranceAuth
from radar.tests.test_forecast_grib import DRY_BBOX
from radar.tests.test_forecast_grib import DRY_RUN
from radar.tests.test_forecast_grib import DRY_VALID

URL = "https://api.meteofrance.fr/pro/piaf/1.0/wcs/MF-NWP-HIGHRES-PIAF-001-FRANCE-WCS/GetCoverage"
GRIB = {"content-type": "application/wmo-grib"}


class FakeAuth:
    """Stands in for the provider's MeteoFranceAuth; counts mints and invalidations."""

    def __init__(self, *, fail: bool = False) -> None:
        self.minted = 0
        self.invalidated = 0
        self.fail = fail

    async def get_token(self) -> str:
        if self.fail:
            raise AuthError
        self.minted += 1
        return f"tok-{self.minted}"

    async def invalidate(self, stale: str) -> None:
        assert stale == f"tok-{self.minted}", "invalidated a token it was not given"
        self.invalidated += 1


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    async def instant(_delay):
        return None

    monkeypatch.setattr(piaf.asyncio, "sleep", instant)


@pytest.fixture
def grib() -> bytes:
    return gzip.decompress(DRY_BBOX.read_bytes())


async def _download(source, run=DRY_RUN, valid=DRY_VALID):
    async with source.client() as client:
        return await source.download(run, valid, client=client)


# -- the request -------------------------------------------------------------------


def test_coverage_id_from_the_run_clock():
    assert piaf.coverage_id(DRY_RUN) == (
        "TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE___2026-11-08T12.00.00Z_PT5M"
    )


@respx.mock
async def test_request_asks_for_one_lead_cropped_to_the_bbox(grib):
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=grib, headers=GRIB))
    auth = FakeAuth()
    assert await _download(piaf.PiafSource(auth), valid=DRY_RUN + 3600) == grib

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok-1"
    query = parse_qsl(urlsplit(str(request.url)).query)
    south, north, west, east = settings.RADAR_BBOX
    assert ("coverageid", piaf.coverage_id(DRY_RUN)) in query
    assert ("format", "application/wmo-grib") in query
    assert ("subset", "time(3600)") in query
    assert ("subset", f"lat({south},{north})") in query
    assert ("subset", f"long({west},{east})") in query


# -- status mapping ------------------------------------------------------------------


@respx.mock
async def test_404_means_not_published_yet():
    respx.get(URL).mock(return_value=httpx.Response(404, text="<ExceptionReport/>"))
    with pytest.raises(RunNotReady):
        await _download(piaf.PiafSource(FakeAuth()))


@respx.mock
async def test_401_invalidates_the_shared_token_and_retries_once(grib):
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, content=grib, headers=GRIB)]
    )
    auth = FakeAuth()
    assert await _download(piaf.PiafSource(auth)) == grib
    assert auth.invalidated == 1
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-2"


@respx.mock
async def test_second_401_fails_the_run():
    respx.get(URL).mock(return_value=httpx.Response(401))
    auth = FakeAuth()
    with pytest.raises(ForecastUnavailable):
        await _download(piaf.PiafSource(auth))
    assert auth.invalidated == 1  # once, not a loop


@respx.mock
async def test_staggered_401s_on_one_revoked_token_mint_once(grib):
    """Concurrent leads holding one revoked token re-mint it once, not once per lead.

    Another process mints, revoking tok-1 under two in-flight leads. Lead A's 401 comes
    back first and mints tok-2; lead B's 401 only lands after that. Every mint revokes
    the previous token (§8.1), so B must pick up tok-2: minting tok-3 would revoke the
    token A — and the observation poll, which shares this auth — is using.
    """
    state = {"minted": 0, "live": None}

    def mint(_request):
        state["minted"] += 1
        state["live"] = f"tok-{state['minted']}"
        return httpx.Response(200, json={"access_token": state["live"], "expires_in": 3600})

    lead_a, lead_b = DRY_VALID, DRY_VALID + 300
    b_sent, a_recovered = asyncio.Event(), asyncio.Event()

    async def coverage(request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        is_b = f"time({lead_b - DRY_RUN})" in request.url.params.get_list("subset")
        if token != state["live"]:
            # Both requests go out on the revoked tok-1; A's 401 comes back first,
            # B's only once A has re-minted and succeeded.
            if is_b:
                b_sent.set()
                await a_recovered.wait()
            else:
                await b_sent.wait()
            return httpx.Response(401)
        if not is_b:
            a_recovered.set()
        return httpx.Response(200, content=grib, headers=GRIB)

    respx.post(settings.METEOFRANCE_TOKEN_URL).mock(side_effect=mint)
    respx.get(URL).mock(side_effect=coverage)
    auth = MeteoFranceAuth()
    await auth.get_token()  # tok-1, shared with the observation poll
    state["live"] = "minted-elsewhere"  # the web container minted: tok-1 is revoked

    source = piaf.PiafSource(auth)
    async with asyncio.timeout(5), source.client() as client:
        results = await asyncio.gather(
            source.download(DRY_RUN, lead_a, client=client),
            source.download(DRY_RUN, lead_b, client=client),
        )
    assert results == [grib, grib]
    assert state["minted"] == 2, "a late 401 re-minted over the fresh token"


@respx.mock
async def test_5xx_and_transport_errors_are_retried(grib):
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("boom"),
            httpx.Response(200, content=grib, headers=GRIB),
        ]
    )
    assert await _download(piaf.PiafSource(FakeAuth())) == grib
    assert route.call_count == 3


@respx.mock
async def test_persistent_5xx_gives_up():
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(ForecastUnavailable, match="after retries"):
        await _download(piaf.PiafSource(FakeAuth()))
    assert route.call_count == 3  # first try + two retries


@pytest.mark.parametrize("status", [400, 403, 429])
@respx.mock
async def test_other_4xx_fail_without_retry(status):
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(ForecastUnavailable, match=str(status)):
        await _download(piaf.PiafSource(FakeAuth()))
    assert route.call_count == 1


async def test_token_failure_is_a_forecast_failure():
    with pytest.raises(ForecastUnavailable, match="token"):
        await _download(piaf.PiafSource(FakeAuth(fail=True)))


@respx.mock
async def test_a_200_that_is_not_grib_is_rejected():
    """Once, GetCapabilities answered 200 with a GRIB; the reverse must not pass either."""
    respx.get(URL).mock(
        return_value=httpx.Response(200, text="<xml/>", headers={"content-type": "application/xml"})
    )
    with pytest.raises(PayloadRejected, match="expected GRIB"):
        await _download(piaf.PiafSource(FakeAuth()))


# -- decoding --------------------------------------------------------------------------


def test_decode_returns_the_bbox_field(grib):
    field = piaf.PiafSource(FakeAuth()).decode(grib, DRY_RUN, DRY_VALID)
    assert field.rate.shape == (1031, 1571)
    assert (field.run_ts, field.valid_ts) == (DRY_RUN, DRY_VALID)


def test_decode_rejects_the_wrong_lead(grib):
    """Well-formed GRIB for another valid time: somebody else's answer under a 200."""
    with pytest.raises(PayloadRejected, match="forecast time"):
        piaf.PiafSource(FakeAuth()).decode(grib, DRY_RUN, DRY_VALID + 300)


@override_settings(FORECAST_API_BASE_URL="https://example.test/piaf", FORECAST_WCS_SERVICE="SVC")
def test_url_comes_from_settings():
    assert piaf.PiafSource.url() == "https://example.test/piaf/wcs/SVC/GetCoverage"
