"""PIAF rain-rate grid → the 62-tile matrix, masked to the observation's radar coverage.

PIAF fills the areas the radars cannot see — the Bay of Biscay, northern Spain, the
open sea, about 11 % of ``RADAR_BBOX`` — with model rain, while the observation leaves
them blank. Drawn as-is, rain would appear there the moment the cursor crossed into
the forecast. The maintainer decided the forecast is shown only where the newest
observed frame has radar coverage (``specs/forecast-nowcast.md`` §5, item 6), so every
PIAF cell whose LAME_D_EAU counterpart is ``nodata`` — or off the observation grid —
becomes NaN. The coverage arrives as a
:class:`~radar.providers.meteofrance_render.Coverage`, handed over by the Météo-France
provider. NaN is what the shared pipeline already treats as "no coverage": excluded
from the pyramid's maxima and from the blur, and drawn transparent.

Rendering goes through :func:`~radar.providers.meteofrance_render.render_rate_frame`,
so the forecast uses the observation's own pyramid, smoothing and rain LUT. Pure
functions plus one process-wide cache; no HTTP, no Django models.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

import numpy as np

from radar.providers.meteofrance_render import GridGeo
from radar.providers.meteofrance_render import render_rate_frame
from radar.providers.meteofrance_render import transformer_for

if TYPE_CHECKING:
    from radar.forecast.grib_decode import PiafField
    from radar.providers.meteofrance_render import Coverage


@dataclass
class ForecastGrid:
    """A masked forecast field, in the shape ``render_rate_frame`` consumes."""

    field: np.ndarray  # float32 mm/h; NaN = outside the observation's radar coverage
    geo: GridGeo
    # Mip pyramid memo, filled by the render pipeline once per frame.
    _levels: list[tuple[np.ndarray, GridGeo]] | None = field(default=None, repr=False)


# (observation geo, forecast geo) -> (flat observation index, inside-the-grid mask), one
# entry per forecast cell. Both geometries are fixed for the process lifetime, so this
# is computed once; the bound only matters if a geometry ever changes underneath us.
_index_cache: dict[tuple[GridGeo, GridGeo], tuple[np.ndarray, np.ndarray]] = {}
_INDEX_CACHE_MAX = 4


def _observation_index(obs_geo: GridGeo, geo: GridGeo) -> tuple[np.ndarray, np.ndarray]:
    """For every forecast cell centre, the nearest observation cell (same rule as tiles)."""
    key = (obs_geo, geo)
    cached = _index_cache.get(key)
    if cached is not None:
        return cached

    lon = geo.x_ul + geo.xscale * (np.arange(geo.xsize, dtype=np.float64) + 0.5)
    lat = geo.y_ul - geo.yscale * (np.arange(geo.ysize, dtype=np.float64) + 0.5)
    lon2d, lat2d = np.meshgrid(lon, lat)
    px, py = transformer_for(obs_geo.projdef).transform(lon2d, lat2d)
    col = np.floor((px - obs_geo.x_ul) / obs_geo.xscale).astype(np.int64)
    row = np.floor((obs_geo.y_ul - py) / obs_geo.yscale).astype(np.int64)
    inside = (col >= 0) & (col < obs_geo.xsize) & (row >= 0) & (row < obs_geo.ysize)
    # int32 is ample (the observation grid has ~2.4 M cells) and halves the cache.
    flat = np.where(inside, row * obs_geo.xsize + col, 0).astype(np.int32)

    if len(_index_cache) >= _INDEX_CACHE_MAX:
        _index_cache.clear()
    result = (flat, inside)
    _index_cache[key] = result
    return result


def coverage_mask(coverage: Coverage, geo: GridGeo) -> np.ndarray:
    """Boolean ``(ysize, xsize)`` mask on ``geo``: True where the observation could see.

    "Coverage" is anything but ``nodata``: ``undetect`` (the radar looked and saw no
    rain) and real rain both count. A forecast cell that falls off the observation
    grid has no coverage either.
    """
    flat, inside = _observation_index(coverage.geo, geo)
    covered = coverage.covered.ravel()[flat]
    return (inside & covered).reshape(geo.ysize, geo.xsize)


def mask_to_coverage(piaf: PiafField, coverage: Coverage) -> ForecastGrid:
    """The forecast with every cell outside the observed radar coverage set to NaN."""
    mask = coverage_mask(coverage, piaf.geo)
    masked = np.where(mask, piaf.rate, np.nan).astype(np.float32)
    return ForecastGrid(field=masked, geo=piaf.geo)


def render_forecast_frame(
    piaf: PiafField,
    coverage: Coverage,
) -> dict[tuple[int, int, int], bytes | None]:
    """Render one forecast frame over the tile matrix, masked to ``coverage``.

    ``coverage`` is the newest observed LAME_D_EAU frame's. Sync and CPU-bound, like
    the observation's render; the caller threads it.
    """
    return render_rate_frame(mask_to_coverage(piaf, coverage))
