"""Météo-France PIAF (GRIB2) → rain-rate grid — the single GRIB quarantine point.

Decodes one ``GetCoverage`` response of the PIAF ``_PT5M`` coverage: a single GRIB2
message carrying the rain forecast for one run and one lead time, on a regular 0.01°
latitude/longitude grid cropped to ``RADAR_BBOX``.

Why this module exists rather than a GRIB library
-------------------------------------------------
The message is about as simple as GRIB2 gets: fixed-layout sections, simple packing at
16 bits, no bitmap. The data section *is* a big-endian ``uint16`` array, so the whole
decode is one ``np.frombuffer`` (~2 ms for the full bbox). ecCodes would bring ~45 MB
of native libraries to do the same, and it would still leave all of the checks below
to us (``specs/forecast-nowcast.md`` §8.8).

Those checks are the point. The service's own metadata is wrong in three places about
what the values are, and it once answered a request with a 200 carrying somebody
else's GRIB (§8.5). So every message must be exactly the template we know — centre,
grid, product, packing — **and** carry the run and valid time we asked for. Any
difference raises :class:`GribDecodeError` rather than decoding plausible-looking rain
for the wrong place or the wrong hour.

What the values are
-------------------
Product template 4.8 with category 1 / parameter 52, which WMO labels "total
precipitation rate, kg m⁻² s⁻¹". The values are not that: they are **millimetres
accumulated over the 5 minutes ending at the valid time** — the same quantity as the
LAME_D_EAU observation, measured against it in Phase 0 (§8.4). The statistical-process
fields agree (accumulation over 5 minutes); the parameter's label does not, so this
module pins the numbers and converts x12 to mm/h, exactly as the observation's
``rate_field`` does.

PIAF has **no notion of "no radar coverage"**: there is no bitmap and every cell is a
number ≥ 0, with model rain where the radars cannot see. Masking to coverage is
:mod:`radar.forecast.render`'s job, not the decoder's.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

import numpy as np

from radar.providers.meteofrance_render import GridGeo


class GribDecodeError(Exception):
    """A malformed or unexpected PIAF message. The caller treats it as a failed fetch."""


@dataclass(frozen=True)
class PiafField:
    """One decoded lead time: the rain rate on PIAF's lat/lon grid."""

    run_ts: int  # UTC epoch of the run (reference time)
    valid_ts: int  # UTC epoch of the end of the 5-minute accumulation window
    rate: np.ndarray  # float32 mm/h, shape (ysize, xsize), row 0 = north edge
    geo: GridGeo


# -- the message we expect ----------------------------------------------------

_MAGIC = b"GRIB"
_END = b"7777"
_EDITION = 2
_DISCIPLINE = 0  # meteorological products
_CENTRE = 84  # Toulouse / Météo-France
_LOCAL_TABLE_VERSION = 0  # no local tables in use
_REF_TIME_START_OF_FORECAST = 1
_IDENTIFICATION_SECTION_LEN = 21
_SECTION_ORDER = (1, 3, 4, 5, 6, 7)  # no local-use section 2

_GRID_TEMPLATE = 0  # regular latitude/longitude
_GRID_SECTION_LEN = 72
_EARTH_SPHERE_6371229 = 6  # shape of the earth: sphere, R = 6 371 229 m
_BASIC_ANGLE_DEFAULT = 0  # angles in micro-degrees
_INCREMENTS_GIVEN = 0x30  # resolution flags: i and j increments both given
_SCAN_WE_NS = 0  # +i west → east, -j north → south, rows consecutive

_PRODUCT_TEMPLATE = 8  # statistically processed over a time interval
_PRODUCT_SECTION_LEN = 58  # template 4.8 with exactly one time range
_CATEGORY_MOISTURE = 1
_PARAMETER_PRECIP = 52  # labelled a rate; carries 5-minute accumulations (module doc)
_PROCESS_FORECAST = 2
_UNIT_MINUTE = 0
_SURFACE_GROUND = 1
_STAT_ACCUMULATION = 1
_ACCUMULATION_MINUTES = 5
_ACCUMULATION_S = _ACCUMULATION_MINUTES * 60

