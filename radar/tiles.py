"""Slippy-map tile maths for the France radar coverage.

The tile matrix is **computed** from the bbox so the bbox stays the single
source of truth — never hardcode the literal (z, x, y) list.
"""

from __future__ import annotations

import math
from functools import lru_cache

# bbox is [S, N, W, E] (south, north, west, east) in degrees.
Bbox = tuple[float, float, float, float]


def lon_to_tile_x(lon: float, z: float) -> int:
    """Slippy-map X index: floor((lon + 180) / 360 * 2^z)."""
    n = 2**z
    x = math.floor((lon + 180.0) / 360.0 * n)
    return max(0, min(int(n) - 1, x))


def lat_to_tile_y(lat: float, z: float) -> int:
    """Slippy-map Y index: floor((1 - asinh(tan(lat)) / pi) / 2 * 2^z)."""
    n = 2**z
    lat_rad = math.radians(lat)
    y = math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(int(n) - 1, y))


def tile_matrix(bbox: Bbox, zoom_min: int, zoom_max: int) -> set[tuple[int, int, int]]:
    """Compute the set of (z, x, y) tiles covering ``bbox`` for the zoom range.

    Y grows southward, so the northern latitude maps to the smaller Y.
    """
    south, north, west, east = bbox
    tiles: set[tuple[int, int, int]] = set()
    for z in range(zoom_min, zoom_max + 1):
        x_min = lon_to_tile_x(west, z)
        x_max = lon_to_tile_x(east, z)
        y_min = lat_to_tile_y(north, z)
        y_max = lat_to_tile_y(south, z)
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                tiles.add((z, x, y))
    return tiles


def matrix_size(bbox: Bbox, zoom_min: int, zoom_max: int) -> int:
    """Total number of tiles in the computed matrix (62 for the default bbox)."""
    return len(tile_matrix(bbox, zoom_min, zoom_max))


@lru_cache(maxsize=4)
def matrix_frozenset(
    bbox: Bbox,
    zoom_min: int,
    zoom_max: int,
) -> frozenset[tuple[int, int, int]]:
    """Cached (z, x, y) membership set for the computed matrix.

    ``bbox`` must be a tuple (hashable) so the result is memoised — the matrix is
    fixed for the configured bbox/zoom, so this is computed once per config.
    """
    return frozenset(tile_matrix(bbox, zoom_min, zoom_max))


def is_valid_tile(z: int, x: int, y: int, zoom_min: int, zoom_max: int) -> bool:
    """Validate a requested tile: zoom in range and x/y inside the z grid."""
    if z < zoom_min or z > zoom_max:
        return False
    if x < 0 or y < 0:
        return False
    n = 2**z
    return x < n and y < n
