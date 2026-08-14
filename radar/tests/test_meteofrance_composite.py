"""The reflectivity wash and the rain-over-wash composite.

Covers three things the design depends on:

* the **linear-Z blur**, which is where a real bug lived — see
  :func:`test_log_domain_blur_preserves_an_isolated_echo`;
* the signed-off wash ramp, pinned so a palette edit is deliberate;
* compositing semantics, including that a wash failure degrades to byte-identical
  rain-only output rather than costing the frame.
"""

from __future__ import annotations

import numpy as np
import pytest

from radar.providers import meteofrance_render as mfr
from radar.providers.bufr_decode import ReflectivityGrid

from .test_meteofrance_render import build_odim
from .test_meteofrance_render import rain_h5

NO_ECHO_DBZ = -40.0  # the mosaic's own "radar looked, nothing there" floor


def _wash_grid(values: np.ndarray, rain: mfr.Grid) -> ReflectivityGrid:
    """A ReflectivityGrid covering exactly the rain grid's extent, at its own mesh.

    Mirrors the real pairing — the two products share a projection and a north-west
    corner but differ in resolution (1 km vs 500 m), so alignment happens in tile
    space rather than grid space.
    """
    geo = rain.geo
    scale_x = geo.xscale * geo.xsize / values.shape[1]
    scale_y = geo.yscale * geo.ysize / values.shape[0]
    wash_geo = mfr.GridGeo(
        projdef=geo.projdef,
        xscale=scale_x,
        yscale=scale_y,
        xsize=values.shape[1],
        ysize=values.shape[0],
        x_ul=geo.x_ul,
        y_ul=geo.y_ul,
    )
    return ReflectivityGrid(field=values.astype(np.float32), geo=wash_geo)


# -- the blur bug --------------------------------------------------------------


def test_log_domain_blur_preserves_an_isolated_echo():
    """Regression: reflectivity must be averaged in linear Z, never in dB.

    dBZ is logarithmic and the mosaic's no-echo floor is -40, so a normalised
    convolution over dB drags every echo toward that floor. Blurring a lone 20 dBZ
    cell in dB space lands it at about -36 dBZ *at its own centre* — below any
    sensible threshold, so it disappears. Only the interiors of large solid blocks
    survive, which renders as flat, hard-edged patches with no gradient at all.

    This was shipped in the prototype and caught by eye ("the wash looks like a
    single colour, and less blurry than RainViewer") before it reached the app.
    """
    field = np.full((41, 41), NO_ECHO_DBZ)
    field[20, 20] = 20.0

    linear = mfr._smooth(field.copy(), mfr.WASH_LUT)
    naive = mfr._gaussian_blur(field.copy(), mfr.WASH_LUT.sigma)

    # The echo survives in linear Z, and is annihilated by the dB-space average.
    assert linear[20, 20] > 0.0
    assert naive[20, 20] < NO_ECHO_DBZ + 10.0

    # ...and it decays smoothly outward rather than cliff-edging to the floor.
    profile = linear[20, 20:28]
    assert np.all(np.diff(profile) < 0.0), "expected a monotonic skirt"
    assert (profile >= mfr.WASH_LUT.floor).sum() >= 3


def test_rain_blur_stays_in_linear_space():
    """The rain LUT must NOT use the log path — mm/h is already linear."""
    assert mfr.RAIN_LUT.log_domain is False
    assert mfr.WASH_LUT.log_domain is True

    field = np.full((21, 21), 0.0)
    field[10, 10] = 5.0
    assert np.allclose(
        mfr._smooth(field.copy(), mfr.RAIN_LUT),
        mfr._gaussian_blur(field.copy(), mfr.RAIN_LUT.sigma),
        equal_nan=True,
    )


def test_smooth_preserves_no_coverage_holes():
    """NaN (no radar coverage) must never be painted over by either blur."""
    field = np.full((21, 21), 10.0)
    field[10, 10] = np.nan
    assert np.isnan(mfr._smooth(field.copy(), mfr.WASH_LUT)[10, 10])


# -- the signed-off ramp -------------------------------------------------------


def test_wash_ramp_is_pinned():
    """The signed-off ramp, as numbers. Changing these is a design decision."""
    assert mfr.WASH_LUT.stops == (-6.0, 0.0, 6.0, 12.0, 20.0)
    assert mfr.WASH_LUT.floor == -6.0
    assert mfr.WASH_LUT.sigma == 3.2
    # Warm greys climbing to cream, with alpha doing the rest.
    assert list(mfr.WASH_LUT.r) == [0x7C, 0x8B, 0x92, 0xCE, 0xDE]
    assert list(mfr.WASH_LUT.a) == [15, 46, 87, 133, 173]


def test_wash_ramp_moves_in_luminance_not_only_alpha():
    """Gradation needs the colour to travel, not just the opacity.

    An earlier candidate ramped between two near-identical greys and rendered as one
    flat tint however far its alpha moved. Guard the property, not the pixels.
    """
    assert mfr.WASH_LUT.r[-1] - mfr.WASH_LUT.r[0] > 0x40
    assert mfr.WASH_LUT.a[-1] - mfr.WASH_LUT.a[0] > 100


