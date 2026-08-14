"""Météo-France ODIM render pipeline. No network, no DB.

Builds a tiny synthetic ODIM_H5 grid in memory (h5py) with the real projdef and a
few known ACRR cells — no committed binary blob. Covers the rain LUT by pin,
nodata/undetect/zero transparency, all-empty ⇒ None, a corner-based orientation
check (catches a north/south flip), and the per-tile index cache.
"""

from __future__ import annotations

import io
import math

import h5py
import numpy as np
import pyproj
import pytest
from PIL import Image

from radar.providers import meteofrance_render
from radar.providers.meteofrance_render import RenderError
from radar.providers.meteofrance_render import parse_grid
from radar.providers.meteofrance_render import rain_color
from radar.providers.meteofrance_render import render_frame
from radar.providers.meteofrance_render import render_tile

# The real upstream projection; the synthetic grid reuses it (coarser scale/size).
REAL_PROJDEF = (
    "+proj=stere +lat_0=90 +lon_0=0 +lat_ts=45 +ellps=WGS84 "
    "+x_0=619652.07 +y_0=5262818.34 +datum=WGS84"
)
UNDETECT = 65534
NODATA = 65535
GAIN = 0.01
# raw 25 -> 25*0.01*12 = 3.0 mm/h exactly.
# In the RainViewer-matched LUT, 3.0 mm/h sits between dark blue (2.73 mm/h,
# #005588ff) and yellow (5.6 mm/h, #ffee00ff) — a muted teal.
RAW_3MMH = 25
COLOR_3MMH = (0x17, 0x63, 0x7B, 0xFF)

MATRIX_SIZE = 62


def _fwd():
    return pyproj.Transformer.from_crs(
        "EPSG:4326",
        pyproj.CRS.from_proj4(REAL_PROJDEF),
        always_xy=True,
    )


def build_odim(  # noqa: PLR0913 — keyword-only ODIM knobs, all with defaults
    *,
    data=None,
    quantity="ACRR",
    xsize=256,
    ysize=256,
    xscale=5000.0,
    yscale=5000.0,
    center=(2.5, 46.6),
) -> tuple[bytes, dict]:
    """A minimal in-memory ODIM_H5 LAME_D_EAU grid centred over France."""
    fwd = _fwd()
    inv = pyproj.Transformer.from_crs(
        pyproj.CRS.from_proj4(REAL_PROJDEF),
        "EPSG:4326",
        always_xy=True,
    )
    xc, yc = fwd.transform(*center)
    x_ul = xc - (xsize / 2) * xscale
    y_ul = yc + (ysize / 2) * yscale
    x_lr = x_ul + xsize * xscale
    y_lr = y_ul - ysize * yscale
    ul_lon, ul_lat = inv.transform(x_ul, y_ul)
    lr_lon, lr_lat = inv.transform(x_lr, y_lr)
    ll_lon, ll_lat = inv.transform(x_ul, y_lr)
    ur_lon, ur_lat = inv.transform(x_lr, y_ul)

    if data is None:
        data = np.full((ysize, xsize), UNDETECT, dtype=np.uint16)

    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        where = f.create_group("where")
        where.attrs["projdef"] = REAL_PROJDEF
        where.attrs["xscale"] = xscale
        where.attrs["yscale"] = yscale
        where.attrs["xsize"] = xsize
        where.attrs["ysize"] = ysize
        where.attrs["UL_lat"] = ul_lat
        where.attrs["UL_lon"] = ul_lon
        where.attrs["LR_lat"] = lr_lat
        where.attrs["LR_lon"] = lr_lon
        where.attrs["LL_lat"] = ll_lat
        where.attrs["LL_lon"] = ll_lon
        where.attrs["UR_lat"] = ur_lat
        where.attrs["UR_lon"] = ur_lon
        what = f.create_group("dataset1/data1/what")
        what.attrs["quantity"] = quantity
        what.attrs["gain"] = GAIN
        what.attrs["offset"] = 0.0
        what.attrs["nodata"] = NODATA
        what.attrs["undetect"] = UNDETECT
        f.create_dataset("dataset1/data1/data", data=data)

    meta = {"x_ul": x_ul, "y_ul": y_ul, "xscale": xscale, "yscale": yscale}
    return buf.getvalue(), meta


def rain_h5() -> bytes:
    """A uniform 3 mm/h ODIM composite — the stock "it rains everywhere" fixture.

    Shared with the provider and composite suites, which need a grid that paints
    tiles without caring about its shape.
    """
    return build_odim(data=np.full((256, 256), RAW_3MMH, dtype=np.uint16))[0]


