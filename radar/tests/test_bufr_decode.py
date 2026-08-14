"""BUFR reflectivity decoder — ground truth + the ways it must refuse to guess.

The decoder is hand-rolled: it validates that a message is exactly the Météo-France
``Mosaique_metropole_Z_1km`` template, then walks its structure arithmetically and
unpacks the pixel run with numpy. That is only safe if two things hold, and both are
tested here:

1. **It reproduces ground truth.** The pinned fixture is a real message fetched from
   the DPRadar API. Its expected statistics are not this module's own output — they
   are what *two independent general decoders* (pybufrkit and ECMWF ecCodes, both fed
   Météo-France's corrected local tables) agree on, to the digit. A width or offset
   transcription error in ``_ELEMENTS`` desynchronises the walk and breaks these
   immediately.
2. **It fails loudly on drift.** A different edition, centre, local table version or
   descriptor list must raise rather than silently produce plausible noise.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import numpy as np
import pytest

from radar.providers import bufr_decode

FIXTURE = Path(__file__).parent / "fixtures" / "meteofrance_reflectivite.bufr.gz"

# Cross-validated against pybufrkit AND ecCodes on this exact message.
# Do not "fix" these to match a code change — if they move, the
# decoder is wrong, not the numbers.
EXPECTED_ROWS = EXPECTED_COLS = 1536
EXPECTED_MISSING = 940_872
EXPECTED_MIN_DBZ = -40.0
EXPECTED_MAX_DBZ = 46.0
# Fraction of *covered* cells at or above each dBZ floor. Both reference decoders
# reported these as percentages to three decimals (0.552%, 0.429%, ...), so they are
# only known to +/-0.0005% — hence the tolerance. Tightening it would be asserting
# precision the oracles never gave us.
EXPECTED_FRACTIONS = {0: 0.00552, 5: 0.00429, 10: 0.00335, 20: 0.00070}
FRACTION_TOLERANCE = 5e-6

# The LAME_D_EAU composite's own north-west corner (ODIM /where). The whole
# composite design rests on the two products sharing it.
LAME_UL_LAT, LAME_UL_LON = 53.67, -9.965


@pytest.fixture(scope="module")
def raw() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def grid(raw: bytes):
    return bufr_decode.decode(raw)


# -- ground truth --------------------------------------------------------------


def test_decodes_to_the_reference_field(grid):
    """The load-bearing check: our field matches pybufrkit and ecCodes exactly."""
    field = grid.field
    assert field.shape == (EXPECTED_ROWS, EXPECTED_COLS)

    finite = field[np.isfinite(field)]
    assert int(np.isnan(field).sum()) == EXPECTED_MISSING
    assert float(finite.min()) == pytest.approx(EXPECTED_MIN_DBZ)
    assert float(finite.max()) == pytest.approx(EXPECTED_MAX_DBZ)
    for floor, expected in EXPECTED_FRACTIONS.items():
        actual = float((finite >= floor).mean())
        assert actual == pytest.approx(expected, abs=FRACTION_TOLERANCE), f">= {floor} dBZ"


def test_no_echo_floor_dominates(grid):
    """>99% of covered cells sit exactly at the -40 dBZ floor.

    Pins the sentinel handling: -40 is a real "radar looked, no echo" value that must
    survive as data, while out-of-coverage cells become NaN. Confusing the two would
    either paint the whole domain or blank the mosaic.
    """
    finite = grid.field[np.isfinite(grid.field)]
    assert float((finite == EXPECTED_MIN_DBZ).mean()) > 0.99


def test_gzipped_and_raw_payloads_agree(raw, grid):
    """The API serves gzip; the decoder must accept either without changing output."""
    decoded = bufr_decode.decode(gzip.decompress(raw))
    assert np.array_equal(decoded.field, grid.field, equal_nan=True)


# -- geometry ------------------------------------------------------------------


def test_geometry_matches_the_lame_d_eau_grid(grid):
    """Same projection and same north-west corner ⇒ no reprojection when compositing."""
    geo = grid.geo
    assert geo.xsize == EXPECTED_COLS
    assert geo.ysize == EXPECTED_ROWS
    assert geo.xscale == geo.yscale == 1000.0
    assert "+proj=stere" in geo.projdef
    assert "+lat_ts=45" in geo.projdef
    assert "+lon_0=0" in geo.projdef

    # The projected UL corner must be the same physical point as LAME_D_EAU's.
    from radar.providers.meteofrance_render import transformer_for  # noqa: PLC0415

    x, y = transformer_for(geo.projdef).transform(LAME_UL_LON, LAME_UL_LAT)
    assert geo.x_ul == pytest.approx(x, abs=1.0)
    assert geo.y_ul == pytest.approx(y, abs=1.0)


def test_row_zero_is_the_north_edge(grid):
    """Orientation pin.

    Established empirically by correlating the echo against the rain field — the
    flipped alternative scored 0.3% overlap against 77.6%. A silent flip would put
    every storm in the wrong hemisphere of France while still looking like weather,
    so assert it structurally: the mosaic's southern rows reach into the
    Mediterranean/Atlantic and are mostly out of radar coverage, while the middle
    band over metropolitan France is well covered.
    """
    covered = np.isfinite(grid.field).mean(axis=1)
    middle = covered[600:900].mean()  # ~46-49N, the heart of the network
    bottom = covered[-200:].mean()  # far south, off the continental shelf
    assert middle > bottom


def test_timestamp_is_the_nominal_time(grid):
    """Section 1's nominal time decodes to a plausible epoch (2026-07-20 19:15 UTC)."""
    assert grid.timestamp == 1_784_574_900