_DATA_TEMPLATE_SIMPLE = 0
_DATA_SECTION_LEN = 21
_ORIGINAL_FLOAT = 0
_BITMAP_SECTION_LEN = 6
_NO_BITMAP = 255
_BITS_CONSTANT = 0  # legal GRIB2 for a constant field: no data bytes at all
_BITS_PACKED = 16  # the only width Météo-France has been seen to emit

_MIN_SECTION_LEN = 5
_HEADER_LEN = 16  # section 0
_MIN_MESSAGE_LEN = _HEADER_LEN + len(_END)

_MICRO = 1_000_000  # GRIB2 angles are micro-degrees
_FULL_CIRCLE = 360 * _MICRO
_STEP = 10_000  # 0.01° in micro-degrees
_STEP_DEG = _STEP / _MICRO

# PIAF's grid on its own sphere. Read as WGS84 lon/lat (the identity transform, the
# convention every GRIB consumer uses), it lines up with LAME_D_EAU to within a cell
# (Phase 0, §8.6).
PROJDEF = "+proj=longlat +R=6371229 +no_defs"

_FIVE_MIN_TO_HOURLY = 12.0


def _signed(raw: bytes) -> int:
    """GRIB2 sign-and-magnitude integer: the top bit is the sign, not two's complement."""
    value = int.from_bytes(raw, "big")
    sign_bit = 1 << (len(raw) * 8 - 1)
    return -(value & ~sign_bit) if value & sign_bit else value


