"""Météo-France radar grids → Web-Mercator PNG tile rendering.

Only :func:`parse_grid` is ODIM-specific; everything downstream is parametrised on a
:class:`Lut`, so the BUFR-decoded reflectivity grid from
:mod:`radar.providers.bufr_decode` runs through the same sampling, pyramid, masking
and smoothing code as the rain.

Pure functions + process-wide caches; no HTTP, no Django models. The pipeline:

    parse_grid(h5_bytes) -> Grid          decode the ODIM composite (h5py)
    tile_index(z, x, y, geo) -> indices   Web-Mercator pixels -> grid cells (pyproj)
    render_tile(grid, z, x, y) -> bytes?  sample + colour-map -> PNG (Pillow)
    render_frame(grid) -> {(z,x,y): png?} the whole 62-tile matrix

and, when the reflectivity wash is enabled:

    render_composite_frame(grid, wash) -> {(z,x,y): png?}  wash under rain, flattened

The reprojection is a plain vectorized coordinate transform (the grid is regular
with a PROJ string in its metadata) — no GDAL. The per-tile index arrays and
the pyproj transformer are cached process-wide; the grid geometry is fixed, so each
tile's mapping is computed once ever (62 entries). CPU-bound work stays sync here;
the provider runs the whole decode-and-render in ``asyncio.to_thread``.
"""

from __future__ import annotations

import gzip
import io
import math
import time
from dataclasses import dataclass
from dataclasses import field

import numpy as np
import pyproj
from django.conf import settings

from radar import tiles

TILE_PX = 256  # output tile edge in pixels

# Earth circumference at the equator (m) — Web-Mercator pixel-size maths.
_EQUATOR_M = 40075016.686

# Output-space smoothing, in tile pixels. The composite is a 500 m step function, so
# an unsmoothed render is visibly blocky next to RainViewer's. Chosen by sweeping
# against real RainViewer tiles (scripts/sigma_sweep.py): ~1.0 removes the blockiness
# and gives the soft low-intensity fringe, while >=1.8 melts cells into featureless
# blobs without meaningfully closing the residual coverage gap — LAME_D_EAU is a
# 0.01 mm accumulation whose smallest quantum is already 0.12 mm/h, so the light
# drizzle and virga a reflectivity field shows are simply absent here, and no blur
# invents them. That gap is what WASH_LUT below addresses.
_BLUR_SIGMA = 1.0

# 5-minute accumulation (mm) -> hourly rate (mm/h): *(60/5) = *12.
_FIVE_MIN_TO_HOURLY = 12.0

