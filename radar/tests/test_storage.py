"""Tile storage — UTC paths, atomic writes, dir sizing.

Pure disk I/O against ``tmp_path``; no DB, no network.
"""

from __future__ import annotations

import os
import threading

import pytest
from django.test import override_settings

from radar import storage

PROVIDER = "rainviewer"


@pytest.fixture
def tile_root(tmp_path):
    with override_settings(TILE_ROOT=str(tmp_path)):
        yield tmp_path


def test_utc_date_is_utc_not_local():
    # 1683849599 = 2023-05-11 23:59:59 UTC (still the 12th in Europe/Paris).
    assert storage.utc_date(1683849599) == "2023-05-11"
    # 1683849600 = 2023-05-12 00:00:00 UTC — the boundary tips over.
    assert storage.utc_date(1683849600) == "2023-05-12"


def test_tile_path_layout_is_provider_scoped(tile_root):
    p = storage.tile_path(PROVIDER, 1683790200, 5, 16, 11)
    # 1683790200 = 2023-05-11 08:50:00 UTC; provider segment leads the day dir.
    assert p == tile_root / PROVIDER / "2023-05-11" / "1683790200" / "5" / "16" / "11.png"


def test_tile_paths_differ_by_provider(tile_root):
    rv = storage.tile_path("rainviewer", 1683790200, 5, 16, 11)
    mf = storage.tile_path("meteofrance", 1683790200, 5, 16, 11)
    assert rv != mf
    assert rv.parents[4].name == "rainviewer"
    assert mf.parents[4].name == "meteofrance"


def test_write_tile_creates_exact_path_atomically(tile_root):
    data = b"\x89PNG fake"
    written = storage.write_tile(PROVIDER, 1683790200, 5, 16, 11, data)
    assert written.read_bytes() == data
    assert written == tile_root / PROVIDER / "2023-05-11" / "1683790200" / "5" / "16" / "11.png"
    # No leftover temp files.
    leftovers = list(written.parent.glob("*.tmp-*"))
    assert leftovers == []


def test_tile_exists(tile_root):
    assert storage.tile_exists(PROVIDER, 1683790200, 5, 16, 11) is False
    storage.write_tile(PROVIDER, 1683790200, 5, 16, 11, b"x")
    assert storage.tile_exists(PROVIDER, 1683790200, 5, 16, 11) is True


def test_path_stays_under_tile_root(tile_root):
    p = storage.tile_path(PROVIDER, 1683790200, 5, 16, 11)
    assert tile_root in p.parents


def _du(path):
    """Independent ``du -s`` equivalent: allocated blocks of every entry, root included."""
    entries = [path, *path.rglob("*")]
    return sum(os.lstat(p).st_blocks * 512 for p in entries)


def test_dir_size_and_tree_bytes_sum_across_providers(tile_root):
    storage.write_tile("rainviewer", 1, 5, 16, 11, b"a" * 10)
    storage.write_tile("meteofrance", 1, 5, 16, 12, b"b" * 20)
    rv = storage.dir_size(storage.provider_root("rainviewer"))
    mf = storage.dir_size(storage.provider_root("meteofrance"))
    assert rv == _du(storage.provider_root("rainviewer"))
    assert mf == _du(storage.provider_root("meteofrance"))
    # tile_tree_bytes is global (the gauge covers the whole archive, both providers),
    # plus the root directory's own block.
    assert storage.tile_tree_bytes() == rv + mf + storage.disk_bytes(tile_root)


def test_dir_size_counts_allocated_blocks_not_apparent_size(tile_root):
    # A 5000-byte tile is rounded up to whole blocks on disk (5000 is no multiple of
    # 512, so the two can never agree; big enough that no filesystem inlines it), and
    # its day / frame / zoom / column directories take blocks of their own: the gauge
    # must count what the disk loses, not the bytes of content.
    tile = storage.write_tile(PROVIDER, 1, 5, 16, 11, b"a" * 5000)
    file_bytes = storage.disk_bytes(tile)
    assert file_bytes > 5000
    assert file_bytes % 512 == 0
    assert storage.dir_size(storage.provider_root(PROVIDER)) > file_bytes


def test_write_tile_sized_returns_the_tiles_disk_usage(tile_root):
    n = storage.write_tile_sized(PROVIDER, 1683790200, 5, 16, 11, b"x" * 5000)
    path = storage.tile_path(PROVIDER, 1683790200, 5, 16, 11)
    assert path.read_bytes() == b"x" * 5000
    assert n == storage.disk_bytes(path) >= 5000


def test_day_dirs_lists_only_date_dirs_sorted(tile_root):
    storage.write_tile(PROVIDER, 1683849600, 5, 16, 11, b"x")  # 2023-05-12
    storage.write_tile(PROVIDER, 1683790200, 5, 16, 11, b"x")  # 2023-05-11
    (storage.provider_root(PROVIDER) / "not-a-date").mkdir()
    (storage.provider_root(PROVIDER) / "tmpfile").write_text("ignore")
    names = [d.name for d in storage.day_dirs(PROVIDER)]
    assert names == ["2023-05-11", "2023-05-12"]


def test_day_dirs_empty_for_unknown_provider(tile_root):
    assert storage.day_dirs("meteofrance") == []


def test_dir_size_zero_for_missing(tmp_path):
    assert storage.dir_size(tmp_path / "nope") == 0


def test_concurrent_writes_same_tile_all_succeed(tile_root):
    # Per-write unique temp names: many writers of the SAME tile in one process
    # must not collide on a shared temp file (no FileNotFoundError on replace).
    errors: list[Exception] = []

    def writer() -> None:
        try:
            storage.write_tile(PROVIDER, 1683790200, 5, 16, 11, b"radartile")
        except Exception as exc:  # noqa: BLE001 — record any write failure
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for thr in threads:
        thr.start()
    for thr in threads:
        thr.join()

    assert errors == []
    assert storage.tile_exists(PROVIDER, 1683790200, 5, 16, 11)
    assert list(storage.tile_path(PROVIDER, 1683790200, 5, 16, 11).parent.glob("*.tmp-*")) == []
