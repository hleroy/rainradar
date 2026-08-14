"""Tile storage — UTC paths, atomic writes, dir sizing, legacy migration.

Pure disk I/O against ``tmp_path``; no DB, no network.
"""

from __future__ import annotations

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


def test_dir_size_and_tree_bytes_sum_across_providers(tile_root):
    storage.write_tile("rainviewer", 1, 5, 16, 11, b"a" * 10)
    storage.write_tile("meteofrance", 1, 5, 16, 12, b"b" * 20)
    # tile_tree_bytes is global (the gauge covers the whole archive, both providers).
    assert storage.tile_tree_bytes() == 30
    assert storage.dir_size(storage.provider_root("rainviewer")) == 10
    assert storage.dir_size(storage.provider_root("meteofrance")) == 20


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


# -- legacy layout migration --------------------------------------------------


def _write_legacy_tile(root, date, ts, z, x, y, data=b"legacy"):  # noqa: PLR0913, PLR0917
    """Create a legacy root-level (no provider segment) tile on disk."""
    p = root / date / str(ts) / str(z) / str(x) / f"{y}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_migrate_legacy_layout_moves_and_is_idempotent(tile_root):
    _write_legacy_tile(tile_root, "2023-05-11", 1683790200, 5, 16, 11)
    _write_legacy_tile(tile_root, "2023-05-12", 1683849600, 5, 16, 11)

    moved = storage.migrate_legacy_layout()
    assert moved == 2
    # Tiles now live under rainviewer/; the root-level day dirs are gone.
    assert storage.tile_exists("rainviewer", 1683790200, 5, 16, 11)
    assert storage.tile_exists("rainviewer", 1683849600, 5, 16, 11)
    assert not (tile_root / "2023-05-11").exists()

    # Idempotent: a second pass finds nothing to move.
    assert storage.migrate_legacy_layout() == 0


def test_migrate_legacy_layout_skips_existing_target(tile_root):
    # A day already present under rainviewer/ must not be clobbered by a stray
    # legacy dir of the same name (idempotency guard).
    storage.write_tile("rainviewer", 1683790200, 5, 16, 11, b"canonical")
    _write_legacy_tile(tile_root, "2023-05-11", 1683790200, 5, 16, 11, data=b"stale")

    moved = storage.migrate_legacy_layout()
    assert moved == 0
    # The canonical tile is untouched; the stale legacy dir is left where it is.
    assert storage.tile_path("rainviewer", 1683790200, 5, 16, 11).read_bytes() == b"canonical"
    assert (tile_root / "2023-05-11").exists()


def test_migrate_legacy_layout_no_root(tmp_path):
    with override_settings(TILE_ROOT=str(tmp_path / "does-not-exist")):
        assert storage.migrate_legacy_layout() == 0