# Colour LUT anchored on rain rate (mm/h), matching the RainViewer "Universal
# Blue" palette (scheme 2).  dBZ→mm/h converted via Marshall-Palmer
# (Z = 200·R¹·⁶).  Linear-interpolated between stops (np.interp); alpha 0
# below the first stop; the last colour holds at ≥420 mm/h.
#
# Key visual transitions from the RainViewer palette:
#   dBZ  -8 (0.01 mm/h)  first visible — warm grey, minimal alpha (drizzle)
#   dBZ  -2 (0.03 mm/h)  warm grey, low alpha
#   dBZ   5 (0.08 mm/h)  beige-brown
#   dBZ  10 (0.15 mm/h)  cream
#   dBZ  15 (0.32 mm/h)  cyan  ← beige→blue boundary
#   dBZ  20 (0.65 mm/h)  medium blue
#   dBZ  25 (1.33 mm/h)  blue
#   dBZ  30 (2.73 mm/h)  dark blue
#   dBZ  35 (5.6  mm/h)  yellow ← blue→warm boundary
#   dBZ  40 (11.5 mm/h)  orange
#   dBZ  45 (23.7 mm/h)  red-orange
#   dBZ  50 (48.6 mm/h)  red
#   dBZ  55 (100  mm/h)  magenta
#   dBZ  65 (420  mm/h)  white
_LUT_RATES = (
    0.01,
    0.03,
    0.05,
    0.08,
    0.15,
    0.24,
    0.32,
    0.65,
    1.33,
    2.73,
    5.6,
    11.5,
    23.7,
    48.6,
    100.0,
    200.0,
    420.0,
)
_LUT_RGBA = (
    (0x7C, 0x75, 0x65, 0x1A),  #  0.01 mm/h  ~dBZ -8  warm grey, minimal alpha
    (0x7C, 0x75, 0x65, 0x3E),  #  0.03 mm/h  ~dBZ -2  warm grey
    (0x8B, 0x82, 0x6D, 0x59),  #  0.05 mm/h  ~dBZ  3  light brown
    (0x92, 0x88, 0x71, 0x64),  #  0.08 mm/h  ~dBZ  5  beige-brown
    (0xCE, 0xC0, 0x87, 0x96),  #  0.15 mm/h  ~dBZ 10  cream
    (0xDE, 0xD0, 0x97, 0xBE),  #  0.24 mm/h  ~dBZ 14  light beige
    (0x88, 0xDD, 0xEE, 0xFF),  #  0.32 mm/h  ~dBZ 15  cyan
    (0x00, 0xA3, 0xE0, 0xFF),  #  0.65 mm/h  ~dBZ 20  medium blue
    (0x00, 0x77, 0xAA, 0xFF),  #  1.33 mm/h  ~dBZ 25  blue
    (0x00, 0x55, 0x88, 0xFF),  #  2.73 mm/h  ~dBZ 30  dark blue
    (0xFF, 0xEE, 0x00, 0xFF),  #  5.6  mm/h  ~dBZ 35  yellow
    (0xFF, 0xAA, 0x00, 0xFF),  # 11.5  mm/h  ~dBZ 40  orange
    (0xFF, 0x44, 0x00, 0xFF),  # 23.7  mm/h  ~dBZ 45  red-orange
    (0xC1, 0x00, 0x00, 0xFF),  # 48.6  mm/h  ~dBZ 50  red
    (0xFF, 0xAA, 0xFF, 0xFF),  # 100   mm/h  ~dBZ 55  magenta
    (0xFF, 0x77, 0xFF, 0xFF),  # 200   mm/h  ~dBZ 60  pink
    (0xFF, 0xFF, 0xFF, 0xFF),  # 420   mm/h  ~dBZ 65  white
)
_MIN_RATE = _LUT_RATES[0]  # below this: fully transparent

# -- the reflectivity wash (signed off July 2026) ------------------------------
#
# REFLECTIVITE renders *under* the rain as a "wet atmosphere" wash: moisture aloft
# that may be light rain or only humidity, which LAME_D_EAU's 0.12 mm/h floor records
# as flat zero. Deliberately RainViewer's own low-intensity warm greys/creams, ramping
# in luminance (0x7C -> 0xDE) **and** alpha (0.06 -> 0.68). Movement in both is what
# makes the wash read as information: an earlier candidate ramped only alpha, between
# two near-identical greys, and rendered as one flat tint.
#
# Consequence accepted at sign-off: because these are the rain LUT's own first stops,
# the composite reads as one continuous ramp from haze through drizzle into rain rather
# than as two separable signals. Never describe the wash in UI copy as a distinct
# measurement, and never let it drive an alert.
_WASH_DBZ = (-6.0, 0.0, 6.0, 12.0, 20.0)  # first stop doubles as the transparency floor
_WASH_RGBA = (
    (0x7C, 0x75, 0x65, 15),  #  -6 dBZ  warm grey, barely there
    (0x8B, 0x82, 0x6D, 46),  #   0 dBZ  light brown
    (0x92, 0x88, 0x71, 87),  #   6 dBZ  beige-brown
    (0xCE, 0xC0, 0x87, 133),  # 12 dBZ  cream
    (0xDE, 0xD0, 0x97, 173),  # 20 dBZ  light beige
)

# The wash is smoothed harder than the rain (1.0): the maintainer chose the softer of
# the two candidates, knowingly trading a wider kernel-driven extent for RainViewer's
# blur. Measured against an unblurred render, this sigma paints ~2.8x the area (sigma
# 2.2 gave 1.6-2.2x), so roughly two thirds of what the wash covers is kernel rather
# than echo — its presence is a measurement, its extent is a presentation choice.
_WASH_BLUR_SIGMA = 3.2

