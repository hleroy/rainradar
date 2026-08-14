"""Météo-France REFLECTIVITE (BUFR) → dBZ grid — the single BUFR quarantine point.

Decodes the DPRadar ``Mosaique_metropole_Z_1km`` product: a gzipped WMO BUFR edition-4
message carrying a 1536x1536 reflectivity mosaic on the *same* polar-stereographic grid
as the LAME_D_EAU ODIM composite we already render, so the result drops straight into
:class:`~radar.providers.meteofrance_render.GridGeo` with no reprojection.

Why this module exists rather than a BUFR library
-------------------------------------------------
Both general decoders were measured against this product and neither is usable:

* **pybufrkit** (pure Python) decodes it correctly in **~52 s** per frame.
* **ecCodes** (the ECMWF reference C library) takes **~61 s** — *slower* — for 145 MB
  of image weight, and still needs the Météo-France local tables supplied by hand.

The cost is not bit extraction, it is the descriptor walk: the pixel field is a group
of five descriptors replicated 2 359 296 times, so a general decoder expands ~11.8 M
descriptor instances. This module instead validates that the message is exactly the
template we know, walks its ~56-descriptor structure arithmetically (skipping the bulk
arrays by ``count x width`` rather than iterating them), and unpacks the reflectivity
run with ``np.unpackbits`` — **~0.02 s**, bit-identical to both reference decoders.

That trade is safe only because the template is *checked*: any drift in the descriptor
list, the edition, the centre, or the local table version raises rather than silently
mis-decoding into plausible-looking noise.

Table provenance
----------------
Twelve of the 56 descriptors are in the WMO *local* range, so no stock table set can
walk this message. Météo-France's own ``tables_bufr_361`` distribution is malformed;
the corrected local table 14 for centre 85 comes from
`theperk08/Meteo_France_Radars <https://github.com/theperk08/Meteo_France_Radars>`_
(**MIT licence**). :data:`_ELEMENTS` embeds only the entries this one template touches,
cross-checked against ecCodes' WMO master table 0 v16 for the international ones.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from dataclasses import field as dc_field

import numpy as np

from radar.providers.meteofrance_render import GridGeo
from radar.providers.meteofrance_render import transformer_for


class BufrDecodeError(Exception):
    """A malformed or unexpected BUFR message. The provider chains it onward."""


# -- the message we expect ----------------------------------------------------

_EDITION = 4
_MASTER_TABLE = 0
_CENTRE = 85  # Toulouse / Météo-France
_LOCAL_TABLE_VERSION = 14
_DATA_CATEGORY = 6  # radar

# Section 0 is 8 bytes and the shortest edition-4 section 1 is 22, so anything below
# this cannot carry the origin fields _sections reads — and would index past the end
# rather than raising BufrDecodeError, breaking decode()'s documented contract.
_MIN_MESSAGE_BYTES = 30

# Section 3's descriptor list, verbatim. Validated on every decode: this module's
# arithmetic walk is only sound for this exact template.
_TEMPLATE = (
    "001099", "030031", "001192", "301011", "301013", "008021", "004025", "004026",
    "029002", "029001", "030021", "030022", "005033", "006033", "329192", "029192",
    "025194", "030032", "025192", "025009", "025010", "025011",
    "110000", "031001",                                        # 32 radar sites, 10 members
    "301001", "301011", "301013", "005001", "006001", "006196", "025210",
    "101000", "031001", "048192",                              # per-site alignment bits
    "101000", "031001", "048192",                              # alignment bits
    "101000", "031192", "021120",                              # probability-of-rain array
    "103000", "031192", "201124", "010002", "201000",          # echo-top height array
    "203011", "021001", "203255",                              # new reference value
    "105000", "031192", "201132", "202129", "021001", "202000", "201000", "203000",
)  # fmt: skip

# (scale, reference, bits) per element descriptor. International entries are ecCodes'
# WMO master table 0 v16; the 0-**-19x / 0-48-192 entries are Météo-France local
# table 14 (see "Table provenance" above).
_ELEMENTS: dict[str, tuple[int, int, int]] = {
    "001001": (0, 0, 7),  # WMO block number
    "001002": (0, 0, 10),  # WMO station number
    "001099": (0, 0, 248),  # unique product definition (CCITT IA5, 31 chars)
    "001192": (0, 0, 8),  # LOCAL indicateur des composites
    "004001": (0, 0, 12),  # year
    "004002": (0, 0, 4),  # month
    "004003": (0, 0, 6),  # day
    "004004": (0, 0, 5),  # hour
    "004005": (0, 0, 6),  # minute
    "004006": (0, 0, 6),  # second
    "004025": (0, -2048, 12),  # time period (min)
    "004026": (0, -4096, 13),  # time period (s)
    "005001": (5, -9000000, 25),  # latitude (high accuracy)
    "005033": (-1, 0, 16),  # pixel size on horizontal - 1
    "005194": (0, 0, 8),  # LOCAL indicateur du centre de projection
    "005195": (5, -9000000, 25),  # LOCAL latitude de reference
    "006001": (5, -18000000, 26),  # longitude (high accuracy)
    "006033": (-1, 0, 16),  # pixel size on horizontal - 2
    "006196": (-3, 0, 16),  # LOCAL distance oblique maximale
    "006198": (5, -18000000, 26),  # LOCAL longitude du meridien parallele a l'axe des Y
    "008021": (0, 0, 5),  # time significance
    "010002": (-1, -40, 16),  # height
    "021001": (0, -64, 7),  # horizontal reflectivity
    "021120": (3, 0, 10),  # probability of rain
    "025009": (0, 0, 4),  # calibration method
    "025010": (0, 0, 4),  # clutter treatment
    "025011": (0, 0, 2),  # ground occultation correction
    "025192": (0, 0, 8),  # LOCAL methode de composition
    "025194": (2, 0, 16),  # LOCAL numero de version de composite
    "025210": (2, 0, 10),  # LOCAL facteur de correction global
    "029001": (0, 0, 3),  # projection type
    "029002": (0, 0, 3),  # coordinate grid type
    "029192": (0, 0, 6),  # LOCAL systeme geodesique
    "030021": (0, 0, 12),  # number of pixels per row
    "030022": (0, 0, 12),  # number of pixels per column
    "030031": (0, 0, 4),  # picture type
    "030032": (0, 0, 16),  # combination with other data
    "030192": (0, 0, 8),  # LOCAL mode de balayage
    "031001": (0, 0, 8),  # delayed descriptor replication factor
    "031192": (0, 0, 32),  # LOCAL facteur super elargi de repetition differe
    "048192": (0, 0, 1),  # LOCAL bit de calage
}

# Table D sequences this template expands.
_SEQUENCES: dict[str, tuple[str, ...]] = {
    "301001": ("001001", "001002"),
    "301011": ("004001", "004002", "004003"),
    "301013": ("004004", "004005", "004006"),
    "329192": ("005001", "006001", "006198", "005194", "030192", "005195"),
}

# The mosaic's projection is fixed by the message itself (0-05-195 reference latitude,
# 0-06-198 meridian parallel to the Y axis); the rest matches LAME_D_EAU's ODIM projdef
# exactly, which is what lets the two products composite without reprojection.
_PROJDEF = "+proj=stere +lat_0=90 +lon_0={lon_0:g} +lat_ts={lat_ts:g} +ellps=WGS84 +datum=WGS84"

_REFLECTIVITY = "021001"


@dataclass(frozen=True)
class _Run:
    """Where the reflectivity array sits in the body, and how to scale it."""

    start_bit: int
    count: int
    bits: int
    scale: int
    reference: int


@dataclass
class ReflectivityGrid:
    """A decoded reflectivity mosaic: dBZ values (NaN = outside radar coverage)."""

    field: np.ndarray  # float32 (ysize, xsize); row 0 is the NORTH edge
    geo: GridGeo
    timestamp: int | None = None  # epoch seconds of the message's nominal time
    # Lazily-built mip pyramid, memoised by meteofrance_render._wash_pyramid so all
    # 62 tile renders of a frame share it (mirrors Grid._levels).
    _levels: list[tuple[np.ndarray, GridGeo]] | None = dc_field(default=None, repr=False)


# -- bit plumbing -------------------------------------------------------------


class _Reader:
    """A big-endian bit cursor over section 4's data."""

    __slots__ = ("bit", "data", "limit")

    def __init__(self, data: bytes, start_bit: int, limit_bit: int) -> None:
        self.data = data
        self.bit = start_bit
        self.limit = limit_bit

    def read(self, nbits: int) -> int:
        if self.bit + nbits > self.limit:
            msg = f"BUFR message truncated: need {nbits} bits at offset {self.bit}"
            raise BufrDecodeError(msg)
        value = 0
        bit = self.bit
        for _ in range(nbits):
            byte = self.data[bit >> 3]
            value = (value << 1) | ((byte >> (7 - (bit & 7))) & 1)
            bit += 1
        self.bit = bit
        return value

    def skip(self, nbits: int) -> None:
        if self.bit + nbits > self.limit:
            msg = f"BUFR message truncated: cannot skip {nbits} bits at offset {self.bit}"
            raise BufrDecodeError(msg)
        self.bit += nbits


