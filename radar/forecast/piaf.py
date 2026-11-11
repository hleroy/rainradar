"""Météo-France PIAF client: one ``GetCoverage`` per lead time, decoded strictly.

All URLs are built here from settings — the coverage ID from the run clock, never
from upstream XML — so there is nothing upstream-controlled to validate against SSRF,
and ``GetCapabilities`` (a 2 MB document) is never needed (``specs/forecast-nowcast.md``
§8.2). The token comes from the Météo-France provider's own :class:`MeteoFranceAuth`
instance: a second instance would revoke the first's token (§8.1).

Status mapping, measured in Phase 0 (§8.5): **404 means "not published yet"**, never
403; a 401 means our token was revoked by another mint, so invalidate and retry once;
5xx and transport errors are retried briefly; anything else fails the run. A 200 must
be GRIB and must decode to exactly the run and valid time requested, because the
service once answered with somebody else's GRIB under a 200.

Never logs the token or the application ID.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx
from django.conf import settings

from radar.forecast import grib_decode
from radar.forecast.base import ForecastUnavailable
from radar.forecast.base import PayloadRejected
from radar.forecast.base import RunNotReady
from radar.providers.meteofrance_auth import AuthError
from radar.providers.meteofrance_auth import MeteoFranceAuth

logger = logging.getLogger(__name__)

_PARAMETER = "TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE"
_PERIOD = "PT5M"
_GRIB = "application/wmo-grib"

# A bbox frame is 3.2 MB; the API answered in 0.5 s typically, 7 s at worst.
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=30.0)
_BACKOFFS = (1.0, 3.0)  # 5xx / transport retries within one tick
_BACKOFF_JITTER = 0.5

# Etalab Licence Ouverte 2.0: name the source. The other half of the licence, the date
# of the last update, is the run's reference time — the frontend shows it next to this.
_ATTRIBUTION = (
    'Prévision <a href="https://meteofrance.fr" target="_blank" '
    'rel="noopener">Météo-France</a> PIAF (Licence Ouverte 2.0)'
)


def coverage_id(run_ts: int) -> str:
    """``TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE___2026-11-11T11.10.00Z_PT5M``."""
    run = time.strftime("%Y-%m-%dT%H.%M.%SZ", time.gmtime(run_ts))
    return f"{_PARAMETER}___{run}_{_PERIOD}"


class PiafSource:
    name = "piaf"

    def __init__(self, auth: MeteoFranceAuth) -> None:
        self._auth = auth

    @staticmethod
    def attribution() -> str:
        return _ATTRIBUTION

    @property
    def run_interval(self) -> int:
        return settings.FORECAST_RUN_INTERVAL

    @property
    def publication_delay(self) -> int:
        return settings.FORECAST_PUBLICATION_DELAY

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)

    @staticmethod
    def url() -> str:
        return f"{settings.FORECAST_API_BASE_URL}/wcs/{settings.FORECAST_WCS_SERVICE}/GetCoverage"

    @staticmethod
    def params(run_ts: int, valid_ts: int) -> list[tuple[str, str]]:
        south, north, west, east = settings.RADAR_BBOX
        return [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("coverageid", coverage_id(run_ts)),
            ("format", _GRIB),
            # The window that ends at valid_ts starts 5 minutes earlier; the API
            # indexes it by its end, in seconds after the run (§8.3).
            ("subset", f"time({valid_ts - run_ts})"),
            ("subset", f"lat({south},{north})"),
            ("subset", f"long({west},{east})"),
        ]

    async def download(self, run_ts: int, valid_ts: int, *, client: httpx.AsyncClient) -> bytes:
        forced_refresh = False
        attempt = 0
        while True:
            try:
                token = await self._auth.get_token()
                resp = await client.get(
                    self.url(),
                    params=self.params(run_ts, valid_ts),
                    headers={"Authorization": f"Bearer {token}"},
                )
            except AuthError as exc:
                msg = "Météo-France token unavailable"
                raise ForecastUnavailable(msg) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.warning("PIAF download error: %s", exc)
            else:
                status = resp.status_code
                if status == httpx.codes.OK:
                    return self._checked(resp)
                if status == httpx.codes.NOT_FOUND:
                    raise RunNotReady
                if status == httpx.codes.UNAUTHORIZED and not forced_refresh:
                    # Another mint for this application revoked our token (§8.1).
                    await self._auth.invalidate(token)
                    forced_refresh = True
                    continue
                logger.warning("PIAF GetCoverage -> HTTP %s", status)
                if status < httpx.codes.INTERNAL_SERVER_ERROR:
                    msg = f"PIAF answered HTTP {status}"
                    raise ForecastUnavailable(msg)  # a 4xx (incl. 429) will not fix on retry
            if attempt >= len(_BACKOFFS):
                msg = "PIAF download failed after retries"
                raise ForecastUnavailable(msg)
            await asyncio.sleep(_BACKOFFS[attempt] + random.uniform(0.0, _BACKOFF_JITTER))  # noqa: S311
            attempt += 1

    @staticmethod
    def _checked(resp: httpx.Response) -> bytes:
        content_type = resp.headers.get("content-type", "")
        if not content_type.startswith(_GRIB):
            msg = f"expected GRIB, got {content_type or 'no content type'}"
            raise PayloadRejected(msg)
        return resp.content

    def decode(self, message: bytes, run_ts: int, valid_ts: int) -> grib_decode.PiafField:
        try:
            return grib_decode.decode(
                message,
                run_ts=run_ts,
                valid_ts=valid_ts,
                bbox=tuple(settings.RADAR_BBOX),
            )
        except grib_decode.GribDecodeError as exc:
            raise PayloadRejected(str(exc)) from exc
