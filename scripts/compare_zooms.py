#!/usr/bin/env python
"""Dev study: RainViewer vs old nearest-neighbour vs mip-mean vs mip-max, per zoom.

Renders the tile containing a chosen lat/lon at every zoom in the matrix, four ways,
so the downsampling strategy can be picked by looking at it rather than by argument.

    docker compose -f docker-compose.local.yml run --rm django \\
        python scripts/compare_zooms.py --out /app/.compare
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from pathlib import Path

import django
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from compare_tiles import GUTTER
from compare_tiles import LABEL_H
from compare_tiles import _checkerboard
from compare_tiles import fetch_meteofrance
from compare_tiles import fetch_rainviewer_frame
from django.conf import settings

from radar import tiles
from radar.providers import meteofrance_render as mr


def reduce2(a: np.ndarray, how: str) -> np.ndarray:
    h, w = a.shape
    h2, w2 = h // 2, w // 2
    block = a[: h2 * 2, : w2 * 2].reshape(h2, 2, w2, 2)
    ok = ~np.isnan(block)
    if how == "max":
        filled = np.where(ok, block, -np.inf).max(axis=(1, 3))
        return np.where(np.isfinite(filled), filled, np.nan).astype(np.float32)
    total = np.where(ok, block, 0.0).sum(axis=(1, 3))
    count = ok.sum(axis=(1, 3))
    return np.where(count > 0, total / np.maximum(count, 1), np.nan).astype(np.float32)


def pyramid(grid, how: str):
    levels = [(mr.rate_field(grid), grid.geo)]
    while min(levels[-1][0].shape) > mr.TILE_PX:
        prev, geo = levels[-1]
        red = reduce2(prev, how)
        levels.append(
            (
                red,
                mr.GridGeo(
                    projdef=geo.projdef,
                    xscale=geo.xscale * 2,
                    yscale=geo.yscale * 2,
                    xsize=red.shape[1],
                    ysize=red.shape[0],
                    x_ul=geo.x_ul,
                    y_ul=geo.y_ul,
                ),
            )
        )
    return levels


def paint(mm_h: np.ndarray, valid: np.ndarray) -> bytes | None:
    visible = valid & ~np.isnan(mm_h) & (mm_h >= mr.RAIN_LUT.floor)
    if not visible.any():
        return None
    rgba = mr._colorize(np.nan_to_num(mm_h), visible, mr.RAIN_LUT)
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def render_nn(grid, z, x, y) -> bytes | None:
    """The pre-fix renderer: point-sample the native grid at each pixel centre."""
    row, col, valid = mr.tile_index(z, x, y, grid.geo)
    raw = grid.data[row, col]
    detected = valid & (raw != grid.nodata) & (raw != grid.undetect)
    mm_h = (raw.astype(np.float64) * grid.gain + grid.offset) * 12.0
    return paint(np.where(detected, mm_h, np.nan), detected)


def render_mip(grid, levels, z, x, y) -> bytes | None:
    lvl = mr.mip_level(z, y, grid.geo, len(levels))
    rate, geo = levels[lvl]
    row, col, valid = mr.tile_index(z, x, y, geo)
    return paint(rate[row, col].astype(np.float64), valid)


def strip(cells: list[tuple[str, bytes | None]]):
    from PIL import Image
    from PIL import ImageDraw

    size = mr.TILE_PX
    canvas = Image.new(
        "RGBA",
        (size * len(cells) + GUTTER * (len(cells) - 1), size + LABEL_H),
        (0x18,) * 3 + (255,),
    )
    draw = ImageDraw.Draw(canvas)
    for idx, (label, png) in enumerate(cells):
        ox = idx * (size + GUTTER)
        cell = _checkerboard(size)
        if png is not None:
            cell.alpha_composite(Image.open(io.BytesIO(png)).convert("RGBA"))
        canvas.paste(cell, (ox, LABEL_H))
        draw.text((ox + 4, 5), label, fill=(0xEE, 0xEE, 0xEE, 0xFF))
    return canvas


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--lon", type=float, default=None)
    ap.add_argument("--out", default=".compare")
    args = ap.parse_args()

    mf_ts, grid = await fetch_meteofrance()
    rv, rv_ts = await fetch_rainviewer_frame(mf_ts)
    mean_levels, max_levels = pyramid(grid, "mean"), pyramid(grid, "max")
    print(f"pyramid levels: {[lv[0].shape for lv in mean_levels]}")

    lat, lon = args.lat, args.lon
    if lat is None:
        # Centre on the densest rain *area* (argmax of a coarse mean level), not the
        # single hottest cell — an isolated hail spike makes a useless z7 comparison.
        coarse, geo = mean_levels[3]
        rate = np.nan_to_num(coarse)
        r, c = np.unravel_index(int(rate.argmax()), rate.shape)
        r, c = r * 8, c * 8  # back to native-grid indices (level 3 = /8)
        geo = grid.geo
        inv = mr.transformer_for(geo.projdef)
        lon, lat = inv.transform(
            geo.x_ul + (c + 0.5) * geo.xscale,
            geo.y_ul - (r + 0.5) * geo.yscale,
            direction="INVERSE",
        )
        print(f"densest rain area at lat={lat:.3f} lon={lon:.3f}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    async with rv.tile_client() as client:
        for z in range(settings.RADAR_ZOOM_MIN, settings.RADAR_ZOOM_MAX + 1):
            x, y = tiles.lon_to_tile_x(lon, z), tiles.lat_to_tile_y(lat, z)
            lvl = mr.mip_level(z, y, grid.geo, len(mean_levels))
            image = strip(
                [
                    (f"RainViewer z{z}", await rv.get_tile(rv_ts, z, x, y, client=client)),
                    (f"old nearest z{z}", render_nn(grid, z, x, y)),
                    (f"mip-MEAN L{lvl}", render_mip(grid, mean_levels, z, x, y)),
                    (f"mip-MAX L{lvl}", render_mip(grid, max_levels, z, x, y)),
                ]
            )
            path = out / f"zoom_{z}_{x}_{y}.png"
            image.save(path)
            print(f"{path}  tile={z}/{x}/{y}  mip level={lvl}")


if __name__ == "__main__":
    asyncio.run(main())