@dataclass
class _State:
    """Operator state: 2-01 width, 2-02 scale, 2-03 new reference values."""

    width: int = 0
    scale: int = 0
    refval_bits: int = 0
    refvals: dict[str, int] | None = None

    def reference(self, fxy: str, table_ref: int) -> int:
        if self.refvals and fxy in self.refvals:
            return self.refvals[fxy]
        return table_ref


def _u(buf: bytes, offset: int, length: int) -> int:
    return int.from_bytes(buf[offset : offset + length], "big")


def _expand(descriptors: tuple[str, ...]) -> list[str]:
    """Expand Table D sequences one level deep (this template nests no further).

    Only the bulk path needs this: ``_walk_descriptors`` splices sequences itself as it
    goes, but ``_block_bits`` measures a block arithmetically and has no ``"3"`` case.
    """
    out: list[str] = []
    for fxy in descriptors:
        if fxy in _SEQUENCES:
            out.extend(_SEQUENCES[fxy])
        else:
            out.append(fxy)
    return out


# -- section parsing ----------------------------------------------------------


def _validate_origin(meta: dict) -> None:
    """Fail loudly rather than mis-decode.

    The arithmetic walk is only sound for this originating centre and local table
    version; a bump on either side means the embedded table subset no longer applies,
    and we would otherwise desynchronise into plausible-looking noise.
    """
    if meta["master_table"] != _MASTER_TABLE or meta["centre"] != _CENTRE:
        msg = (
            f"unexpected origin: master table {meta['master_table']}, centre "
            f"{meta['centre']} (expected {_MASTER_TABLE}/{_CENTRE})"
        )
        raise BufrDecodeError(msg)
    if meta["local_table_version"] != _LOCAL_TABLE_VERSION:
        msg = (
            f"local table version {meta['local_table_version']} != {_LOCAL_TABLE_VERSION}; "
            "the embedded Météo-France table subset no longer applies"
        )
        raise BufrDecodeError(msg)
    if meta["data_category"] != _DATA_CATEGORY:
        msg = f"data category {meta['data_category']} != {_DATA_CATEGORY} (radar)"
        raise BufrDecodeError(msg)