def _unsigned(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


def _epoch(raw: bytes) -> int:
    """Seven octets: year (2), month, day, hour, minute, second → UTC epoch."""
    year, month, day, hour, minute, second = struct.unpack(">HBBBBB", raw)
    try:
        return int(datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp())
    except ValueError as exc:
        msg = f"invalid timestamp {year}-{month}-{day} {hour}:{minute}:{second}"
        raise GribDecodeError(msg) from exc


def _expect(name: str, actual, expected) -> None:
    if actual != expected:
        msg = f"{name} is {actual!r}, expected {expected!r}"
        raise GribDecodeError(msg)


# -- geometry -----------------------------------------------------------------


@dataclass(frozen=True)
class _GridSpec:
    """The grid a ``RADAR_BBOX`` crop must have, in GRIB2's own units."""

    ni: int
    nj: int
    la1: int  # north edge, micro-degrees
    lo1: int  # west edge, micro-degrees, 0-360 as GRIB2 encodes it
    la2: int  # south edge
    lo2: int  # east edge


def _grid_spec(bbox) -> _GridSpec:
    """The expected grid for a ``(south, north, west, east)`` bbox on the 0.01° lattice.

    The service subsets inclusively: ``lat(41.2,51.5)`` yields 1031 rows. A bbox that
    is not on the lattice is a configuration error, not an upstream one.
    """
    south, north, west, east = (round(v * _MICRO) for v in bbox)
    span_i, span_j = east - west, north - south
    if span_i <= 0 or span_j <= 0 or span_i % _STEP or span_j % _STEP:
        msg = f"bbox {tuple(bbox)} is not a positive multiple of 0.01°"
        raise ValueError(msg)
    return _GridSpec(
        ni=span_i // _STEP + 1,
        nj=span_j // _STEP + 1,
        la1=north,
        lo1=west % _FULL_CIRCLE,
        la2=south,
        lo2=east % _FULL_CIRCLE,
    )


def grid_geo(bbox) -> GridGeo:
    """The :class:`GridGeo` of a PIAF crop of ``bbox``.

    GRIB2 gives the *centre* of the first cell; ``GridGeo`` wants the upper-left
    *edge*, half a cell further north-west. Getting this wrong shifts the whole
    forecast by ~500 m against the observation.
    """
    spec = _grid_spec(bbox)
    half = _STEP_DEG / 2
    west = bbox[2]
    north = bbox[1]
    return GridGeo(
        projdef=PROJDEF,
        xscale=_STEP_DEG,
        yscale=_STEP_DEG,
        xsize=spec.ni,
        ysize=spec.nj,
        x_ul=round(west - half, 6),
        y_ul=round(north + half, 6),
    )


# -- sections -----------------------------------------------------------------


def _sections(message: bytes) -> dict[int, bytes]:
    """Validate the envelope and split the message into sections 1, 3, 4, 5, 6, 7."""
    if len(message) < _MIN_MESSAGE_LEN or message[:4] != _MAGIC:
        msg = "not a GRIB message"
        raise GribDecodeError(msg)
    _expect("edition", message[7], _EDITION)
    _expect("discipline", message[6], _DISCIPLINE)
    total = _unsigned(message[8:16])
    # Exactly one message, whole: a truncated body or a second message appended would
    # otherwise decode as far as it goes.
    _expect("message length", total, len(message))
    if message[-4:] != _END:
        msg = "missing end marker"
        raise GribDecodeError(msg)

    end = total - len(_END)
    offset = _HEADER_LEN
    sections: dict[int, bytes] = {}
    order: list[int] = []
    while offset < end:
        if offset + _MIN_SECTION_LEN > end:
            msg = f"section header overruns the message at byte {offset}"
            raise GribDecodeError(msg)
        length = _unsigned(message[offset : offset + 4])
        number = message[offset + 4]
        if length < _MIN_SECTION_LEN or offset + length > end:
            msg = f"section {number} has an invalid length {length}"
            raise GribDecodeError(msg)
        order.append(number)
        sections[number] = message[offset : offset + length]
        offset += length
    _expect("section order", tuple(order), _SECTION_ORDER)
    return sections


def _check_identification(s1: bytes, run_ts: int) -> None:
    # Checked first, like every fixed-layout section: a short one would otherwise fail
    # in the slicing below with a struct.error instead of a GribDecodeError.
    _expect("identification section length", len(s1), _IDENTIFICATION_SECTION_LEN)
    _expect("originating centre", _unsigned(s1[5:7]), _CENTRE)
    _expect("local table version", s1[10], _LOCAL_TABLE_VERSION)
    _expect("reference time significance", s1[11], _REF_TIME_START_OF_FORECAST)
    _expect("run (reference time)", _epoch(s1[12:19]), run_ts)


def _check_grid(s3: bytes, spec: _GridSpec) -> None:
    _expect("grid section length", len(s3), _GRID_SECTION_LEN)
    _expect("grid template", _unsigned(s3[12:14]), _GRID_TEMPLATE)
    _expect("number of points", _unsigned(s3[6:10]), spec.ni * spec.nj)
    _expect("shape of the earth", s3[14], _EARTH_SPHERE_6371229)
    _expect("Ni", _unsigned(s3[30:34]), spec.ni)
    _expect("Nj", _unsigned(s3[34:38]), spec.nj)
    _expect("basic angle", _unsigned(s3[38:42]), _BASIC_ANGLE_DEFAULT)
    _expect("La1", _signed(s3[46:50]), spec.la1)
    _expect("Lo1", _unsigned(s3[50:54]), spec.lo1)
    _expect("resolution flags", s3[54], _INCREMENTS_GIVEN)
    _expect("La2", _signed(s3[55:59]), spec.la2)
    _expect("Lo2", _unsigned(s3[59:63]), spec.lo2)
    _expect("Di", _unsigned(s3[63:67]), _STEP)
    _expect("Dj", _unsigned(s3[67:71]), _STEP)
    _expect("scanning mode", s3[71], _SCAN_WE_NS)


def _check_product(s4: bytes, run_ts: int, valid_ts: int) -> None:
    _expect("product section length", len(s4), _PRODUCT_SECTION_LEN)
    _expect("product template", _unsigned(s4[7:9]), _PRODUCT_TEMPLATE)
    _expect("coordinate values", _unsigned(s4[5:7]), 0)
    _expect("parameter category", s4[9], _CATEGORY_MOISTURE)
    _expect("parameter number", s4[10], _PARAMETER_PRECIP)
    _expect("generating process type", s4[11], _PROCESS_FORECAST)
    _expect("forecast time unit", s4[17], _UNIT_MINUTE)
    # forecastTime is the *start* of the accumulation window, so lead - 5 minutes.
    start_minutes = (valid_ts - _ACCUMULATION_S - run_ts) // 60
    _expect("forecast time (minutes)", _signed(s4[18:22]), start_minutes)
    _expect("first surface", s4[22], _SURFACE_GROUND)
    _expect("end of interval (valid time)", _epoch(s4[34:41]), valid_ts)
    _expect("time ranges", s4[41], 1)
    _expect("missing values in the interval", _unsigned(s4[42:46]), 0)
    _expect("statistical process", s4[46], _STAT_ACCUMULATION)
    _expect("time range unit", s4[48], _UNIT_MINUTE)
    _expect("accumulation length (minutes)", _unsigned(s4[49:53]), _ACCUMULATION_MINUTES)


def _values(s5: bytes, s6: bytes, s7: bytes, n: int) -> np.ndarray:
    """Unpack the simple-packed field to float64 millimetres (per 5 minutes)."""
    _expect("data representation section length", len(s5), _DATA_SECTION_LEN)
    _expect("data representation template", _unsigned(s5[9:11]), _DATA_TEMPLATE_SIMPLE)
    _expect("number of values", _unsigned(s5[5:9]), n)
    _expect("original field type", s5[20], _ORIGINAL_FLOAT)
    _expect("bitmap section length", len(s6), _BITMAP_SECTION_LEN)
    _expect("bitmap indicator", s6[5], _NO_BITMAP)

    reference = struct.unpack(">f", s5[11:15])[0]
    binary_scale = _signed(s5[15:17])
    decimal_scale = _signed(s5[17:19])
    bits = s5[19]
    payload = len(s7) - _MIN_SECTION_LEN
    if bits == _BITS_CONSTANT:
        _expect("data bytes for a constant field", payload, 0)
        packed = np.zeros(n, dtype=np.float64)
    elif bits == _BITS_PACKED:
        _expect("data bytes", payload, 2 * n)
        packed = np.frombuffer(s7, dtype=">u2", count=n, offset=_MIN_SECTION_LEN)
        packed = packed.astype(np.float64)
    else:
        msg = f"unsupported packing width {bits} bits"
        raise GribDecodeError(msg)
    values = (reference + packed * 2.0**binary_scale) / 10.0**decimal_scale
    if not np.isfinite(values).all() or values.min() < 0.0:
        msg = "negative or non-finite rain amounts"
        raise GribDecodeError(msg)
    return values


def decode(message: bytes, *, run_ts: int, valid_ts: int, bbox) -> PiafField:
    """Decode one PIAF ``_PT5M`` GetCoverage response cropped to ``bbox``.

    ``run_ts`` and ``valid_ts`` are what the caller *asked for*; the message must carry
    exactly those, which is what rejects a wrong-but-well-formed response. Raises
    :class:`GribDecodeError` on any mismatch, ``ValueError`` on a bbox that is not on
    the 0.01° lattice.
    """
    spec = _grid_spec(bbox)
    if valid_ts - run_ts < _ACCUMULATION_S:
        msg = f"valid time {valid_ts} is not after the first window of run {run_ts}"
        raise ValueError(msg)
    sections = _sections(message)
    _check_identification(sections[1], run_ts)
    _check_grid(sections[3], spec)
    _check_product(sections[4], run_ts, valid_ts)
    values = _values(sections[5], sections[6], sections[7], spec.ni * spec.nj)
    rate = (values * _FIVE_MIN_TO_HOURLY).astype(np.float32).reshape(spec.nj, spec.ni)
    return PiafField(run_ts=run_ts, valid_ts=valid_ts, rate=rate, geo=grid_geo(bbox))
