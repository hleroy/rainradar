"""PIAF forecast render — the coverage mask and the shared rain pipeline. No network.

The observation side is a synthetic ODIM grid (``build_odim``, the render suite's own
builder); the forecast side is a real PIAF fixture. Covers: the maintainer's decision
that forecast rain is hidden wherever the newest observation has no radar coverage
(``specs/forecast-nowcast.md`` §5, item 6), the mask cache, and that the forecast
renders through the observation's own pyramid and palette — including the mip-level
choice for a grid measured in degrees rather than metres.
"""

from __future__ import annotations

import gzip
import math

import numpy as np
import pytest
from django.conf import settings

from radar.forecast import grib_decode
from radar.forecast import render as forecast_render
from radar.providers import meteofrance_render
from radar.providers.meteofrance_render import coverage_of
from radar.providers.meteofrance_render import parse_grid
from radar.tests.test_forecast_grib import DRY_BBOX
from radar.tests.test_forecast_grib import DRY_RUN
from radar.tests.test_forecast_grib import DRY_VALID
from radar.tests.test_forecast_grib import RAIN_RUN
from radar.tests.test_forecast_grib import RAIN_SMALL
from radar.tests.test_forecast_grib import RAIN_VALID
from radar.tests.test_forecast_grib import SMALL_BBOX
from radar.tests.test_meteofrance_render import NODATA
from radar.tests.test_meteofrance_render import UNDETECT
from radar.tests.test_meteofrance_render import build_odim


@pytest.fixture(scope="module")
def rain():
    """The Ligurian-Sea box: 38 % wet, peaking at 108 mm/h."""
    return grib_decode.decode(
        RAIN_SMALL.read_bytes(), run_ts=RAIN_RUN, valid_ts=RAIN_VALID, bbox=SMALL_BBOX
    )


@pytest.fixture(scope="module")
def dry_bbox():
    """The near-dry full ``RADAR_BBOX`` frame: exactly the production geometry."""
    return grib_decode.decode(
        gzip.decompress(DRY_BBOX.read_bytes()),
        run_ts=DRY_RUN,
        valid_ts=DRY_VALID,
        bbox=tuple(settings.RADAR_BBOX),
    )


def _observation(size: int, *, fill: int = UNDETECT, xscale: float = 5000.0):
    """The coverage of a ``size``² synthetic LAME_D_EAU grid centred on France."""
    data = np.full((size, size), fill, dtype=np.uint16)
    raw, _ = build_odim(data=data, xsize=size, ysize=size, xscale=xscale, yscale=xscale)
    return coverage_of(parse_grid(raw), 0)


def _cell(geo, lat: float, lon: float) -> tuple[int, int]:
    return (
        math.floor((geo.y_ul - lat) / geo.yscale),
        math.floor((lon - geo.x_ul) / geo.xscale),
    )


# -- the coverage mask -------------------------------------------------------------


def test_undetect_counts_as_coverage(dry_bbox):
    """Undetect ("the radar looked and saw nothing") is coverage: the forecast shows there."""
    obs = _observation(512)  # 2 560 km square: covers the whole bbox
    assert forecast_render.coverage_mask(obs, dry_bbox.geo).all()


def test_nodata_is_not_coverage(dry_bbox):
    obs = _observation(512, fill=NODATA)
    assert not forecast_render.coverage_mask(obs, dry_bbox.geo).any()


def test_off_the_observation_grid_is_not_coverage(dry_bbox):
    """A 320 km observation grid around (2.5, 46.6) covers its centre, not Brittany."""
    obs = _observation(64)
    mask = forecast_render.coverage_mask(obs, dry_bbox.geo)
    assert mask[_cell(dry_bbox.geo, 46.6, 2.5)]
    assert not mask[_cell(dry_bbox.geo, 48.4, -4.5)]


