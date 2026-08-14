#!/usr/bin/env python
"""Météo-France local BUFR tables -> pybufrkit JSON *and* ecCodes definitions.

Dev-only, not part of the app: it feeds the two offline reference decoders
(``bufr_decode_spike.py``, ``bufr_eccodes_spike.py``), never the shipped one.
``radar/providers/bufr_decode.py`` embeds the handful of table entries its one
template touches directly, so it needs nothing from here.

Kept for the case ``bufr_decode`` is built to refuse: a local-table-version bump,
after which both oracles need tables regenerated from a refreshed CSV set before they
can re-establish ground truth.

Our REFLECTIVITE messages declare master table 0 v16 (which pybufrkit ships) plus
**local** table v14 from centre 85 (Toulouse), which it does not. Twelve of the 56
section-3 descriptors live in that local table — including ``0-31-192``, a 32-bit
"facteur super elargi de repetition differe" used as a delayed-replication factor,
so the message cannot even be walked without it.

Météo-France publishes those tables but, per the theperk08 project, ships them
malformed; that project's corrected CSVs are the practical source. This module
converts them into both decoders' formats — the JSON layout pybufrkit expects:

    <root>/0/85_0/14/TableB.json     {"001192": [name, unit, scale, ref, bits, ...]}
    <root>/0/85_0/14/TableD.json     {"301194": [name, ["001003", "001200", ...]]}

and, via ``build_eccodes``, the flat ``element.table`` / ``sequence.def`` pair ecCodes
reads from ``ECCODES_DEFINITION_PATH``.

Source CSVs (MIT-licensed, see NOTICE printed by ``main``):
    https://github.com/theperk08/Meteo_France_Radars/tree/main/tables
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# theperk08's CSVs are semicolon-separated and Latin-1 (accented French names).
_ENCODING = "latin-1"

NOTICE = (
    "BUFR local tables derived from theperk08/Meteo_France_Radars (MIT licence).\n"
    "Météo-France's own tables_bufr_361 distribution is malformed; that project\n"
    "publishes a corrected set, which is what we convert here."
)


def _fxy(f: str, x: str, y: str) -> str:
    """('0', '1', '192') -> '001192' — the six-digit id pybufrkit keys tables by."""
    return f"{int(f):01d}{int(x):02d}{int(y):03d}"


def parse_table_b(path: Path) -> dict[str, list]:
    """CSV rows ``F;X;Y;name;unit;scale;reference;bits`` -> pybufrkit TableB.json."""
    table: dict[str, list] = {}
    for line in path.read_text(encoding=_ENCODING).splitlines():
        parts = [c.strip() for c in line.split(";")]
        if len(parts) < 8 or not parts[0].isdigit():
            continue
        f, x, y, name, unit, scale, ref, bits = parts[:8]
        try:
            entry = [name, unit, int(scale), int(ref), int(bits)]
        except ValueError:
            continue  # a malformed row: skip rather than poison the table
        # pybufrkit expects the CREX trio too; BUFR decoding never reads them, so
        # mirror the BUFR unit/scale and give a width wide enough to be harmless.
        table[_fxy(f, x, y)] = [*entry, unit, int(scale), 0]
    return table


def parse_table_d(path: Path) -> dict[str, list]:
    """CSV with a leading ``F;X;Y`` row then blank-keyed continuation rows.

    3;01;194;  0;01;003
     ;  ;   ;  0;01;200     <- continuation of 301194
    """
    table: dict[str, list] = {}
    current: list[str] | None = None
    for line in path.read_text(encoding=_ENCODING).splitlines():
        parts = [c.strip() for c in line.split(";")]
        if len(parts) < 6:
            continue
        seq_f, seq_x, seq_y, mem_f, mem_x, mem_y = parts[:6]
        if not mem_f.isdigit():
            continue
        if seq_f.isdigit():  # a new sequence starts here
            current = []
            table[_fxy(seq_f, seq_x, seq_y)] = ["", current]
        if current is None:
            continue  # continuation before any header: malformed, skip
        current.append(_fxy(mem_f, mem_x, mem_y))
    return table


def build(
    src: Path,
    dest: Path,
    centre: int = 85,
    subcentre: int = 0,
    local_version: int = 14,
    master_table: int = 0,
) -> Path:
    """Write ``<dest>/<master>/<centre>_<sub>/<local_version>/Table{B,D}.json``."""
    table_b = parse_table_b(src / f"localtabb_{centre}_{local_version}.csv")
    table_d = parse_table_d(src / f"localtabd_{centre}_{local_version}.csv")

    out = dest / str(master_table) / f"{centre}_{subcentre}" / str(local_version)
    out.mkdir(parents=True, exist_ok=True)
    (out / "TableB.json").write_text(json.dumps(table_b, indent=1, ensure_ascii=False))
    (out / "TableD.json").write_text(json.dumps(table_d, indent=1, ensure_ascii=False))
    # pybufrkit also looks for code/flag tables; an empty one keeps it from warning.
    (out / "code_and_flag.json").write_text("{}")
    print(f"{out}/TableB.json  {len(table_b)} entries")
    print(f"{out}/TableD.json  {len(table_d)} entries")
    return out


def _camel(name: str) -> str:
    """A stable ecCodes-style abbreviation for a French table-B description."""
    words = "".join(c if c.isalnum() else " " for c in name).split()
    if not words:
        return "unknownElement"
    head, *rest = words
    return head.lower() + "".join(w.capitalize() for w in rest)


def _ec_type(unit: str, scale: int) -> str:
    """Map a Table B unit/scale onto ecCodes' element types."""
    lowered = unit.lower()
    if "code table" in lowered:
        return "table"
    if "flag table" in lowered:
        return "flag"
    if "ccitt" in lowered or "character" in lowered:
        return "string"
    return "double" if scale != 0 else "long"