def _cell_of(lat, lon, meta):
    """Grid (row, col) containing a lat/lon, using the same geometry as the render."""
    px, py = _fwd().transform(lon, lat)
    col = math.floor((px - meta["x_ul"]) / meta["xscale"])
    row = math.floor((meta["y_ul"] - py) / meta["yscale"])
    return row, col


def _tile_of(lat, lon, z):
    """Slippy tile (x, y) and within-tile pixel (px, py) for a lat/lon (independent math)."""
    n_pix = 256 * (2**z)
    gpx = (lon + 180.0) / 360.0 * n_pix
    gpy = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n_pix
    xt, yt = int(gpx // 256), int(gpy // 256)
    return xt, yt, gpx - xt * 256, gpy - yt * 256


# -- LUT ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        # Exact LUT stops (RainViewer-matched Universal Blue palette)
        (0.01, (0x7C, 0x75, 0x65, 0x1A)),
        (0.03, (0x7C, 0x75, 0x65, 0x3E)),
        (0.05, (0x8B, 0x82, 0x6D, 0x59)),
        (0.08, (0x92, 0x88, 0x71, 0x64)),
        (0.15, (0xCE, 0xC0, 0x87, 0x96)),
        (0.24, (0xDE, 0xD0, 0x97, 0xBE)),
        (0.32, (0x88, 0xDD, 0xEE, 0xFF)),
        (0.65, (0x00, 0xA3, 0xE0, 0xFF)),
        (1.33, (0x00, 0x77, 0xAA, 0xFF)),
        (2.73, (0x00, 0x55, 0x88, 0xFF)),
        (5.6, (0xFF, 0xEE, 0x00, 0xFF)),
        (11.5, (0xFF, 0xAA, 0x00, 0xFF)),
        (23.7, (0xFF, 0x44, 0x00, 0xFF)),
        (48.6, (0xC1, 0x00, 0x00, 0xFF)),
        (100.0, (0xFF, 0xAA, 0xFF, 0xFF)),
        (200.0, (0xFF, 0x77, 0xFF, 0xFF)),
        (420.0, (0xFF, 0xFF, 0xFF, 0xFF)),
        (500.0, (0xFF, 0xFF, 0xFF, 0xFF)),  # clamps to the last stop at ≥420
    ],
)
def test_lut_values_by_pin(rate, expected):
    assert rain_color(rate) == expected


def test_lut_below_first_stop_is_transparent():
    # 0.02 is above the new 0.01 minimum — it interpolates between the first two stops
    assert rain_color(0.02) == (0x7C, 0x75, 0x65, 0x2C)
    assert rain_color(0.0) == (0, 0, 0, 0)
    assert rain_color(0.005) == (0, 0, 0, 0)


# -- parse_grid ---------------------------------------------------------------


def test_parse_grid_reads_metadata():
    h5_bytes, _ = build_odim()
    grid = parse_grid(h5_bytes)
    assert grid.gain == GAIN
    assert grid.offset == 0.0
    assert grid.nodata == NODATA
    assert grid.undetect == UNDETECT
    assert grid.geo.xsize == 256
    assert grid.geo.ysize == 256
    assert grid.data.shape == (256, 256)
    assert "+proj=stere" in grid.geo.projdef


def test_parse_grid_rejects_wrong_quantity():
    h5_bytes, _ = build_odim(quantity="DBZH")
    with pytest.raises(RenderError):
        parse_grid(h5_bytes)


def test_parse_grid_rejects_garbage():
    with pytest.raises(RenderError):
        parse_grid(b"not an hdf5 file at all")


# -- empty-tile semantics -----------------------------------------------------


def _paris_tile(z=7):
    return _tile_of(48.85, 2.35, z)


def test_render_tile_undetect_is_none():
    h5_bytes, _ = build_odim()  # all undetect
    grid = parse_grid(h5_bytes)
    xt, yt, _, _ = _paris_tile()
    assert render_tile(grid, 7, xt, yt) is None


def test_render_tile_nodata_is_none():
    data = np.full((256, 256), NODATA, dtype=np.uint16)
    grid = parse_grid(build_odim(data=data)[0])
    xt, yt, _, _ = _paris_tile()
    assert render_tile(grid, 7, xt, yt) is None


def test_render_tile_zero_is_none():
    data = np.zeros((256, 256), dtype=np.uint16)  # detected but 0 mm ⇒ below 0.01 mm/h
    grid = parse_grid(build_odim(data=data)[0])
    xt, yt, _, _ = _paris_tile()
    assert render_tile(grid, 7, xt, yt) is None