# Floor for the log10 back-conversion in _smooth: -120 dBZ, far below the mosaic's
# own -40 no-echo floor, so it only guards against log10(0) rather than clipping data.
_MIN_LINEAR_Z = 1e-12


class RenderError(Exception):
    """A malformed ODIM grid or a render failure. Provider chains it onward."""


@dataclass(frozen=True)
class GridGeo:
    """The fixed grid geometry — hashable, so it keys the per-tile index cache."""

    projdef: str
    xscale: float
    yscale: float
    xsize: int
    ysize: int
    x_ul: float  # projected x of the west (left) edge — the UL corner
    y_ul: float  # projected y of the north (top) edge — the UL corner (row 0)


@dataclass
class Grid:
    data: np.ndarray  # uint16 (ysize, xsize); row 0 is the NORTH edge
    gain: float
    offset: float
    nodata: int
    undetect: int
    geo: GridGeo
    # Lazily-built mip pyramid of rain-rate fields (see _pyramid). Level 0 is the
    # native 500 m grid in mm/h; level k is block-reduced by 2**k. Built once per
    # frame and shared by all 62 tile renders.
    _levels: list[tuple[np.ndarray, GridGeo]] | None = field(default=None, repr=False)


# Process-wide caches: pyproj transformers per projdef, index arrays per (z,x,y,geo).
_transformers: dict[str, pyproj.Transformer] = {}
_index_cache: dict[tuple, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}


def _attr_str(attrs, key: str) -> str:
    v = attrs[key]
    return v.decode() if isinstance(v, bytes) else str(v)


def parse_grid(h5_bytes: bytes) -> Grid:
    """Decode an ODIM_H5 LAME_D_EAU composite into a:class:`Grid`.

    Reads ``/dataset1/data1/data`` plus the gain/offset/nodata/undetect on its
    ``what`` group and the projection/geometry on ``/where``. Validates
    ``@quantity == "ACRR"`` and the corner orientation (row 0 = north); anything
    malformed raises :class:`RenderError` (never a bare h5py/OS error).
    """
    import h5py  # noqa: PLC0415 — heavy optional dep, imported lazily

    if h5_bytes[:2] == b"\x1f\x8b":  # some DPRadar products are served gzipped
        try:
            h5_bytes = gzip.decompress(h5_bytes)
        except (OSError, EOFError) as exc:
            msg = f"malformed gzip payload: {type(exc).__name__}: {exc}"
            raise RenderError(msg) from exc

    try:
        with h5py.File(io.BytesIO(h5_bytes), "r") as f:
            where = f["where"].attrs
            data1 = f["dataset1"]["data1"]
            what = data1["what"].attrs
            if _attr_str(what, "quantity") != "ACRR":
                msg = f"unexpected quantity {_attr_str(what, 'quantity')!r} (want ACRR)"
                raise RenderError(msg)  # noqa: TRY301 — validated inside the h5py context
            data = np.asarray(data1["data"][()])
            gain = float(what["gain"])
            offset = float(what["offset"])
            nodata = int(what["nodata"])
            undetect = int(what["undetect"])
            projdef = _attr_str(where, "projdef")
            xscale = float(where["xscale"])
            yscale = float(where["yscale"])
            xsize = int(where["xsize"])
            ysize = int(where["ysize"])
            ul_lat = float(where["UL_lat"])
            ul_lon = float(where["UL_lon"])
            lr_lat = float(where["LR_lat"])
            lr_lon = float(where["LR_lon"])
    except RenderError:
        raise
    except (OSError, KeyError, ValueError, TypeError) as exc:
        msg = f"malformed ODIM grid: {type(exc).__name__}: {exc}"
        raise RenderError(msg) from exc

    if data.shape != (ysize, xsize):
        msg = f"dataset shape {data.shape} != (ysize={ysize}, xsize={xsize})"
        raise RenderError(msg)

    transformer = transformer_for(projdef)
    x_ul, y_ul = transformer.transform(ul_lon, ul_lat)
    x_lr, y_lr = transformer.transform(lr_lon, lr_lat)
    # Verify orientation explicitly (don't assume): the UL corner must be west of
    # and north of the LR corner in the projected CRS, i.e. row 0 is the north edge.
    if not (x_lr > x_ul and y_lr < y_ul):
        msg = "corner orientation unexpected (UL is not north-west of LR)"
        raise RenderError(msg)

    geo = GridGeo(
        projdef=projdef,
        xscale=xscale,
        yscale=yscale,
        xsize=xsize,
        ysize=ysize,
        x_ul=float(x_ul),
        y_ul=float(y_ul),
    )
    return Grid(data=data, gain=gain, offset=offset, nodata=nodata, undetect=undetect, geo=geo)


