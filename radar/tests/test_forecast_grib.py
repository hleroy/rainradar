"""PIAF GRIB2 decoder — ground truth, geometry, and the ways it must refuse to guess.

The fixtures are real ``GetCoverage`` responses captured in the Phase 0 spike
(``specs/forecast-nowcast.md`` §8.10). The expected numbers are not this decoder's own
output: they come from the GeoTIFF rendition of the *same* requests, which Météo-France
produces server-side with GDAL. The two formats carry bit-identical values, so any
error in the bit unpacking, the scale factors or the row order shows up here.

The decoder's other job is to refuse anything that is not exactly the message we
expect: the service mislabels its units and has once answered with somebody else's
GRIB under a 200 (§8.5). Every rejection test below patches one field of a real
message and must raise rather than produce plausible rain.
"""

from __future__ import annotations

import gzip
import struct
from datetime import UTC
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from django.conf import settings

from radar.forecast import grib_decode
from radar.forecast.grib_decode import GribDecodeError

FIXTURES = Path(__file__).parent / "fixtures"
RAIN_SMALL = FIXTURES / "piaf_rain_small_20261111T1125Z_lead5.grib2"
DRY_SMALL = FIXTURES / "piaf_dry_small_20261108T1200Z_lead5.grib2"
DRY_BBOX = FIXTURES / "piaf_dry_bbox_20261108T1200Z_lead5.grib2.gz"

SMALL_BBOX = (42.75, 44.25, 8.0, 9.5)  # S, N, W, E — the Ligurian Sea box


def _ts(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


RAIN_RUN, RAIN_VALID = _ts(2026, 11, 11, 11, 25), _ts(2026, 11, 11, 11, 30)
DRY_RUN, DRY_VALID = _ts(2026, 11, 8, 12, 0), _ts(2026, 11, 8, 12, 5)

# From the GeoTIFF of each request (GDAL, server-side). In mm per 5 minutes; the
# decoder returns mm/h, i.e. x12. Do not "fix" these to match a code change.
ORACLE = {
    "rain_small": {"zero": 14_138, "wet": 8_663, "max": 9.02001953125, "sum": 4405.58203125,
                   "argmax": (22, 114), "wet_north_half": 6_872},
    "dry_small": {"zero": 22_126, "wet": 675, "max": 0.989990234375, "sum": 67.30609130859375,
                  "argmax": (18, 105), "wet_north_half": 675},
    "dry_bbox": {"zero": 1_598_570, "wet": 21_131, "max": 1.8900146484375,
                 "sum": 1300.2588500976562, "argmax": (149, 566), "wet_north_half": 9_557},
}  # fmt: skip


@pytest.fixture(scope="module")
def rain_raw() -> bytes:
    return RAIN_SMALL.read_bytes()


def _decode_rain(message: bytes, **overrides):
    kwargs = {"run_ts": RAIN_RUN, "valid_ts": RAIN_VALID, "bbox": SMALL_BBOX} | overrides
    return grib_decode.decode(message, **kwargs)


# -- message surgery ------------------------------------------------------------


def _split(message: bytes) -> tuple[bytes, list[bytes]]:
    """Section 0 and the list of following sections (section 8 dropped)."""
    sections, offset, end = [], 16, len(message) - 4
    while offset < end:
        length = int.from_bytes(message[offset : offset + 4], "big")
        sections.append(message[offset : offset + length])
        offset += length
    return message[:16], sections


def _join(header: bytes, sections: list[bytes]) -> bytes:
    """Reassemble a message, fixing every section length and the total length."""
    fixed = [len(s).to_bytes(4, "big") + s[4:] for s in sections]
    body = b"".join(fixed)
    total = 16 + len(body) + 4
    return header[:8] + total.to_bytes(8, "big") + body + b"7777"


def _patch(message: bytes, number: int, offset: int, value: bytes) -> bytes:
    """Overwrite bytes at ``offset`` inside section ``number``."""
    header, sections = _split(message)
    for i, section in enumerate(sections):
        if section[4] == number:
            sections[i] = section[:offset] + value + section[offset + len(value) :]
    return _join(header, sections)


def _section(message: bytes, number: int) -> bytes:
    return next(s for s in _split(message)[1] if s[4] == number)


def _replace_section(message: bytes, number: int, new: bytes) -> bytes:
    header, sections = _split(message)
    return _join(header, [new if s[4] == number else s for s in sections])


# -- ground truth -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "path", "run_ts", "valid_ts", "bbox", "shape"),
    [
        ("rain_small", RAIN_SMALL, RAIN_RUN, RAIN_VALID, SMALL_BBOX, (151, 151)),
        ("dry_small", DRY_SMALL, DRY_RUN, DRY_VALID, SMALL_BBOX, (151, 151)),
        ("dry_bbox", DRY_BBOX, DRY_RUN, DRY_VALID, None, (1031, 1571)),
    ],
)
def test_decodes_to_the_gdal_reference(name, path, run_ts, valid_ts, bbox, shape):  # noqa: PLR0913, PLR0917
    """The load-bearing check: the field matches Météo-France's own GeoTIFF of it."""
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    bbox = bbox or tuple(settings.RADAR_BBOX)
    field = grib_decode.decode(raw, run_ts=run_ts, valid_ts=valid_ts, bbox=bbox)
    oracle = ORACLE[name]

    mm = field.rate.astype(np.float64) / 12.0
    assert field.rate.dtype == np.float32
    assert field.rate.shape == shape
    assert int((mm == 0).sum()) == oracle["zero"]
    assert int((mm > 0).sum()) == oracle["wet"]
    # 16-bit values x 2^E x 12 are exact in float32, so the maximum is exact too.
    assert float(field.rate.max()) == oracle["max"] * 12
    assert float(mm.sum()) == pytest.approx(oracle["sum"], rel=1e-9)
    assert np.unravel_index(int(field.rate.argmax()), shape) == oracle["argmax"]
    assert field.run_ts == run_ts
    assert field.valid_ts == valid_ts