def test_render_frame_all_empty_returns_full_none_matrix():
    grid = parse_grid(build_odim()[0])
    frame = render_frame(grid)
    assert len(frame) == MATRIX_SIZE  # the computed 62-tile matrix, not a hardcoded list
    assert all(v is None for v in frame.values())


# -- orientation + sampling ---------------------------------------------------


def test_known_cell_lands_in_expected_tile_pixel():
    """A rain cell at a known lat/lon paints the matching tile pixel (catches N/S flip)."""
    lat_p, lon_p = 48.85, 2.35  # Paris — well inside the synthetic grid
    data = np.full((256, 256), UNDETECT, dtype=np.uint16)
    _, meta = build_odim()  # geometry only, to locate the cell
    row_p, col_p = _cell_of(lat_p, lon_p, meta)
    data[row_p - 1 : row_p + 2, col_p - 1 : col_p + 2] = RAW_3MMH  # a 3x3 rain block
    grid = parse_grid(build_odim(data=data)[0])

    z = 7
    xt, yt, px_exp, py_exp = _tile_of(lat_p, lon_p, z)
    png = render_tile(grid, z, xt, yt)
    assert png is not None

    arr = np.asarray(Image.open(io.BytesIO(png)))
    ys, xs = np.where(arr[..., 3] > 0)
    assert ys.size > 0
    # The painted centroid must sit at the point's pixel, not its vertical mirror.
    assert abs(ys.mean() - py_exp) < 12
    assert abs(xs.mean() - px_exp) < 12
    # And it carries the 3 mm/h colour.
    cy, cx = int(ys.mean()), int(xs.mean())
    assert tuple(int(v) for v in arr[cy, cx]) == COLOR_3MMH


# -- index cache --------------------------------------------------------------


def test_tile_index_cached_on_second_call():
    grid = parse_grid(build_odim()[0])
    first = meteofrance_render.tile_index(5, 16, 11, grid.geo)
    second = meteofrance_render.tile_index(5, 16, 11, grid.geo)
    assert first is second  # same cached (row, col, valid) tuple, recomputed once
    # A different geometry keys a distinct entry (guarded by geo in the key).
    other_geo = meteofrance_render.GridGeo(**{**grid.geo.__dict__, "x_ul": grid.geo.x_ul + 1.0})
    assert meteofrance_render.tile_index(5, 16, 11, other_geo) is not first


# -- downsampling / anti-aliasing (the mip pyramid) ---------------------------

STORM_LAT, STORM_LON = 48.85, 2.35  # Paris
FINE_SCALE = 500.0  # the real LAME_D_EAU cell size
FINE_SIZE = 1024  # big enough that a pyramid actually gets built


def _fine_grid_with_storm(raw_value=RAW_3MMH, half_width=4):
    """A 500 m grid (like production) holding one compact storm over Paris."""
    _, meta = build_odim(xsize=FINE_SIZE, ysize=FINE_SIZE, xscale=FINE_SCALE, yscale=FINE_SCALE)
    row, col = _cell_of(STORM_LAT, STORM_LON, meta)
    data = np.full((FINE_SIZE, FINE_SIZE), UNDETECT, dtype=np.uint16)
    data[row - half_width : row + half_width, col - half_width : col + half_width] = raw_value
    h5_bytes, _ = build_odim(
        data=data, xsize=FINE_SIZE, ysize=FINE_SIZE, xscale=FINE_SCALE, yscale=FINE_SCALE
    )
    return parse_grid(h5_bytes)


def _rain_levels(grid):
    """The grid's memoised rain mip pyramid, as the render pipeline builds it."""
    return meteofrance_render._rain_layer(grid).levels


def test_pyramid_reduces_until_below_one_tile():
    grid = _fine_grid_with_storm()
    levels = _rain_levels(grid)
    assert [lv[0].shape for lv in levels] == [(1024, 1024), (512, 512), (256, 256)]
    # Reduction halves the scale-space resolution but must not move the UL corner,
    # or every coarse level would be spatially offset from the native one.
    for field_arr, geo in levels:
        assert (geo.x_ul, geo.y_ul) == (grid.geo.x_ul, grid.geo.y_ul)
        assert geo.xsize == field_arr.shape[1]
        assert geo.xscale == FINE_SCALE * (FINE_SIZE / field_arr.shape[1])


def test_pyramid_is_memoised_per_grid():
    """Built once per frame, then shared by all 62 tile renders."""
    grid = _fine_grid_with_storm()
    assert _rain_levels(grid) is _rain_levels(grid)