def test_mask_follows_the_observation_nodata_cells(dry_bbox):
    """West half nodata, east half undetect ⇒ the mask flips across the grid's centre."""
    data = np.full((512, 512), UNDETECT, dtype=np.uint16)
    data[:, :256] = NODATA
    obs = coverage_of(parse_grid(build_odim(data=data, xsize=512, ysize=512)[0]), 0)
    mask = forecast_render.coverage_mask(obs, dry_bbox.geo)
    assert not mask[_cell(dry_bbox.geo, 46.6, -1.0)]
    assert mask[_cell(dry_bbox.geo, 46.6, 6.0)]


def test_mask_to_coverage_blanks_only_uncovered_cells(rain):
    """Masked cells become NaN (the pipeline's "no coverage"); the rest are untouched."""
    data = np.full((512, 512), UNDETECT, dtype=np.uint16)
    data[:256, :] = NODATA  # the northern half of an observation grid centred on the box
    grid = parse_grid(build_odim(data=data, xsize=512, ysize=512, center=(8.75, 43.5))[0])
    obs = coverage_of(grid, 0)
    mask = forecast_render.coverage_mask(obs, rain.geo)
    masked = forecast_render.mask_to_coverage(rain, obs)

    assert masked.geo == rain.geo
    assert masked.field.dtype == np.float32
    assert mask.any()
    assert not mask.all()
    assert np.isnan(masked.field[~mask]).all()
    assert np.array_equal(masked.field[mask], rain.rate[mask])


def test_observation_index_is_cached_and_bounded(dry_bbox):
    obs = _observation(64)
    first = forecast_render._observation_index(obs.geo, dry_bbox.geo)
    assert forecast_render._observation_index(obs.geo, dry_bbox.geo) is first

    for size in (65, 66, 67, 68):  # four more geometries: the cache must not grow past 4
        forecast_render._observation_index(_observation(size).geo, dry_bbox.geo)
    assert len(forecast_render._index_cache) <= forecast_render._INDEX_CACHE_MAX


# -- rendering ---------------------------------------------------------------------


def test_fully_covered_rain_paints_tiles(rain):
    tiles = forecast_render.render_forecast_frame(rain, _observation(512))
    assert len(tiles) == 62
    assert any(tiles.values())


def test_no_coverage_paints_nothing(rain):
    """Rain everywhere in PIAF, nodata everywhere in the observation ⇒ every tile empty."""
    tiles = forecast_render.render_forecast_frame(rain, _observation(512, fill=NODATA))
    assert len(tiles) == 62
    assert not any(tiles.values())


def test_rate_frame_matches_the_observation_render():
    """Reuse, don't fork: a rate field rendered as a forecast is the observation's tiles.

    The same LAME_D_EAU grid through ``render_frame`` (the observation path) and, as
    its ``rate_field``, through ``render_rate_frame`` (the forecast path) must give
    byte-identical PNGs — same pyramid, same blur, same palette.
    """
    data = np.full((256, 256), UNDETECT, dtype=np.uint16)
    data[100:140, 90:150] = 25  # 3 mm/h
    data[:, :40] = NODATA
    grid = parse_grid(build_odim(data=data)[0])
    observed = meteofrance_render.render_frame(grid)

    twin = parse_grid(build_odim(data=data)[0])  # fresh pyramid memo
    as_forecast = forecast_render.ForecastGrid(
        field=meteofrance_render.rate_field(twin), geo=twin.geo
    )
    assert meteofrance_render.render_rate_frame(as_forecast) == observed
    assert any(observed.values())


# -- mip levels for a grid in degrees -------------------------------------------------


@pytest.mark.parametrize(("z", "level"), [(7, 0), (6, 1), (5, 2), (4, 3), (3, 3)])
def test_mip_level_of_the_lat_lon_grid(dry_bbox, z, level):
    """0.01° cells: native at z7, one reduction per zoom out, capped at the last level.

    Read as metres, 0.01 would make every zoom pick the coarsest level — a z7 forecast
    drawn from ~9 km cells.
    """
    n_levels = len(meteofrance_render.build_pyramid(dry_bbox.rate, dry_bbox.geo))
    assert n_levels == 4
    assert meteofrance_render.mip_level(z, 45, dry_bbox.geo, n_levels) == level