@pytest.mark.parametrize("name", ["rain_small", "dry_bbox"])
def test_row_zero_is_the_north_edge(name):
    """Orientation pin: the oracle's wet-cell split between the halves must survive.

    A flipped decode keeps every statistic above except the argmax — and this split,
    which differs sharply between north and south for both fixtures.
    """
    if name == "rain_small":
        rate = grib_decode.decode(
            RAIN_SMALL.read_bytes(), run_ts=RAIN_RUN, valid_ts=RAIN_VALID, bbox=SMALL_BBOX
        ).rate
    else:
        rate = grib_decode.decode(
            gzip.decompress(DRY_BBOX.read_bytes()),
            run_ts=DRY_RUN,
            valid_ts=DRY_VALID,
            bbox=tuple(settings.RADAR_BBOX),
        ).rate
    assert int((rate[: rate.shape[0] // 2] > 0).sum()) == ORACLE[name]["wet_north_half"]


def test_heaviest_cell_sits_over_the_ligurian_sea(rain_raw):
    """The 108 mm/h cell is at 44.03 °N 9.14 °E — position read through ``geo``."""
    field = _decode_rain(rain_raw)
    row, col = np.unravel_index(int(field.rate.argmax()), field.rate.shape)
    lon = field.geo.x_ul + (col + 0.5) * field.geo.xscale
    lat = field.geo.y_ul - (row + 0.5) * field.geo.yscale
    assert (round(lat, 2), round(lon, 2)) == (44.03, 9.14)
    assert float(field.rate.max()) == pytest.approx(108.24, abs=0.01)


# -- geometry ----------------------------------------------------------------------


def test_geo_of_the_radar_bbox():
    """The first GRIB point is a cell *centre*; ``GridGeo`` wants the edge, half a cell out."""
    geo = grib_decode.grid_geo(tuple(settings.RADAR_BBOX))
    assert geo.projdef == grib_decode.PROJDEF
    assert (geo.xsize, geo.ysize) == (1571, 1031)
    assert geo.xscale == geo.yscale == 0.01
    assert geo.x_ul == pytest.approx(-6.005)
    assert geo.y_ul == pytest.approx(51.505)


def test_negative_west_edge_is_encoded_on_0_360(rain_raw):
    """GRIB2 longitudes are 0-360: the bbox's -6° arrives as Lo1 = 354°."""
    raw = gzip.decompress(DRY_BBOX.read_bytes())
    assert int.from_bytes(_section(raw, 3)[50:54], "big") == 354_000_000
    # …while the Ligurian box's east-of-Greenwich edge is unchanged.
    assert int.from_bytes(_section(rain_raw, 3)[50:54], "big") == 8_000_000


@pytest.mark.parametrize(
    "bbox",
    [(41.2, 51.5, -6.0, 9.705), (51.5, 41.2, -6.0, 9.7), (41.2, 41.2, -6.0, 9.7)],
)
def test_bbox_off_the_lattice_is_a_configuration_error(bbox):
    with pytest.raises(ValueError, match=r"0\.01"):
        grib_decode.grid_geo(bbox)


def test_wrong_bbox_is_rejected(rain_raw):
    """A response for another area (e.g. the full bbox asked, a box returned) raises."""
    with pytest.raises(GribDecodeError, match="number of points"):
        _decode_rain(rain_raw, bbox=tuple(settings.RADAR_BBOX))


# -- identity: what we asked for ------------------------------------------------


def test_another_run_is_rejected(rain_raw):
    """The wrong-payload guard: a well-formed message for a different run must not pass."""
    with pytest.raises(GribDecodeError, match="run"):
        _decode_rain(rain_raw, run_ts=RAIN_RUN - 300, valid_ts=RAIN_VALID - 300)


def test_another_lead_time_is_rejected(rain_raw):
    with pytest.raises(GribDecodeError, match="forecast time"):
        _decode_rain(rain_raw, valid_ts=RAIN_VALID + 300)


def test_valid_time_before_the_first_window_is_a_caller_error(rain_raw):
    with pytest.raises(ValueError, match="valid time"):
        _decode_rain(rain_raw, valid_ts=RAIN_RUN)


# -- template drift -----------------------------------------------------------------

_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")


@pytest.mark.parametrize(
    ("section", "offset", "value", "match"),
    [
        (1, 5, _U16.pack(85), "originating centre"),
        (1, 10, b"\x01", "local table version"),
        (1, 11, b"\x00", "reference time significance"),
        (1, 14, b"\x0d", "invalid timestamp"),  # month 13
        (3, 12, _U16.pack(1), "grid template"),
        (3, 14, b"\x01", "shape of the earth"),
        (3, 30, _U32.pack(152), "Ni"),
        (3, 38, _U32.pack(1), "basic angle"),
        (3, 46, _U32.pack(44_260_000), "La1"),
        (3, 50, _U32.pack(8_010_000), "Lo1"),
        (3, 54, b"\x00", "resolution flags"),
        (3, 63, _U32.pack(20_000), "Di"),
        (3, 71, b"\x40", "scanning mode"),
        (4, 5, _U16.pack(1), "coordinate values"),
        (4, 7, _U16.pack(0), "product template"),
        (4, 9, b"\x00", "parameter category"),
        (4, 10, b"\x08", "parameter number"),
        (4, 11, b"\x00", "generating process type"),
        (4, 17, b"\x01", "forecast time unit"),
        (4, 22, b"\x67", "first surface"),
        (4, 39, b"\x20", "end of interval"),  # minute 32 instead of 30
        (4, 41, b"\x02", "time ranges"),
        (4, 42, _U32.pack(1), "missing values"),
        (4, 46, b"\x00", "statistical process"),  # average, not accumulation
        (4, 48, b"\x01", "time range unit"),
        (4, 49, _U32.pack(15), "accumulation length"),
        (5, 5, _U32.pack(1), "number of values"),
        (5, 9, _U16.pack(40), "data representation template"),  # JPEG 2000
        (5, 19, b"\x0c", "packing width"),
        (5, 20, b"\x01", "original field type"),
        (6, 5, b"\x00", "bitmap indicator"),
    ],
)
def test_template_drift_raises(rain_raw, section, offset, value, match):
    with pytest.raises(GribDecodeError, match=match):
        _decode_rain(_patch(rain_raw, section, offset, value))


@pytest.mark.parametrize(
    ("section", "match"),
    [
        (1, "identification section length"),
        (3, "grid section length"),
        (4, "product section length"),
        (5, "data representation section length"),
        (6, "bitmap section length"),
    ],
)
def test_resized_section_raises(rain_raw, section, match):
    """A fixed-layout section that grows (e.g. a second time range) is a new template."""
    grown = _section(rain_raw, section) + b"\x00\x00"
    with pytest.raises(GribDecodeError, match=match):
        _decode_rain(_replace_section(rain_raw, section, grown))


def test_truncated_identification_section_raises(rain_raw):
    """Too short to hold the run time: a GribDecodeError, never a raw struct.error."""
    truncated = _section(rain_raw, 1)[:15]
    with pytest.raises(GribDecodeError, match="identification section length"):
        _decode_rain(_replace_section(rain_raw, 1, truncated))


# -- envelope ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda m: b"GRIX" + m[4:], "not a GRIB message"),
        (lambda m: m[:12], "not a GRIB message"),
        (lambda m: m[:7] + b"\x01" + m[8:], "edition"),
        (lambda m: m[:6] + b"\x02" + m[7:], "discipline"),
        (lambda m: m + b"\x00", "message length"),  # trailing bytes / a second message
        (lambda m: m[:-10], "message length"),  # truncated body
        (lambda m: m[:-4] + b"0000", "end marker"),
    ],
)
def test_envelope_damage_raises(rain_raw, mutate, match):
    with pytest.raises(GribDecodeError, match=match):
        _decode_rain(mutate(rain_raw))


def test_missing_section_raises(rain_raw):
    header, sections = _split(rain_raw)
    with pytest.raises(GribDecodeError, match="section order"):
        _decode_rain(_join(header, [s for s in sections if s[4] != 6]))


def test_local_use_section_raises(rain_raw):
    """A section 2 appearing is template drift too, even if it would decode."""
    header, sections = _split(rain_raw)
    local = (6).to_bytes(4, "big") + b"\x02\x00"
    with pytest.raises(GribDecodeError, match="section order"):
        _decode_rain(_join(header, [sections[0], local, *sections[1:]]))


def test_section_length_below_minimum_raises(rain_raw):
    # Section 3's length field, written directly (_patch would re-derive it).
    offset = 16 + len(_section(rain_raw, 1))
    damaged = rain_raw[:offset] + (3).to_bytes(4, "big") + rain_raw[offset + 4 :]
    with pytest.raises(GribDecodeError, match="invalid length"):
        _decode_rain(damaged)


def test_section_overrunning_the_message_raises(rain_raw):
    offset = 16 + len(_section(rain_raw, 1))
    damaged = rain_raw[:offset] + (10**6).to_bytes(4, "big") + rain_raw[offset + 4 :]
    with pytest.raises(GribDecodeError, match="invalid length"):
        _decode_rain(damaged)


def test_stray_bytes_before_the_end_marker_raise(rain_raw):
    """Fewer than a section header's worth of bytes left over is not a section."""
    body = rain_raw[16:-4] + b"\x00\x00\x00"
    damaged = rain_raw[:8] + (16 + len(body) + 4).to_bytes(8, "big") + body + b"7777"
    with pytest.raises(GribDecodeError, match="overruns"):
        _decode_rain(damaged)


# -- packing ---------------------------------------------------------------------------


def test_short_data_section_raises(rain_raw):
    shortened = _section(rain_raw, 7)[:-2]
    with pytest.raises(GribDecodeError, match="data bytes"):
        _decode_rain(_replace_section(rain_raw, 7, shortened))


def _constant(rain_raw: bytes, reference: float, payload: bytes = b"") -> bytes:
    """A legal constant-field message: 0 bits per value, every value equal to R."""
    message = _patch(rain_raw, 5, 19, b"\x00")
    message = _patch(message, 5, 11, struct.pack(">f", reference))
    return _replace_section(message, 7, _section(rain_raw, 7)[:5] + payload)


def test_constant_field_decodes_without_data_bytes(rain_raw):
    """``nbits = 0`` is legal GRIB2 for a uniform field; never seen, but must not crash."""
    field = _decode_rain(_constant(rain_raw, 0.25))
    assert field.rate.shape == (151, 151)
    assert np.all(field.rate == np.float32(3.0))  # 0.25 mm per 5 min = 3 mm/h


def test_constant_field_with_data_bytes_raises(rain_raw):
    with pytest.raises(GribDecodeError, match="constant field"):
        _decode_rain(_constant(rain_raw, 0.25, payload=b"\x00\x00"))


@pytest.mark.parametrize("reference", [-1.0, float("nan")])
def test_negative_or_non_finite_amounts_raise(rain_raw, reference):
    with pytest.raises(GribDecodeError, match="negative or non-finite"):
        _decode_rain(_constant(rain_raw, reference))


def test_decimal_scale_is_applied(rain_raw):
    """D is read from each message, not assumed zero: D = 1 divides every value by 10."""
    scaled = _decode_rain(_patch(rain_raw, 5, 17, _U16.pack(1)))
    plain = _decode_rain(rain_raw)
    assert np.allclose(scaled.rate, plain.rate / 10.0)


def test_negative_binary_scale_uses_sign_magnitude(rain_raw):
    """E is sign-and-magnitude (0x800C is -12), not two's complement."""
    assert _section(rain_raw, 5)[15:17] == bytes.fromhex("800c")
    assert grib_decode._signed(bytes.fromhex("800c")) == -12
    assert grib_decode._signed(bytes.fromhex("000c")) == 12