def build_eccodes(
    src: Path,
    dest: Path,
    centre: int = 85,
    subcentre: int = 0,
    local_version: int = 14,
    master_table: int = 0,
) -> Path:
    """Write the same tables in ecCodes' own layout and syntax.

    ecCodes looks for local tables at
    ``<defs>/bufr/tables/<master>/local/<local_version>/<centre>/<subcentre>/``
    and wants a pipe-separated ``element.table`` plus a ``sequence.def``, rather
    than pybufrkit's JSON. Neither pybufrkit nor ecCodes ships Météo-France's
    local table 14, so both need this conversion; only the target syntax differs.
    """
    table_b = parse_table_b(src / f"localtabb_{centre}_{local_version}.csv")
    table_d = parse_table_d(src / f"localtabd_{centre}_{local_version}.csv")

    out = dest / "bufr" / "tables" / str(master_table) / "local" / str(local_version)
    out = out / str(centre) / str(subcentre)
    out.mkdir(parents=True, exist_ok=True)

    header = (
        "#code|abbreviation|type|name|unit|scale|reference|width|crex_unit|crex_scale|crex_width"
    )
    rows = [header]
    seen: set[str] = set()
    for code in sorted(table_b):
        name, unit, scale, ref, width = table_b[code][:5]
        abbrev = _camel(name)
        while abbrev in seen:  # ecCodes keys by abbreviation too — keep them unique
            abbrev += "X"
        seen.add(abbrev)
        rows.append(
            f"{code}|{abbrev}|{_ec_type(unit, scale)}|{name.upper()}|{unit}|"
            f"{scale}|{ref}|{width}|{unit}|{scale}|0"
        )
    (out / "element.table").write_text("\n".join(rows) + "\n", encoding="utf-8")

    seq_lines = [
        f'"{code}" = [ {", ".join(members)} ]' for code, (_n, members) in sorted(table_d.items())
    ]
    (out / "sequence.def").write_text("\n".join(seq_lines) + "\n", encoding="utf-8")

    print(f"{out}/element.table  {len(rows) - 1} entries")
    print(f"{out}/sequence.def   {len(seq_lines)} sequences")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default=".compare/bufrtables", type=Path)
    parser.add_argument("--dest", default=".compare/bufrtables/pybufrkit", type=Path)
    parser.add_argument(
        "--eccodes-dest",
        default=".compare/bufrtables/eccodes",
        type=Path,
        help="also emit ecCodes-format tables here (point ECCODES_DEFINITION_PATH at it)",
    )
    args = parser.parse_args()
    print(NOTICE + "\n")
    build(args.src, args.dest)
    build_eccodes(args.src, args.eccodes_dest)


if __name__ == "__main__":
    main()
