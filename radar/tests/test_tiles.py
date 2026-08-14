"""Tile-matrix maths. Pure logic — no DB, no I/O."""

from __future__ import annotations

import pytest

from radar import tiles

DEFAULT_BBOX = (41.2, 51.5, -6.0, 9.7)  # S, N, W, E — France incl. Corsica
ZOOM_MIN = 3
ZOOM_MAX = 7


def test_matrix_total_is_62_for_default_bbox():
    assert tiles.matrix_size(DEFAULT_BBOX, ZOOM_MIN, ZOOM_MAX) == 62


def test_per_zoom_tile_counts():
    matrix = tiles.tile_matrix(DEFAULT_BBOX, ZOOM_MIN, ZOOM_MAX)
    counts = {z: sum(1 for (tz, _x, _y) in matrix if tz == z) for z in range(3, 8)}
    assert counts == {3: 2, 4: 2, 5: 4, 6: 12, 7: 42}


def test_zoom3_lowest_x_and_y():
    # West=-6 -> x=3 ; North=51.5 -> y=2 (Y grows southward).
    assert tiles.lon_to_tile_x(-6.0, 3) == 3
    assert tiles.lat_to_tile_y(51.5, 3) == 2
    matrix = tiles.tile_matrix(DEFAULT_BBOX, ZOOM_MIN, ZOOM_MAX)
    z3 = [(x, y) for (z, x, y) in matrix if z == 3]
    assert min(x for x, _ in z3) == 3
    assert min(y for _, y in z3) == 2


def test_corsica_inside_matrix():
    # The bbox must cover Corsica (~41.33-43.03 N, ~8.53-9.63 E); pin a point
    # near its southeast so a future bbox shrink fails loudly, not silently.
    matrix = tiles.tile_matrix(DEFAULT_BBOX, ZOOM_MIN, ZOOM_MAX)
    for z in range(ZOOM_MIN, ZOOM_MAX + 1):
        tile = (z, tiles.lon_to_tile_x(9.5, z), tiles.lat_to_tile_y(41.4, z))
        assert tile in matrix


@pytest.mark.parametrize("z", [2, 8, 12, -1])
def test_out_of_range_zoom_rejected(z):
    assert tiles.is_valid_tile(z, 0, 0, ZOOM_MIN, ZOOM_MAX) is False


def test_valid_tile_in_range():
    assert tiles.is_valid_tile(5, 16, 11, ZOOM_MIN, ZOOM_MAX) is True


def test_negative_xy_rejected():
    assert tiles.is_valid_tile(5, -1, 0, ZOOM_MIN, ZOOM_MAX) is False


def test_xy_beyond_grid_rejected():
    # at z=3 there are only 8 tiles per axis (0..7)
    assert tiles.is_valid_tile(3, 8, 0, ZOOM_MIN, ZOOM_MAX) is False