def transformer_for(projdef: str) -> pyproj.Transformer:
    transformer = _transformers.get(projdef)
    if transformer is None:
        target = pyproj.CRS.from_proj4(projdef)
        transformer = pyproj.Transformer.from_crs("EPSG:4326", target, always_xy=True)
        _transformers[projdef] = transformer
    return transformer


def _tile_lonlat(z: int, x: int, y: int) -> tuple[np.ndarray, np.ndarray]:
    """Longitudes (per column) and latitudes (per row) of the tile's pixel centres."""
    n_pix = TILE_PX * (2**z)
    idx = np.arange(TILE_PX, dtype=np.float64) + 0.5
    gx = x * TILE_PX + idx  # (256,)
    gy = y * TILE_PX + idx  # (256,)
    lon = gx / n_pix * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1.0 - 2.0 * gy / n_pix))))
    return lon, lat


def tile_index(
    z: int,
    x: int,
    y: int,
    geo: GridGeo,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Map a tile's 256x256 pixels to nearest grid (row, col), with a validity mask.

    Cached process-wide keyed by ``(z, x, y, geo)`` — the projection and grid
    geometry are fixed, so each tile's mapping is computed once ever. A geo
    change (different projdef/scale/origin) keys a fresh entry.
    """
    key = (z, x, y, geo)
    cached = _index_cache.get(key)
    if cached is not None:
        return cached

    lon, lat = _tile_lonlat(z, x, y)
    lon2d = np.broadcast_to(lon, (TILE_PX, TILE_PX))
    lat2d = np.broadcast_to(lat[:, None], (TILE_PX, TILE_PX))
    px, py = transformer_for(geo.projdef).transform(lon2d, lat2d)

    # int32, not int64: the cache holds one entry per (tile, geo) and a second product
    # doubles that to ~124 live entries, each 256x256. Grid indices peak in the low
    # thousands, so the wider dtype would cost ~70 MB of resident memory per process
    # (the web container pays it too, on the fallback tile path) for no reach.
    col = np.floor((px - geo.x_ul) / geo.xscale).astype(np.int32)
    row = np.floor((geo.y_ul - py) / geo.yscale).astype(np.int32)  # y_ul is the north edge
    valid = (col >= 0) & (col < geo.xsize) & (row >= 0) & (row < geo.ysize)
    # Clamp out-of-range indices so gather is safe; `valid` masks them out later.
    row = np.where(valid, row, 0)
    col = np.where(valid, col, 0)

    result = (row, col, valid)
    _index_cache[key] = result
    return result


@dataclass(frozen=True)
class Lut:
    """A colour ramp plus the presentation rules for the field it maps.

    Parametrising the pipeline on this is what lets rain (mm/h) and the reflectivity
    wash (dBZ) share all the sampling, pyramid, masking and smoothing code — the two
    layers differ only in their ramp, their blur radius, and the *space* the blur is
    computed in (see :attr:`log_domain`).
    """

    stops: tuple[float, ...]  # ascending field values the colours are anchored at
    r: np.ndarray
    g: np.ndarray
    b: np.ndarray
    a: np.ndarray
    sigma: float  # output-space Gaussian blur, in tile pixels
    log_domain: bool  # True when the field is logarithmic (dBZ) — see _smooth

    @property
    def floor(self) -> float:
        """Below the first stop the layer is fully transparent.

        Derived rather than stored so a ramp and its transparency threshold cannot
        drift apart — a floor above ``stops[0]`` would silently clip the ramp's own
        first colour, and one below it would paint values the ramp never anchors.
        """
        return self.stops[0]


def _build_lut(stops, rgba, sigma: float, *, log_domain: bool) -> Lut:
    return Lut(
        stops=tuple(stops),
        r=np.array([c[0] for c in rgba], dtype=np.float64),
        g=np.array([c[1] for c in rgba], dtype=np.float64),
        b=np.array([c[2] for c in rgba], dtype=np.float64),
        a=np.array([c[3] for c in rgba], dtype=np.float64),
        sigma=sigma,
        log_domain=log_domain,
    )


RAIN_LUT = _build_lut(_LUT_RATES, _LUT_RGBA, _BLUR_SIGMA, log_domain=False)
WASH_LUT = _build_lut(_WASH_DBZ, _WASH_RGBA, _WASH_BLUR_SIGMA, log_domain=True)


def _colorize(values: np.ndarray, visible: np.ndarray, lut: Lut) -> np.ndarray:
    """Colour-map an arbitrary scalar field to RGBA uint8 through ``lut``."""
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    if not visible.any():
        return rgba
    sel = values[visible]
    channels = [np.interp(sel, lut.stops, ch) for ch in (lut.r, lut.g, lut.b, lut.a)]
    rgba[visible] = np.stack(channels, axis=-1).astype(np.uint8)
    return rgba


def rain_color(mm_h: float) -> tuple[int, int, int, int]:
    """Single-rate LUT lookup — the pinned anchor colours, for unit tests."""
    arr = np.array([[mm_h]], dtype=np.float64)
    rgba = _colorize(arr, arr >= RAIN_LUT.floor, RAIN_LUT)
    r, g, b, a = (int(v) for v in rgba[0, 0])
    return r, g, b, a


def rate_field(grid: Grid) -> np.ndarray:
    """The native grid as float32 mm/h, with ``nodata`` as NaN and ``undetect`` as 0.

    The distinction matters for downsampling: ``undetect`` means "the radar looked
    and it is not raining" (a real zero, which must dilute a block average), while
    ``nodata`` means "no radar coverage here" (unknown, and must be excluded from
    the average rather than counted as dry).
    """
    raw = grid.data
    rate = (raw.astype(np.float32) * grid.gain + grid.offset) * _FIVE_MIN_TO_HOURLY
    rate[raw == grid.undetect] = 0.0
    rate[raw == grid.nodata] = np.nan
    return rate


def _reduce2(a: np.ndarray) -> np.ndarray:
    """Block-reduce a field 2x2 -> 1 by NaN-aware maximum (NaN only if all-NaN).

    Maximum, not mean. A zoom-3 pixel aggregates ~730 native cells, and LAME_D_EAU
    is sparse (typically >99% of covered cells are exactly zero), so averaging
    dilutes a convective core into invisibility — a storm literally disappears as the
    user zooms out, which also breaks the overview the storm-alert feature is read
    against. Max keeps the core visible at every zoom; it overstates the *area* of
    intense rain at low zoom, which is acceptable because one pixel there is already
    ~13 km on the ground. Empirically this also tracks RainViewer's own pyramid,
    which keeps cores at z3 where a mean-reduced field shows almost nothing.
    """
    h, w = a.shape
    h2, w2 = h // 2, w // 2  # an odd trailing row/column is dropped
    block = a[: h2 * 2, : w2 * 2].reshape(h2, 2, w2, 2)
    ok = ~np.isnan(block)
    filled = np.where(ok, block, -np.inf).max(axis=(1, 3))
    return np.where(np.isfinite(filled), filled, np.nan).astype(np.float32)


def build_pyramid(values: np.ndarray, geo: GridGeo) -> list[tuple[np.ndarray, GridGeo]]:
    """Mip pyramid of a field, by NaN-aware maximum.

    Levels are generated until the grid is smaller than one tile, which covers every
    zoom in the matrix. The UL corner is invariant under reduction (odd edges are
    cropped from the south/east), so only the scale and size change. Max is
    monotonic, so this is equally correct for a logarithmic field (dBZ) — the maximum
    in dB is the maximum in linear Z.
    """
    levels = [(values, geo)]
    while min(levels[-1][0].shape) > TILE_PX:
        prev_field, prev_geo = levels[-1]
        reduced = _reduce2(prev_field)
        levels.append(
            (
                reduced,
                GridGeo(
                    projdef=prev_geo.projdef,
                    xscale=prev_geo.xscale * 2,
                    yscale=prev_geo.yscale * 2,
                    xsize=reduced.shape[1],
                    ysize=reduced.shape[0],
                    x_ul=prev_geo.x_ul,
                    y_ul=prev_geo.y_ul,
                ),
            )
        )
    return levels


def _layer(src, values_fn, lut: Lut) -> _Layer:
    """Build a renderable layer, memoising the pyramid on the decoded grid.

    ``src`` is a ``Grid`` or a ``bufr_decode.ReflectivityGrid`` — both carry a
    ``geo`` and a ``_levels`` slot, which is all this needs. The memo is what keeps
    the pyramid to once per frame rather than once per each of the 62 tiles, so
    ``values_fn`` stays lazy: it allocates the full native field and must not run on
    a cache hit.
    """
    if src._levels is None:  # noqa: SLF001 — the memo slot exists for exactly this
        src._levels = build_pyramid(values_fn(), src.geo)  # noqa: SLF001
    return _Layer(levels=src._levels, lut=lut)  # noqa: SLF001


def _tile_centre_lat(z: int, y: int) -> float:
    """Latitude of the tile's horizontal centre line (for its ground pixel size)."""
    n = 2.0**z
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 0.5) / n))))


