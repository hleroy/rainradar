#!/usr/bin/env python
"""Reference decoder #1: Météo-France REFLECTIVITE BUFR -> dBZ grid, via pybufrkit.

Dev-only, not part of the app. Originally the Phase-0a gate spike ("can this product
be decoded at all?"); that question is long answered — the shipped decoder is
``radar/providers/bufr_decode.py``, which walks the template arithmetically in ~0.02 s
instead of the ~52 s this takes.

**Kept as an offline oracle**, for one scenario: Météo-France bumping local table
version 14, which ``bufr_decode`` refuses outright rather than mis-decode. Re-deriving
ground truth for a new version means a *general* decoder that has never seen our
hand-transcribed element widths — this one, cross-checked against ecCodes
(``bufr_eccodes_spike.py``). On any ordinary day you want neither: the pinned
statistics live in ``radar/tests/test_bufr_decode.py`` and are checked on every run.

It uses pybufrkit (pure Python, no eccodes / no GDAL — keeping the lean-deps
posture) with:

  * pybufrkit's bundled WMO master table 0 v16, and
  * Météo-France local table v14 for centre 85, converted from theperk08's
    corrected CSVs by ``scripts/bufr_tables.py``.

What it reports: decode wall time, the image geometry (rows/cols, pixel size,
corner coordinates, projection hints), and dBZ statistics — enough to say whether
the field is plausible and comparable with LAME_D_EAU.

    docker compose -f docker-compose.local.yml run --rm \\
        -e PYTHONPATH=/app/.compare/pylibs django python scripts/bufr_decode_spike.py \\
        radar/tests/fixtures/meteofrance_reflectivite.bufr.gz
"""

from __future__ import annotations

import argparse
import gzip
import pickle
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

# Descriptors we care about, by six-digit id.
REFLECTIVITY = "021001"
GEOMETRY = {
    "030021": "pixels per row",
    "030022": "pixels per column",
    "005033": "pixel size horizontal-1 (m)",
    "006033": "pixel size horizontal-2 (m)",
    "029001": "projection type",
    "029002": "coordinate grid type",
    "029192": "geodetic system (local)",
    "030031": "picture type",
    "030032": "picture combination",
    "005001": "latitude (high accuracy)",
    "006001": "longitude (high accuracy)",
    "008021": "time significance",
    "001192": "composite indicator (local)",
    "025192": "overlap composition method (local)",
    "025009": "radiation/quality",
}


def read_message(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:4] != b"BUFR":
        msg = f"not a BUFR message: {raw[:4]!r}"
        raise SystemExit(msg)
    return raw


def decode(msg: bytes, local_tables: Path):
    from pybufrkit.decoder import Decoder

    if not (local_tables / "0" / "85_0" / "14" / "TableB.json").exists():
        msg_ = f"local tables missing under {local_tables}\nrun: python scripts/bufr_tables.py"
        raise SystemExit(msg_)

    decoder = Decoder(tables_local_dir=str(local_tables))
    started = time.monotonic()
    bufr = decoder.process(msg)
    elapsed = time.monotonic() - started
    print(f"decode OK in {elapsed:.1f}s")
    return bufr, elapsed


def collect_values(bufr) -> dict[str, list]:
    """Group every decoded value in subset 0 by its descriptor id.

    Walks the flat (descriptor, value) stream rather than the query DSL, because a
    spike wants to *see* everything the message carries, including the parts we
    have not yet decided we need.
    """
    values: dict[str, list] = defaultdict(list)
    for descriptor, value in zip(
        bufr.template_data.value.decoded_descriptors_all_subsets[0],
        bufr.template_data.value.decoded_values_all_subsets[0],
        strict=True,
    ):
        values[f"{descriptor.id:06d}"].append(value)
    return values


def report_geometry(values: dict[str, list]) -> None:
    print("\n-- geometry & metadata --")
    for key, label in GEOMETRY.items():
        got = values.get(key)
        if not got:
            continue
        shown = got[:4]
        suffix = f"  (+{len(got) - 4} more)" if len(got) > 4 else ""
        print(f"  {key}  {label:34} {shown}{suffix}")


def report_reflectivity(values: dict[str, list]) -> np.ndarray | None:
    raw = values.get(REFLECTIVITY)
    print("\n-- reflectivity (021001) --")
    if not raw:
        print("  !! no 021001 values decoded")
        return None
    arr = np.array([np.nan if v is None else float(v) for v in raw], dtype=np.float32)
    finite = arr[np.isfinite(arr)]
    missing = arr.size - finite.size
    print(f"  values            {arr.size}")
    print(f"  missing (None)    {missing} ({missing / arr.size:.1%})")
    if finite.size:
        print(f"  dBZ min/max       {finite.min():.1f} / {finite.max():.1f}")
        p50, p90, p99, p999 = np.percentile(finite, [50, 90, 99, 99.9])
        print(f"  dBZ p50/p90/p99/p99.9  {p50:.1f} / {p90:.1f} / {p99:.1f} / {p999:.1f}")
        for floor in (0, 5, 10, 20, 35):
            frac = float((finite >= floor).mean())
            print(f"  fraction >= {floor:2d} dBZ  {frac:7.3%}")
    return arr


def check_shape(arr: np.ndarray, values: dict[str, list]) -> None:
    """Does the value count match rows x columns? That is the reshape sanity test."""
    rows = values.get("030022")
    cols = values.get("030021")
    print("\n-- shape --")
    if not rows or not cols:
        print("  !! no 030021/030022 to reshape against")
        return
    n_rows, n_cols = int(rows[0]), int(cols[0])
    print(f"  declared          {n_rows} rows x {n_cols} cols = {n_rows * n_cols}")
    print(f"  reflectivity vals {arr.size}")
    if arr.size == n_rows * n_cols:
        print("  MATCH — the field reshapes directly to the image grid")
    else:
        print(f"  MISMATCH — difference {arr.size - n_rows * n_cols}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".compare/raw/REFLECTIVITE.h5", type=Path)
    parser.add_argument("--tables", default=".compare/bufrtables/pybufrkit", type=Path)
    parser.add_argument(
        "--dump",
        default=".compare/raw/REFLECTIVITE_decoded.pkl",
        type=Path,
        help="cache the grouped values here so later tuning skips the slow decode",
    )
    args = parser.parse_args()

    msg = read_message(args.path)
    print(f"{args.path}: {len(msg) / 1e6:.2f} MB raw BUFR")

    bufr, _elapsed = decode(msg, args.tables)
    values = collect_values(bufr)
    print(f"\ndistinct descriptors with values: {len(values)}")

    report_geometry(values)
    arr = report_reflectivity(values)
    if arr is not None:
        check_shape(arr, values)

    args.dump.parent.mkdir(parents=True, exist_ok=True)
    with args.dump.open("wb") as fh:
        pickle.dump(dict(values), fh, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"\ncached decoded values -> {args.dump}")


if __name__ == "__main__":
    main()