# -- refusing to guess ---------------------------------------------------------


def _mutate(raw: bytes, offset: int, value: int) -> bytes:
    """Flip one byte of the *decompressed* message and re-gzip it."""
    body = bytearray(gzip.decompress(raw))
    body[offset] = value
    return gzip.compress(bytes(body))


def test_rejects_non_bufr(raw):
    with pytest.raises(bufr_decode.BufrDecodeError, match="not a BUFR message"):
        bufr_decode.decode(b"GRIB" + gzip.decompress(raw)[4:])


def test_rejects_other_editions(raw):
    with pytest.raises(bufr_decode.BufrDecodeError, match="edition"):
        bufr_decode.decode(_mutate(raw, 7, 3))  # section 0 byte 7 = edition


def test_rejects_another_originating_centre(raw):
    """A different centre means different local tables — refuse rather than mis-scale."""
    with pytest.raises(bufr_decode.BufrDecodeError, match="unexpected origin"):
        bufr_decode.decode(_mutate(raw, 8 + 5, 0x62))  # centre low byte: 85 -> 98


def test_rejects_a_local_table_bump(raw):
    """The embedded table subset is version-specific; a bump must fail loudly."""
    with pytest.raises(bufr_decode.BufrDecodeError, match="local table version"):
        bufr_decode.decode(_mutate(raw, 8 + 14, 20))


def test_rejects_a_changed_template(raw):
    """Section 3 drift invalidates the arithmetic walk entirely."""
    body = bytearray(gzip.decompress(raw))
    # Section 3 starts at 30 (section 1 is 22 bytes, no section 2); descriptors at +7.
    body[30 + 7] ^= 0x01
    with pytest.raises(bufr_decode.BufrDecodeError, match="unexpected BUFR template"):
        bufr_decode.decode(gzip.compress(bytes(body)))


@pytest.mark.parametrize(
    "keep",
    [
        # Below section 0 + section 1: the origin fields _sections reads aren't there.
        pytest.param(4, id="magic-only"),
        pytest.param(8, id="section-0-only"),
        pytest.param(29, id="one-byte-short-of-section-1"),
        # Enough to parse the origin, then cut inside section 3 / the data.
        pytest.param(31, id="into-section-1"),
        pytest.param(None, id="half-the-message"),
    ],
)
def test_rejects_a_truncated_message(raw, keep):
    """Truncation must raise BufrDecodeError at *every* depth, never IndexError.

    The provider's narrow ``except BufrDecodeError`` is written against this contract,
    so a leaked IndexError would only be caught by the broad backstop above it — and
    any future caller reading the docstring would be wrong.
    """
    body = gzip.decompress(raw)
    body = body[: keep if keep is not None else len(body) // 2]
    with pytest.raises(bufr_decode.BufrDecodeError):
        bufr_decode.decode(body)


def test_rejects_malformed_gzip():
    with pytest.raises(bufr_decode.BufrDecodeError, match="gzip"):
        bufr_decode.decode(b"\x1f\x8b" + b"garbage")
