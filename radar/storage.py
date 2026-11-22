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

Forecast layout (``radar.forecast``), a separate root because forecast frames are
disposable and superseded every run, while the archive is immutable per frame::

    {FORECAST_ROOT}/{run_ts}/{valid_ts}/{z}/{x}/{y}.png
    {FORECAST_ROOT}/{run_ts}/{valid_ts}/.done      every tile of that frame written
    {FORECAST_ROOT}/{run_ts}/run.json              every frame done: the run's record

The markers make the forecast job idempotent and let it rebuild its published-run
pointer from disk after a Redis flush.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from django.conf import settings

# A tile-date directory name: strictly YYYY-MM-DD.
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A forecast run directory name: an epoch in seconds.
RUN_DIR_RE = re.compile(r"^\d+$")
FORECAST_FRAME_DONE = ".done"
FORECAST_RUN_RECORD = "run.json"


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
    return _atomic_write(tile_path(provider, ts, z, x, y, date=date), data)


def write_tile_sized(  # noqa: PLR0913, PLR0917 — same inputs as write_tile
    provider: str,
    ts: int,
    z: int,
    x: int,
    y: int,
    data: bytes,
    *,
    date: str | None = None,
) -> int:
    """``write_tile``, returning the tile's disk usage (``disk_bytes``) for the gauge.

    One call, so the archiver pays a single ``sync_to_async`` hop per tile for the
    write and the ``stat`` together.
    """
    return disk_bytes(write_tile(provider, ts, z, x, y, data, date=date))


def _atomic_write(final: Path, data: bytes) -> Path:
    """Write ``data`` to ``final`` via a unique temp name + rename (never half-visible)."""
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


def disk_bytes(path: str | os.PathLike) -> int:
    """Bytes ``path`` actually occupies on disk (allocated blocks, not ``st_size``).

    The archive is millions of small PNGs, each rounded up to whole filesystem
    blocks, plus a directory inode per frame/zoom/column — so the apparent size
    undercounts real usage by roughly a fifth. ``st_blocks`` is always in 512-byte
    units, whatever the filesystem's block size. Raises ``OSError`` if it is gone.
    """
    return os.lstat(path).st_blocks * 512


def dir_size(path: Path) -> int:
    """Disk usage in bytes of ``path`` and everything under it (0 if it doesn't exist).

    Counts allocated blocks of every file **and directory** (see ``disk_bytes``), so
    it matches what ``du -s`` reports and what the shared disk actually loses.
    """
    if not path.exists():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        base = Path(dirpath)
        for entry in (base, *(base / name for name in filenames)):
            try:
                total += disk_bytes(entry)
            except OSError:  # vanished mid-walk (concurrent janitor) — skip
                continue
    return total


def tile_tree_bytes() -> int:
    """Disk usage of the whole tile archive across all providers (storage gauge)."""
    return dir_size(tile_root())


# -- forecast -----------------------------------------------------------------


def forecast_root() -> Path:
    """The forecast root (``FORECAST_ROOT``), outside the archive."""
    return Path(settings.FORECAST_ROOT)


def forecast_run_dir(run_ts: int) -> Path:
    return forecast_root() / str(run_ts)


def forecast_tile_path(run_ts: int, valid_ts: int, z: int, x: int, y: int) -> Path:
    return forecast_run_dir(run_ts) / str(valid_ts) / str(z) / str(x) / f"{y}.png"


def write_forecast_tile(  # noqa: PLR0913, PLR0917 — run + valid + tile coords + payload
    run_ts: int, valid_ts: int, z: int, x: int, y: int, data: bytes
) -> Path:
    """Atomically persist one forecast tile (same temp + rename rule as the archive)."""
    return _atomic_write(forecast_tile_path(run_ts, valid_ts, z, x, y), data)


def forecast_frame_done(run_ts: int, valid_ts: int) -> bool:
    return (forecast_run_dir(run_ts) / str(valid_ts) / FORECAST_FRAME_DONE).is_file()


def mark_forecast_frame_done(run_ts: int, valid_ts: int, tiles: int) -> None:
    """Record that every tile of one forecast frame is on disk (``tiles`` non-empty)."""
    path = forecast_run_dir(run_ts) / str(valid_ts) / FORECAST_FRAME_DONE
    _atomic_write(path, str(tiles).encode())


def write_forecast_run(run_ts: int, record: dict) -> None:
    """Atomically write a run's record — only once every one of its frames is done."""
    _atomic_write(forecast_run_dir(run_ts) / FORECAST_RUN_RECORD, json.dumps(record).encode())


def read_forecast_run(run_ts: int) -> dict | None:
    """A run's record, or ``None`` if the run is incomplete or the record is not its own.

    The record must name this very run: it is what rebuilds the Redis pointer after a
    flush, and a record that cannot would wedge the forecast job instead.
    """
    try:
        record = json.loads((forecast_run_dir(run_ts) / FORECAST_RUN_RECORD).read_bytes())
    except OSError, ValueError:
        return None
    if not isinstance(record, dict) or record.get("run") != run_ts:
        return None
    return record


def forecast_runs() -> list[int]:
    """Every run directory on disk, complete or not, oldest first."""
    root = forecast_root()
    if not root.is_dir():
        return []
    return sorted(int(p.name) for p in root.iterdir() if p.is_dir() and RUN_DIR_RE.match(p.name))


def remove_forecast_run(run_ts: int) -> int:
    """Delete one run's directory; returns the bytes freed."""
    path = forecast_run_dir(run_ts)
    freed = dir_size(path)
    shutil.rmtree(path, ignore_errors=True)
    return freed
