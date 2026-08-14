#!/usr/bin/env python
"""Reference decoder #2: decode REFLECTIVITE with ECMWF ecCodes instead of pybufrkit.

Dev-only. Written to evaluate ecCodes as the shipping decoder; it **lost** on every
axis and none of it ships — 145 MB of image weight, it still needs Météo-France's
local tables supplied by hand, and at ~61 s it is *slower* than pure-Python pybufrkit
(~52 s), because the cost is the descriptor walk, not bit extraction. The three
questions it was written to answer are all answered: ecCodes is not worth its cost.

**Kept as the second offline oracle.** Its whole value is independence: it shares no
code with ``bufr_decode_spike.py``, so when the two agree to the digit, the agreement
is evidence rather than a shared bug. That is the only trustworthy way to re-establish
ground truth if Météo-France bumps local table 14. The dBZ statistics it prints are
directly comparable with the pybufrkit spike's.

    docker compose -f docker-compose.local.yml run --rm \\
        -e PYTHONPATH=/app/.compare/eccodeslib django python scripts/bufr_eccodes_spike.py \\
        radar/tests/fixtures/meteofrance_reflectivite.bufr.gz
"""

from __future__ import annotations

import argparse
import gzip
import time
from pathlib import Path

import numpy as np

# Keys worth pulling once the message is unpacked. ecCodes names them from the
# BUFR tables, so these are the same elements the pybufrkit spike reported by
# six-digit id (021001, 030021, 030022, 005033, 005001 ...).
META_KEYS = (
    "edition",
    "masterTableNumber",
    "bufrHeaderCentre",
    "masterTablesVersionNumber",
    "localTablesVersionNumber",
    "dataCategory",
    "numberOfSubsets",
    "typicalDate",
    "typicalTime",
    "numberOfPixelsPerRow",
    "numberOfPixelsPerColumn",
    "pixelSizeOnHorizontal1",
    "pixelSizeOnHorizontal2",
    "projectionType",
    "coordinateGridType",
    "latitude",
    "longitude",
    # The projection parameters live in Météo-France *local* descriptors (005195,
    # 006198), so ecCodes names them from our converted table's French labels
    # rather than by a WMO-standard abbreviation.
    "latitudeDeReference",
    "longitudeDuMeridienParalleleALAxeDesY",
)


def install_size(root: Path) -> str:
    if not root.exists():
        return "n/a"
    total = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    return f"{total / 1e6:.0f} MB"


def report_env(libdir: Path) -> None:
    import eccodes

    print("-- environment --")
    print(f"  eccodes binding   {eccodes.__version__}")
    try:
        print(f"  ecCodes library   {eccodes.codes_get_api_version()}")
    except Exception as exc:
        print(f"  ecCodes library   unavailable: {exc}")
    print(f"  definitions path  {eccodes.codes_definition_path()}")
    print(f"  install weight    {install_size(libdir)}")


def decode(path: Path) -> tuple[dict, np.ndarray, float]:
    import eccodes

    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    started = time.monotonic()
    handle = eccodes.codes_new_from_message(raw)
    try:
        # Everything below section 3 stays packed until asked for; this is the
        # step that actually walks the descriptors and expands the pixel arrays.
        eccodes.codes_set(handle, "unpack", 1)
        elapsed = time.monotonic() - started

        meta = {}
        for key in META_KEYS:
            try:
                value = eccodes.codes_get(handle, key)
            except Exception as exc:
                value = f"<{type(exc).__name__}>"
            meta[key] = value

        field = np.asarray(
            eccodes.codes_get_array(handle, "horizontalReflectivity"), dtype=np.float64
        )
    finally:
        eccodes.codes_release(handle)
    return meta, field, elapsed


def report(meta: dict, field: np.ndarray) -> None:
    print("\n-- message metadata --")
    for key, value in meta.items():
        print(f"  {key:36} {value}")

    missing = eccodes_missing(field)
    finite = field[~missing]
    print("\n-- reflectivity --")
    print(f"  values            {field.size}")
    print(f"  missing           {int(missing.sum())} ({missing.mean():.1%})")
    if finite.size:
        print(f"  dBZ min/max       {finite.min():.1f} / {finite.max():.1f}")
        p50, p99, p999 = np.percentile(finite, [50, 99, 99.9])
        print(f"  dBZ p50/p99/p99.9 {p50:.1f} / {p99:.1f} / {p999:.1f}")
        for floor in (0, 5, 10, 20, 35):
            print(f"  fraction >= {floor:2d} dBZ  {float((finite >= floor).mean()):7.3%}")

    rows, cols = meta.get("numberOfPixelsPerColumn"), meta.get("numberOfPixelsPerRow")
    print("\n-- shape --")
    if isinstance(rows, int) and isinstance(cols, int):
        print(f"  declared          {rows} x {cols} = {rows * cols}")
        print(f"  values            {field.size}")
        print("  MATCH" if field.size == rows * cols else "  MISMATCH")


def eccodes_missing(field: np.ndarray) -> np.ndarray:
    """ecCodes encodes absent values as a huge *negative* sentinel (~-1e100), not NaN."""
    return ~np.isfinite(field) | (np.abs(field) > 1e6)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".compare/raw/REFLECTIVITE.h5", type=Path)
    parser.add_argument("--libdir", default=".compare/eccodeslib", type=Path)
    args = parser.parse_args()

    report_env(args.libdir)
    try:
        meta, field, elapsed = decode(args.path)
    except Exception as exc:
        print(f"\nDECODE FAILED: {type(exc).__name__}: {exc}")
        print("\nIf this is a missing-table error, ecCodes lacks Météo-France local")
        print("table 14 for centre 85 and would need it supplied in ecCodes' own")
        print("format (element.table / sequence.def), not theperk08's CSVs.")
        raise SystemExit(1) from exc

    print(f"\ndecode + unpack: {elapsed:.2f}s")
    report(meta, field)


if __name__ == "__main__":
    main()