def _sections(message: bytes) -> tuple[dict, int, int]:
    """Validate sections 0/1/3 and return (metadata, s4_data_start, s4_end) in bytes."""
    if message[:4] != b"BUFR":
        msg = f"not a BUFR message (magic {message[:4]!r})"
        raise BufrDecodeError(msg)
    if len(message) < _MIN_MESSAGE_BYTES:
        msg = f"BUFR message truncated: {len(message)} bytes, need at least {_MIN_MESSAGE_BYTES}"
        raise BufrDecodeError(msg)
    total = _u(message, 4, 3)
    if total > len(message):
        msg = f"declared length {total} exceeds payload {len(message)}"
        raise BufrDecodeError(msg)
    edition = message[7]
    if edition != _EDITION:
        msg = f"unsupported BUFR edition {edition} (expected {_EDITION})"
        raise BufrDecodeError(msg)

    s1 = 8
    s1_len = _u(message, s1, 3)
    meta = {
        "master_table": message[s1 + 3],
        "centre": _u(message, s1 + 4, 2),
        "data_category": message[s1 + 10],
        "master_table_version": message[s1 + 13],
        "local_table_version": message[s1 + 14],
        "year": _u(message, s1 + 15, 2),
        "month": message[s1 + 17],
        "day": message[s1 + 18],
        "hour": message[s1 + 19],
        "minute": message[s1 + 20],
        "second": message[s1 + 21],
    }
    _validate_origin(meta)

    pos = s1 + s1_len
    if message[s1 + 9] & 0x80:  # optional section 2
        pos += _u(message, pos, 3)

    if pos + 7 > len(message):
        msg = f"BUFR message truncated before section 3 (offset {pos} of {len(message)})"
        raise BufrDecodeError(msg)
    s3_len = _u(message, pos, 3)
    n_subsets = _u(message, pos + 4, 2)
    if n_subsets != 1:
        msg = f"expected a single subset, got {n_subsets}"
        raise BufrDecodeError(msg)
    if message[pos + 6] & 0x40:
        msg = "compressed BUFR data is not supported"
        raise BufrDecodeError(msg)
    body = message[pos + 7 : pos + s3_len]
    descriptors = tuple(
        f"{(_u(body, i, 2) >> 14)}{((_u(body, i, 2) >> 8) & 0x3F):02d}{(_u(body, i, 2) & 0xFF):03d}"
        for i in range(0, len(body) - 1, 2)
    )
    if descriptors != _TEMPLATE:
        msg = (
            "unexpected BUFR template: section 3 does not match the known "
            f"Météo-France reflectivity mosaic ({len(descriptors)} descriptors)"
        )
        raise BufrDecodeError(msg)

    pos += s3_len
    s4_len = _u(message, pos, 3)
    return meta, pos + 4, pos + s4_len


