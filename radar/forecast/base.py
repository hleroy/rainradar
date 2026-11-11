"""The forecast-source boundary: what the forecast job needs from an upstream.

Mirrors the lightning seam rather than ``RadarProvider``: a radar provider means
"past frames, archived for 90 days", while a forecast run is superseded every few
minutes and never archived. The job (:mod:`radar.forecast.job`) is source-agnostic;
everything PIAF-specific lives behind this protocol in :mod:`radar.forecast.piaf`.

Every source failure maps onto :class:`ForecastUnavailable`, so the job needs no
source-specific handling. :class:`RunNotReady` is the one that is *not* a failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Protocol

if TYPE_CHECKING:
    import httpx

    from radar.forecast.grib_decode import PiafField


class ForecastUnavailable(Exception):
    """A lead could not be fetched or decoded. The run is not published."""


class RunNotReady(ForecastUnavailable):
    """The run is not published upstream yet. Retry on the next tick; not a failure."""


class PayloadRejected(ForecastUnavailable):
    """Upstream answered with something that is not the field we asked for."""


class ForecastSource(Protocol):
    name: str

    @property
    def run_interval(self) -> int:
        """Seconds between consecutive runs."""
        ...

    @property
    def publication_delay(self) -> int:
        """How long after its reference time a run is first worth asking for."""
        ...

    @staticmethod
    def attribution() -> str:
        """The credit shown while a forecast frame is displayed (HTML, like the radar's)."""
        ...

    def client(self) -> httpx.AsyncClient:
        """A pooled client for one run's downloads."""
        ...

    async def download(self, run_ts: int, valid_ts: int, *, client: httpx.AsyncClient) -> bytes:
        """The raw response for one lead time; raises :class:`ForecastUnavailable`."""
        ...

    def decode(self, message: bytes, run_ts: int, valid_ts: int) -> PiafField:
        """Decode (sync, CPU-bound) one lead; raises :class:`PayloadRejected`."""
        ...