def test_wash_is_transparent_below_the_floor():
    values = np.array([[-40.0, -20.0, -6.0, 20.0]])
    visible = values >= mfr.WASH_LUT.floor
    rgba = mfr._colorize(values, visible, mfr.WASH_LUT)
    assert list(rgba[0, :, 3]) == [0, 0, 15, 173]


# -- compositing ---------------------------------------------------------------


def test_alpha_over_puts_rain_on_top():
    """Where rain is opaque the rider sees rain colours, never the wash."""
    rain = np.zeros((1, 1, 4), dtype=np.uint8)
    rain[0, 0] = (255, 0, 0, 255)
    wash = np.zeros((1, 1, 4), dtype=np.uint8)
    wash[0, 0] = (0, 0, 255, 128)
    out = mfr._alpha_over(rain, wash)
    assert tuple(out[0, 0]) == (255, 0, 0, 255)


def test_alpha_over_shows_wash_where_there_is_no_rain():
    rain = np.zeros((1, 1, 4), dtype=np.uint8)
    wash = np.zeros((1, 1, 4), dtype=np.uint8)
    wash[0, 0] = (0x8B, 0x82, 0x6D, 46)
    out = mfr._alpha_over(rain, wash)
    assert tuple(out[0, 0]) == (0x8B, 0x82, 0x6D, 46)


@pytest.mark.parametrize("empty_wash", [True, False])
def test_tile_is_empty_only_when_both_products_are(empty_wash):
    rain = np.zeros((2, 2, 4), dtype=np.uint8)
    wash = np.zeros((2, 2, 4), dtype=np.uint8)
    if not empty_wash:
        wash[0, 0] = (0x8B, 0x82, 0x6D, 46)
    png = mfr._encode_png(mfr._alpha_over(rain, wash))
    assert (png is None) is empty_wash


# -- frame-level behaviour -----------------------------------------------------


def test_no_wash_is_byte_identical_to_the_rain_only_render():
    """Graceful degradation: a reflectivity failure must cost nothing at all.

    This is the flag-off / wash-failed guarantee — the composite path with no wash
    must produce exactly the rain-only bytes, not merely a similar-looking frame.
    """
    h5_bytes = rain_h5()
    grid_a = mfr.parse_grid(h5_bytes)
    grid_b = mfr.parse_grid(h5_bytes)
    assert mfr.render_composite_frame(grid_a, None) == mfr.render_frame(grid_b)


def test_phases_are_reported_without_changing_the_render():
    """The timing out-param must observe the render, never alter it.

    ``wash_pyramid``/``wash_px`` are the reason the hook exists — they are the wash's
    marginal CPU cost, which ``poll_complete.duration_ms`` cannot separate out.
    """
    h5_bytes = rain_h5()

    def render(phases):
        grid = mfr.parse_grid(h5_bytes)
        wash = _wash_grid(np.full((128, 128), 15.0), grid)
        return mfr.render_composite_frame(grid, wash, phases=phases)

    phases: dict[str, float] = {}
    assert render(phases) == render(None)

    assert set(phases) == {"rain_pyramid", "wash_pyramid", "tiles", "wash_px", "encode"}
    assert all(v >= 0.0 for v in phases.values())
    assert phases["wash_px"] > 0.0, "a wash layer must register per-tile time"
    assert phases["tiles"] >= phases["wash_px"]


def test_phases_report_no_wash_cost_when_there_is_no_wash():
    """Rain-only frames attribute *exactly* nothing to the wash.

    Not merely "a small number": the degraded path skips the timing entirely, so the
    flag's cost reads as a true zero rather than as clock noise.
    """
    phases: dict[str, float] = {}
    mfr.render_composite_frame(mfr.parse_grid(rain_h5()), None, phases=phases)
    assert phases["wash_px"] == 0.0


def test_composite_never_loses_a_tile_the_rain_painted():
    """The wash may only add coverage, never mask rain away."""
    h5_bytes = rain_h5()
    rain_only = mfr.render_frame(mfr.parse_grid(h5_bytes))
    grid = mfr.parse_grid(h5_bytes)
    wash = _wash_grid(np.full((128, 128), NO_ECHO_DBZ), grid)  # echo-free wash
    composite = mfr.render_composite_frame(grid, wash)

    painted = [k for k, v in rain_only.items() if v is not None]
    assert painted, "fixture should paint some tiles"
    assert all(composite[k] is not None for k in painted)


def test_wash_adds_coverage_where_there_is_no_rain():
    """A wash-only area must render, which is the whole point of the refactor."""
    dry = np.zeros((256, 256), dtype=np.uint16)  # undetect everywhere ⇒ no rain
    h5_bytes, _ = build_odim(data=dry)
    rain_only = mfr.render_frame(mfr.parse_grid(h5_bytes))
    grid = mfr.parse_grid(h5_bytes)
    wash = _wash_grid(np.full((128, 128), 15.0), grid)  # solid moderate echo
    composite = mfr.render_composite_frame(grid, wash)

    assert all(v is None for v in rain_only.values()), "dry grid should paint nothing"
    assert any(v is not None for v in composite.values())