# -- the walk -----------------------------------------------------------------


def _element_bits(fxy: str, state: _State) -> int:
    entry = _ELEMENTS.get(fxy)
    if entry is None:
        msg = f"no table entry for element {fxy}"
        raise BufrDecodeError(msg)
    # 0-31-* replication factors are never widened by a 2-01 operator.
    if fxy.startswith("031"):
        return entry[2]
    return entry[2] + state.width


def _block_bits(block: list[str], outer: _State) -> tuple[int, dict[str, tuple[int, int]]]:
    """Bits per repetition of a block containing only elements and 2-0x operators.

    Returns the per-repetition width plus, per element, the ``(bits, scale)`` in force
    when it is read — enough to locate and scale a bulk array without iterating its
    millions of repetitions.
    """
    scratch = _State(width=outer.width, scale=outer.scale)
    total = 0
    seen: dict[str, tuple[int, int]] = {}
    for fxy in block:
        kind = fxy[0]
        if kind == "2":
            _apply_operator(fxy, scratch)
            continue
        if kind != "0":
            msg = f"unsupported descriptor {fxy} inside a bulk replication block"
            raise BufrDecodeError(msg)
        bits = _element_bits(fxy, scratch)
        seen[fxy] = (bits, _ELEMENTS[fxy][0] + scratch.scale)
        total += bits
    return total, seen


def _apply_operator(fxy: str, state: _State) -> None:
    """Apply a 2-0x operator. 2-03 *definitions* are handled by the caller."""
    op, operand = int(fxy[1:3]), int(fxy[3:])
    if op == 1:  # change data width
        state.width = (operand - 128) if operand else 0
    elif op == 2:  # change scale  # noqa: PLR2004
        state.scale = (operand - 128) if operand else 0
    elif op == 3:  # change reference values  # noqa: PLR2004
        if operand == 0:  # cancel every new reference value
            state.refvals = {}
            state.refval_bits = 0
        elif operand == 255:  # noqa: PLR2004 — concludes the definition phase
            state.refval_bits = 0
        else:
            state.refval_bits = operand
            if state.refvals is None:
                state.refvals = {}
    else:
        msg = f"unsupported BUFR operator {fxy}"
        raise BufrDecodeError(msg)


def _read_new_refval(fxy: str, state: _State, reader: _Reader) -> None:
    """A 2-03-YYY definition: read a YYY-bit sign-magnitude reference for ``fxy``."""
    raw = reader.read(state.refval_bits)
    sign_bit = 1 << (state.refval_bits - 1)
    value = -(raw & (sign_bit - 1)) if raw & sign_bit else raw
    if state.refvals is None:
        state.refvals = {}
    state.refvals[fxy] = value