def test_mip_level_coarsens_as_zoom_decreases():
    grid = _fine_grid_with_storm()
    n_levels = len(_rain_levels(grid))
    _, yt, _, _ = _tile_of(STORM_LAT, STORM_LON, 7)
    chosen = [
        meteofrance_render.mip_level(z, _tile_of(STORM_LAT, STORM_LON, z)[1], grid.geo, n_levels)
        for z in range(3, 8)
    ]
    assert chosen == sorted(chosen, reverse=True)  # coarser level at lower zoom
    assert chosen[-1] == 0  # z7: ~840 m/px vs a 500 m grid ⇒ sample it natively
    assert chosen[0] == n_levels - 1  # z3: ~13.5 km/px ⇒ the coarsest level available
    assert meteofrance_render.mip_level(7, yt, grid.geo, n_levels) == 0


def test_compact_storm_survives_every_zoom():
    """A sub-pixel storm must not vanish as the user zooms out (aliasing regression).

    At z3 one tile pixel spans ~13.5 km, so point-sampling the 500 m grid reads one
    cell in ~730 and misses this 4 km storm almost surely. Reducing by maximum first
    keeps it visible at every zoom — which the storm-alert overview depends on.
    """
    grid = _fine_grid_with_storm()
    for z in range(3, 8):
        xt, yt, _, _ = _tile_of(STORM_LAT, STORM_LON, z)
        png = render_tile(grid, z, xt, yt)
        assert png is not None, f"storm disappeared at zoom {z}"
        alpha = np.asarray(Image.open(io.BytesIO(png)))[..., 3]
        assert (alpha > 0).any(), f"storm rendered fully transparent at zoom {z}"


def test_reduce2_takes_maximum_and_ignores_nodata():
    field_arr = np.array(
        [
            [1.0, 5.0, np.nan, np.nan],
            [2.0, 3.0, np.nan, 7.0],
            [0.0, 0.0, np.nan, np.nan],
            [0.0, 0.0, np.nan, np.nan],
        ],
        dtype=np.float32,
    )
    out = meteofrance_render._reduce2(field_arr)
    assert out[0, 0] == 5.0  # max of the block, not its mean
    assert out[0, 1] == 7.0  # NaN cells are skipped, the one real value wins
    assert out[1, 0] == 0.0
    assert np.isnan(out[1, 1])  # an all-NaN block stays "no coverage"


def test_rate_field_separates_undetect_from_nodata():
    data = np.array([[RAW_3MMH, UNDETECT], [NODATA, 0]], dtype=np.uint16)
    grid = parse_grid(build_odim(data=data, xsize=2, ysize=2)[0])
    rate = meteofrance_render.rate_field(grid)
    assert rate[0, 0] == pytest.approx(3.0)
    assert rate[0, 1] == 0.0  # measured dry: a real zero that must dilute nothing away
    assert np.isnan(rate[1, 0])  # no coverage: excluded from aggregation entirely
    assert rate[1, 1] == 0.0


# -- output smoothing ---------------------------------------------------------


def test_blur_zero_sigma_is_identity():
    field_arr = np.array([[0.0, 1.0], [2.0, 3.0]])
    assert meteofrance_render._gaussian_blur(field_arr, 0) is field_arr


def test_blur_spreads_into_dry_ground_but_not_into_nodata():
    field_arr = np.zeros((9, 9))
    field_arr[4, 4] = 10.0
    field_arr[:, 7:] = np.nan  # a "no radar coverage" band

    out = meteofrance_render._gaussian_blur(field_arr, 1.0)
    assert out[4, 4] < 10.0  # the peak is spread out
    assert out[4, 5] > 0.0  # ...into measured-dry neighbours, giving the soft fringe
    assert np.isnan(out[:, 7:]).all()  # never invents data outside coverage
    # Weights are renormalised over the valid neighbours, so totals near a coverage
    # edge drift slightly upward (the alternative — treating no-coverage as dry —
    # would carve fake dry fringes along every domain boundary). What must hold is
    # that smoothing never reports an intensity nobody measured.
    assert np.nanmax(out) <= field_arr[4, 4]


def test_blur_conserves_rain_away_from_coverage_edges():
    field_arr = np.zeros((15, 15))
    field_arr[7, 7] = 10.0
    out = meteofrance_render._gaussian_blur(field_arr, 1.0)
    assert out.sum() == pytest.approx(10.0, rel=1e-6)
    assert out[7, 7] == pytest.approx(out[7, 7])  # symmetric about the peak
    assert out[6, 7] == pytest.approx(out[8, 7])
    assert out[7, 6] == pytest.approx(out[7, 8])