def mip_level(z: int, y: int, geo: GridGeo, n_levels: int) -> int:
    """Pick the pyramid level whose cells are at most one output pixel across.

    A zoom-3 tile pixel spans ~13.5 km on the ground while the native grid is 500 m,
    so point-sampling the native grid would read 1 cell in ~730 and alias badly —
    scattered storms break into speckle or vanish entirely. Reducing first means
    every source cell contributes. The residual ratio is always < 2, so the final
    nearest-neighbour gather is well-conditioned.
    """
    m_per_px = _EQUATOR_M / (TILE_PX * 2**z) * math.cos(math.radians(_tile_centre_lat(z, y)))
    ratio = m_per_px / geo.xscale
    if ratio <= 1.0:
        return 0
    return min(int(math.log2(ratio)), n_levels - 1)


def _gaussian_blur(field: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur with NaN treated as absent (normalised convolution).

    Implemented as a weighted sum of shifted copies — a handful of numpy slices per
    axis, so no scipy. NaN cells (outside radar coverage) neither contribute to nor
    receive weight, so the blur never invents data across the domain edge; genuine
    zeros (``undetect``) do participate, which is what produces the soft fringe
    around a cell instead of a hard 500 m step.
    """
    if sigma <= 0:
        return field
    radius = max(1, math.ceil(3.0 * sigma))
    offsets = np.arange(-radius, radius + 1)
    weights = np.exp(-(offsets**2) / (2.0 * sigma * sigma))

    valid = ~np.isnan(field)
    num = np.where(valid, field, 0.0).astype(np.float64)
    den = valid.astype(np.float64)
    for axis in (0, 1):
        num = _convolve1d(num, weights, axis)
        den = _convolve1d(den, weights, axis)
    smoothed = np.where(den > 1e-6, num / np.maximum(den, 1e-6), np.nan)  # noqa: PLR2004
    # Re-apply the input mask: a normalised convolution would otherwise assign a
    # value to any no-coverage cell within one kernel radius of real data, smearing
    # rain across the domain edge and across the holes left by out-of-service radars
    # (this composite routinely reports several in ``how/missing_nodes``).
    return np.where(valid, smoothed, np.nan)


def _convolve1d(a: np.ndarray, weights: np.ndarray, axis: int) -> np.ndarray:
    """1-D convolution along ``axis`` by summing zero-padded shifted copies."""
    radius = len(weights) // 2
    pad = [(0, 0), (0, 0)]
    pad[axis] = (radius, radius)
    padded = np.pad(a, pad, mode="constant", constant_values=0.0)
    out = np.zeros_like(a, dtype=np.float64)
    # One reused scratch buffer instead of a fresh tile-sized temporary per tap: the
    # wash's sigma 3.2 means 21 taps, run twice per axis (value + weight) for each of
    # 62 tiles, so the naive form churns thousands of arrays per frame.
    scratch = np.empty_like(out)
    n = a.shape[axis]
    for i, w in enumerate(weights):
        sl = [slice(None), slice(None)]
        sl[axis] = slice(i, i + n)
        np.multiply(padded[tuple(sl)], w, out=scratch)
        out += scratch
    return out


def _smooth(field: np.ndarray, lut: Lut) -> np.ndarray:
    """Blur a sampled field, in the space its units actually live in.

    For a linear field (mm/h) this is a plain normalised convolution. For a
    **logarithmic** one it must not be: dBZ's "no echo" floor is -40, so averaging dB
    directly drags every echo toward that floor — a lone 20 dBZ cell blurred at sigma
    1.6 lands at -36.3 dBZ *at its own centre* and vanishes under any threshold. Only
    the interiors of large solid blocks survive, which renders as flat, hard-edged
    patches with no gradient at all.

    Reflectivity is therefore converted to linear Z (``Z = 10**(dBZ/10)``), blurred,
    and converted back. The same cell then holds 7.9 dBZ and decays smoothly through
    4.5, 0.3, -5.6, -13.3 over five pixels — the soft skirt the wash is meant to have.
    """
    if lut.sigma <= 0:
        return field
    if not lut.log_domain:
        return _gaussian_blur(field, lut.sigma)
    linear = np.where(np.isnan(field), np.nan, np.power(10.0, field / 10.0))
    linear = _gaussian_blur(linear, lut.sigma)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 10.0 * np.log10(np.maximum(linear, _MIN_LINEAR_Z))


@dataclass(frozen=True)
class _Layer:
    """One renderable field: its mip pyramid and the ramp that colours it.

    Level 0 carries the native geometry, so mip selection reads it from there rather
    than storing a second copy that a caller could pass inconsistently.
    """

    levels: list[tuple[np.ndarray, GridGeo]]
    lut: Lut


def _render_layer(layer: _Layer, z: int, x: int, y: int) -> np.ndarray:
    """Sample, smooth and colour-map one layer into an RGBA array for one tile."""
    levels, lut = layer.levels, layer.lut
    level = mip_level(z, y, levels[0][1], len(levels))
    values, geo = levels[level]
    row, col, valid = tile_index(z, x, y, geo)
    sampled = np.where(valid, values[row, col].astype(np.float64), np.nan)
    sampled = _smooth(sampled, lut)
    visible = valid & ~np.isnan(sampled) & (sampled >= lut.floor)
    return _colorize(np.nan_to_num(sampled), visible, lut)


def _alpha_over(over: np.ndarray, under: np.ndarray) -> np.ndarray:
    """Straight source-over compositing of two RGBA uint8 arrays.

    Rain goes *over* the wash, so wherever LAME_D_EAU reports rain the rider sees rain
    colours and the wash shows only in the surrounding moist area.
    """
    if not under[..., 3].any():
        return over
    if not over[..., 3].any():
        return under
    o = over.astype(np.float64) / 255.0
    u = under.astype(np.float64) / 255.0
    oa, ua = o[..., 3:4], u[..., 3:4]
    out_a = oa + ua * (1.0 - oa)
    safe = np.where(out_a > 0.0, out_a, 1.0)
    out_rgb = (o[..., :3] * oa + u[..., :3] * ua * (1.0 - oa)) / safe
    return (np.concatenate([out_rgb, out_a], axis=-1) * 255.0).round().astype(np.uint8)


def _encode_png(rgba: np.ndarray, acc: dict[str, float] | None = None) -> bytes | None:
    """PNG-encode an RGBA tile, or ``None`` when nothing is visible (sparse archive)."""
    if not rgba[..., 3].any():
        return None
    from PIL import Image  # noqa: PLC0415 — heavy optional dep, imported lazily

    t0 = time.perf_counter()
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=False)
    if acc is not None:
        acc["encode"] += time.perf_counter() - t0
    return buf.getvalue()


def render_tile(grid: Grid, z: int, x: int, y: int) -> bytes | None:
    """Render one rain tile to PNG bytes, or ``None`` for a fully-transparent tile."""
    return _encode_png(_render_layer(_rain_layer(grid), z, x, y))


def _rain_layer(grid: Grid) -> _Layer:
    return _layer(grid, lambda: rate_field(grid), RAIN_LUT)


def render_frame(grid: Grid) -> dict[tuple[int, int, int], bytes | None]:
    """Render the whole computed tile matrix (sync, CPU-bound — provider threads it)."""
    return render_composite_frame(grid, None)


def render_composite_frame(
    grid: Grid,
    wash,
    *,
    phases: dict[str, float] | None = None,
) -> dict[tuple[int, int, int], bytes | None]:
    """Render the matrix, with the reflectivity wash composited under the rain.

    ``wash`` is a ``bufr_decode.ReflectivityGrid`` (typed loosely to keep this module
    free of a BUFR import). ``None`` renders rain alone, which is what makes a
    reflectivity failure a best-effort miss rather than a lost frame — and because
    that is the *same* code path with a one-layer stack rather than a parallel one,
    the "rain-only output is unchanged" guarantee is structural rather than asserted.

    A tile is stored unless **both** products are empty there, so the composite is
    less sparse than rain alone — bounded by the 62-tile-per-frame cap.

    ``phases`` is an out-parameter: pass a dict and it is filled with per-stage
    seconds so the caller can log where a frame's CPU went. The ``wash_*`` keys are
    the point — they are the wash's *marginal* cost, which no other signal exposes
    (the poll's ``duration_ms`` bundles downloads, render and disk writes for every
    frame it archived). Timing only ever reads the clock; it never changes what is
    rendered, so the rain-only guarantee above is unaffected.
    """
    acc = phases if phases is not None else {}
    acc.setdefault("wash_px", 0.0)
    acc.setdefault("encode", 0.0)

    t0 = time.perf_counter()
    layers = [_rain_layer(grid)]
    t1 = time.perf_counter()
    if wash is not None:
        # Under the rain: wherever LAME_D_EAU reports rain the rider sees rain colours,
        # and the wash shows only in the surrounding moist area.
        layers.append(_layer(wash, lambda: wash.field, WASH_LUT))
    t2 = time.perf_counter()
    acc["rain_pyramid"] = t1 - t0
    acc["wash_pyramid"] = t2 - t1

    rendered = {
        (z, x, y): _encode_png(_flatten(layers, z, x, y, acc), acc) for (z, x, y) in _matrix()
    }
    acc["tiles"] = time.perf_counter() - t2
    return rendered


def _flatten(
    layers: list[_Layer],
    z: int,
    x: int,
    y: int,
    acc: dict[str, float] | None = None,
) -> np.ndarray:
    """Render each layer for one tile and composite them, first on top."""
    over = _render_layer(layers[0], z, x, y)
    if len(layers) == 1:  # rain-only: not even a clock read on the degraded path
        return over
    t0 = time.perf_counter()
    for layer in layers[1:]:
        over = _alpha_over(over, _render_layer(layer, z, x, y))
    if acc is not None:
        acc["wash_px"] += time.perf_counter() - t0
    return over


def _matrix():
    return tiles.tile_matrix(
        tuple(settings.RADAR_BBOX),
        settings.RADAR_ZOOM_MIN,
        settings.RADAR_ZOOM_MAX,
    )