def _decode_value(fxy: str, raw: int, bits: int, state: _State) -> float | None:
    scale, table_ref, _ = _ELEMENTS[fxy]
    if raw == (1 << bits) - 1:  # all-ones is BUFR's "missing"
        return None
    reference = state.reference(fxy, table_ref)
    return (raw + reference) / (10.0 ** (scale + state.scale))


def _walk(reader: _Reader) -> tuple[dict[str, list], _Run]:
    """Walk the template, collecting metadata and locating the reflectivity run."""
    state = _State()
    values: dict[str, list] = {}
    capture: dict[str, _Run] = {}
    _walk_descriptors(list(_TEMPLATE), reader, state, values, capture)
    if "run" not in capture:
        msg = "reflectivity run not found in the message body"
        raise BufrDecodeError(msg)
    return values, capture["run"]


def _walk_descriptors(
    descriptors: list[str],
    reader: _Reader,
    state: _State,
    values: dict[str, list],
    capture: dict[str, _Run],
) -> None:
    """Interpret a descriptor list, advancing the bit cursor.

    The three bulk pixel arrays (probability, echo-top height, reflectivity) are
    replicated by the local 32-bit factor ``0-31-192``; those are *skipped
    arithmetically* rather than iterated, which is the whole reason this module is
    fast. Everything else — including the 32-radar site block and its nested delayed
    replication — is walked normally, because it is small.
    """
    queue = list(descriptors)
    i = 0
    while i < len(queue):
        fxy = queue[i]
        i += 1
        kind = fxy[0]

        if kind == "3":  # Table D sequence: splice its members in place
            queue[i:i] = list(_SEQUENCES[fxy])
            continue

        if kind == "2":
            op, operand = int(fxy[1:3]), int(fxy[3:])
            _apply_operator(fxy, state)
            if op == 3 and operand not in (0, 255):  # noqa: PLR2004
                # A 2-03-YYY definition phase: every element descriptor that follows,
                # up to 2-03-255, carries a new YYY-bit reference value in the data.
                while i < len(queue) and queue[i][0] == "0":
                    _read_new_refval(queue[i], state, reader)
                    i += 1
            continue

        if kind == "1":
            n_members, fixed = int(fxy[1:3]), int(fxy[3:])
            if fixed:
                count, bulk = fixed, False
                members = queue[i : i + n_members]
                i += n_members
            else:
                factor = queue[i]
                count = reader.read(_element_bits(factor, state))
                values.setdefault(factor, []).append(count)
                bulk = factor == "031192"
                members = queue[i + 1 : i + 1 + n_members]
                i += 1 + n_members
            if bulk:
                _skip_bulk(_expand(tuple(members)), count, reader, state, capture)
            else:
                # No _expand here: the recursive call splices sequences itself.
                for _ in range(count):
                    _walk_descriptors(members, reader, state, values, capture)
            continue

        bits = _element_bits(fxy, state)
        raw = reader.read(bits)
        values.setdefault(fxy, []).append(_decode_value(fxy, raw, bits, state))


def _skip_bulk(
    block: list[str],
    count: int,
    reader: _Reader,
    state: _State,
    capture: dict[str, _Run],
) -> None:
    """Step over a millions-long pixel array, recording the reflectivity one."""
    per_rep, seen = _block_bits(block, state)
    if _REFLECTIVITY in seen:
        bits, scale = seen[_REFLECTIVITY]
        capture["run"] = _Run(
            start_bit=reader.bit,
            count=count,
            bits=bits,
            scale=scale,
            reference=state.reference(_REFLECTIVITY, _ELEMENTS[_REFLECTIVITY][1]),
        )
    reader.skip(per_rep * count)


# -- public API ---------------------------------------------------------------


def decode(payload: bytes) -> ReflectivityGrid:
    """Decode a (optionally gzipped) REFLECTIVITE BUFR message into a dBZ grid.

    Raises :class:`BufrDecodeError` for anything unexpected — a wrong edition, a
    different originating centre or local table version, a template that no longer
    matches, or a truncated body — rather than returning a plausible-looking field.
    """
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            msg = f"malformed gzip payload: {type(exc).__name__}: {exc}"
            raise BufrDecodeError(msg) from exc

    meta, s4_start, s4_end = _sections(payload)
    reader = _Reader(payload, s4_start * 8, s4_end * 8)
    values, run = _walk(reader)

    rows = _one_int(values, "030022", "number of pixels per column")
    cols = _one_int(values, "030021", "number of pixels per row")
    if run.count != rows * cols:
        msg = f"reflectivity run holds {run.count} values, expected {rows}x{cols}={rows * cols}"
        raise BufrDecodeError(msg)

    field = _unpack(payload, run).reshape(rows, cols)
    geo = _geometry(values, rows, cols)
    return ReflectivityGrid(field=field, geo=geo, timestamp=_nominal_ts(meta))


