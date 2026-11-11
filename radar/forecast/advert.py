"""The ``forecast`` advert in ``/api/radar/frames`` — what the browser may show, and when.

The job publishes a run pointer (``forecast:current``) once every frame of the run is
on disk; this module decides which of those frames are still worth showing. It only
reads that pointer: no disk, no upstream, and any failure degrades to an advert with
no frames, never to a failed ``/api/radar/frames``. That keeps the forecast's failure
domain out of the radar timeline.

Staleness rules (``specs/forecast-nowcast.md`` §4, §8.11):

1. **Live window and forecast provider only.** The forecast extends the Météo-France
   observation (§5 item 1) and exists only for the live window (item 5).
2. **Run age.** A run older than ``FORECAST_MAX_RUN_AGE`` is dropped whole.
3. **Never overlap a measurement.** Valid times at or before the newest observed frame
   are dropped: once a time is measured, the measurement wins.
4. **Horizon.** Valid times beyond newest observed + ``FORECAST_HORIZON_SECONDS`` are
   dropped, so the forecast always reaches the same distance past "now".
5. **Contiguous with the observation.** If the first remaining frame is more than one
   step after the newest observed frame, the whole forecast is dropped rather than
   shown across a hole the timeline would not explain.

Old clients and radar-only callers ignore the object, as they do the ``lightning`` one.
"""

from __future__ import annotations

import contextlib

from django.conf import settings

from radar import cache
from radar.forecast.piaf import PiafSource

# The radar source whose observation the forecast extends (and is masked to).
RADAR_PROVIDER = "meteofrance"
TILE_PREFIX = "/forecast"


def advertised_frames(record: dict, newest_observed: int, now: int) -> list[int]:
    """The valid times of ``record`` the browser may show; ``[]`` if none should be."""
    run = int(record["run"])
    if now - run > settings.FORECAST_MAX_RUN_AGE:
        return []
    horizon_end = newest_observed + settings.FORECAST_HORIZON_SECONDS
    valids = sorted(int(v) for v in record["frames"] if newest_observed < int(v) <= horizon_end)
    if not valids or valids[0] - newest_observed > settings.FORECAST_LEAD_STEP_SECONDS:
        return []
    return valids


def _empty() -> dict:
    return {
        "enabled": settings.FORECAST_ENABLED,
        "provider": RADAR_PROVIDER,
        "run": None,
        "frames": [],
        "attribution": PiafSource.attribution(),
        "tile_base": None,
    }


async def forecast_advert(provider_name: str | None, observed: list[int], now: int) -> dict:
    """The advert for one ``/api/radar/frames`` response.

    ``provider_name`` is the response's radar source, ``None`` for a historical query;
    ``observed`` its frame timestamps, oldest first.
    """
    advert = _empty()
    if not settings.FORECAST_ENABLED or provider_name != RADAR_PROVIDER or not observed:
        return advert
    record = None
    with contextlib.suppress(Exception):  # Redis down or a mangled pointer: no forecast
        record = await cache.get_forecast_current()
    if record is None:
        return advert
    try:
        run = int(record["run"])
        frames = advertised_frames(record, observed[-1], now)
    except KeyError, TypeError, ValueError:
        return advert
    if frames:
        advert.update(
            run=run,
            frames=[{"timestamp": ts} for ts in frames],
            tile_base=f"{TILE_PREFIX}/{run}",
        )
    return advert
