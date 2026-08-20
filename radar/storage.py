"""Tile disk storage — paths, atomic writes, UTC-date foldering.

This is the single home for all tile path math and disk I/O so the UTC-date
rule and the atomic-write rule live in exactly one place; both the archiver and
the Django fallback view import from here. All date math is **UTC** and explicit
(``time.gmtime``) — never via Django's display ``TIME_ZONE``.

Tile layout (authoritative)::

    {TILE_ROOT}/{provider}/{YYYY-MM-DD}/{ts}/{z}/{x}/{y}.png
                └ source ┘ └ UTC date ┘ └epoch┘ └ slippy indices ┘

This module owns only the canonical layout above; the URL that maps onto it is the
tile view's business.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

from django.conf import settings

# A tile-date directory name: strictly YYYY-MM-DD.
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def utc_date(ts: int) -> str:
    """Return the UTC calendar date of an epoch-seconds timestamp as YYYY-MM-DD.

    Mirrors the frontend's ``new Date(ts*1000).toISOString().slice(0,10)``.
    """
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def tile_root() -> Path:
    """The archive root (``TILE_ROOT``)."""
    return Path(settings.TILE_ROOT)


def provider_root(provider: str) -> Path:
    """The per-provider subtree of the archive (``{TILE_ROOT}/{provider}``)."""
    return tile_root() / provider


def tile_path(  # noqa: PLR0913 — provider + tile coords are the irreducible inputs
    provider: str,
    ts: int,
    z: int,
    x: int,
    y: int,
    *,
    date: str | None = None,
) -> Path:
    """Absolute on-disk path for a tile under its provider subtree.

    ``date`` may be supplied (already validated by the caller) to avoid recomputing
    it; when omitted it is derived from ``ts`` (the canonical UTC date).
    """
    d = date if date is not None else utc_date(ts)
    return provider_root(provider) / d / str(ts) / str(z) / str(x) / f"{y}.png"


def tile_exists(  # noqa: PLR0913 — provider + tile coords are the irreducible inputs
    provider: str,
    ts: int,
    z: int,
    x: int,
    y: int,
    *,
    date: str | None = None,
) -> bool:
    return tile_path(provider, ts, z, x, y, date=date).is_file()


def write_tile(  # noqa: PLR0913, PLR0917 — provider + tile coords + payload are irreducible
    provider: str,
    ts: int,
    z: int,
    x: int,
    y: int,
    data: bytes,
    *,
    date: str | None = None,
) -> Path:
    """Atomically persist ``data`` as the tile PNG and return its path.

    Writes to ``…/{y}.png.tmp-<pid>-<uuid>`` then ``Path.replace()`` to the final
    name so a half-written tile is never visible/served. The uuid makes the temp
    name unique per write, so two concurrent writers of the same tile (even in one
    process) never share a temp file. Parent dirs created with ``exist_ok=True``.
    """
    final = tile_path(provider, ts, z, x, y, date=date)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f"{final.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp.write_bytes(data)
    tmp.replace(final)  # atomic on the same filesystem
    return final


def day_dirs(provider: str) -> list[Path]:
    """All YYYY-MM-DD day directories under a provider's subtree, sorted."""
    root = provider_root(provider)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and DATE_DIR_RE.match(p.name))


def provider_dirs() -> list[str]:
    """Provider subtree names present on disk (immediate non-date subdirs of the root).

    Lets the janitor purge day dirs under *every* provider's subtree, including a
    provider that was enabled once and later turned off. The ``DATE_DIR_RE`` guard
    keeps a day dir from ever being mistaken for a provider name, so the two levels
    of the tree can never be confused.
    """
    root = tile_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not DATE_DIR_RE.match(p.name))


def dir_size(path: Path) -> int:
    """Total size in bytes of all files under ``path`` (0 if it doesn't exist)."""
    if not path.exists():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                total += fp.stat().st_size
            except OSError:  # file vanished mid-walk (concurrent janitor) — skip
                continue
    return total


def tile_tree_bytes() -> int:
    """Total bytes of the whole tile archive across all providers (storage gauge)."""
    return dir_size(tile_root())