def _unpack(data: bytes, run: _Run) -> np.ndarray:
    """Vectorised read of the run's big-endian ``bits``-wide words → dBZ float32.

    This is the whole point of the module: one ``np.unpackbits`` over ~3.2 MB instead
    of 2.36 M per-value descriptor evaluations.
    """
    first_byte, offset = divmod(run.start_bit, 8)
    span = run.count * run.bits
    need = -(-(offset + span) // 8)
    chunk = data[first_byte : first_byte + need]
    if len(chunk) < need:
        msg = "BUFR message truncated inside the reflectivity run"
        raise BufrDecodeError(msg)
    unpacked = np.unpackbits(np.frombuffer(chunk, dtype=np.uint8))[offset : offset + span]
    words = unpacked.reshape(run.count, run.bits)
    # Accumulate the big-endian bit columns by shift-or rather than widening the whole
    # 2.36 M x 11 bit matrix to uint32 and matrix-multiplying it: integer `@` is not
    # BLAS-backed, and the widened copy alone is ~104 MB of transient in the archiver.
    raw = np.zeros(run.count, dtype=np.uint32)
    for k in range(run.bits):
        raw <<= 1
        raw |= words[:, k]

    missing = raw == (1 << run.bits) - 1  # all-ones is BUFR's "missing"
    out = (raw.astype(np.float32) + run.reference) / np.float32(10.0**run.scale)
    out[missing] = np.nan
    return out


def _one_int(values: dict[str, list], fxy: str, label: str) -> int:
    return int(_one_float(values, fxy, label))


def _geometry(values: dict[str, list], rows: int, cols: int) -> GridGeo:
    """Build the ODIM-compatible GridGeo from the message's own projection fields.

    Row 0 is the **north** edge, matching LAME_D_EAU. Verified empirically in Phase 0
    by correlating the echo against the rain field (the alternative orientation scored
    0.3 % overlap against 77.6 %), and pinned by a regression test.
    """
    lat_ts = _one_float(values, "005195", "reference latitude")
    lon_0 = _one_float(values, "006198", "meridian parallel to the Y axis")
    ul_lat = _one_float(values, "005001", "upper-left latitude")
    ul_lon = _one_float(values, "006001", "upper-left longitude")
    xscale = _one_float(values, "005033", "pixel size on horizontal - 1")
    yscale = _one_float(values, "006033", "pixel size on horizontal - 2")
    if xscale <= 0 or yscale <= 0:
        msg = f"non-positive pixel size ({xscale}, {yscale})"
        raise BufrDecodeError(msg)

    projdef = _PROJDEF.format(lon_0=lon_0, lat_ts=lat_ts)
    x_ul, y_ul = transformer_for(projdef).transform(ul_lon, ul_lat)
    return GridGeo(
        projdef=projdef,
        xscale=xscale,
        yscale=yscale,
        xsize=cols,
        ysize=rows,
        x_ul=float(x_ul),
        y_ul=float(y_ul),
    )


def _one_float(values: dict[str, list], fxy: str, label: str) -> float:
    got = values.get(fxy)
    if not got or got[0] is None:
        msg = f"missing {label} ({fxy})"
        raise BufrDecodeError(msg)
    return float(got[0])


def _nominal_ts(meta: dict) -> int | None:
    """Section 1's nominal time as epoch seconds (UTC), or None if implausible."""
    from calendar import timegm  # noqa: PLC0415 — tiny stdlib helper, used once

    try:
        return timegm(
            (
                meta["year"],
                meta["month"],
                meta["day"],
                meta["hour"],
                meta["minute"],
                meta["second"],
                0,
                0,
                0,
            )
        )
    except ValueError, OverflowError:
        return None
