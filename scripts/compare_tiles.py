#!/usr/bin/env python
"""Side-by-side study tool: RainViewer tile vs the same tile rendered by us.

Dev-only, not part of the app. Pulls the current Météo-France LAME_D_EAU
composite, renders the whole 62-tile matrix through
``radar.providers.meteofrance_render``, ranks the tiles by how much rain they
actually contain, then fetches the *same* (z, x, y) from RainViewer and writes
one comparison PNG per tile.

Tile picking: you can't know where it's raining ahead of time, so the script
derives it — it renders the matrix first and sorts by opaque-pixel coverage, so
the top tiles are by construction the wettest ones in the current frame. Zoom 7
tiles are preferred (most detail per pixel); pass --zoom to override.

Run it in the django container (h5py/pyproj/Pillow live there):

    docker compose -f docker-compose.local.yml run --rm django \\
        python scripts/compare_tiles.py --top 3 --out /app/.compare

Neither Redis nor Postgres is touched: the upstream JSON is fetched directly and
fed to the providers' own parsers, so nothing is cached or archived.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from pathlib import Path

import django
import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from django.conf import settings

from radar import tiles
from radar.providers import meteofrance_render
from radar.providers.meteofrance import MeteoFranceProvider
from radar.providers.rainviewer import RainViewerProvider

CHECKER = (0x22, 0x22, 0x22, 0xFF), (0x2E, 0x2E, 0x2E, 0xFF)
LABEL_H = 22
GUTTER = 8


# -- upstream fetches (no cache, no DB) ---------------------------------------


async def fetch_meteofrance() -> tuple[int, meteofrance_render.Grid]:
    """Return (validity_ts, parsed grid) for the current LAME_D_EAU composite."""
    provider = MeteoFranceProvider()
    body = await provider._fetch_description()
    if body is None:
        msg = "Météo-France catalog fetch failed (check METEOFRANCE_APPLICATION_ID)"
        raise SystemExit(msg)
    frames = provider._parse_frames(body)
    if not frames:
        raise SystemExit("Météo-France catalog carried no usable product link")
    frame = frames[0]
    async with provider.tile_client() as client:
        h5_bytes = await provider._download_product(client, frame.ref)
    print(f"Météo-France: {len(h5_bytes) / 1e6:.2f} MB HDF5, validity ts={frame.timestamp}")
    return frame.timestamp, meteofrance_render.parse_grid(h5_bytes)


async def fetch_rainviewer_frame(target_ts: int) -> tuple[RainViewerProvider, int]:
    """Return a provider primed with the frame list, and the ts nearest ``target_ts``."""
    provider = RainViewerProvider()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(settings.RAINVIEWER_API_URL)
        resp.raise_for_status()
    frames = provider._parse_frames(resp.content)
    if not frames:
        raise SystemExit("RainViewer returned no past frames")
    nearest = min(frames, key=lambda f: abs(f.timestamp - target_ts))
    drift = nearest.timestamp - target_ts
    print(f"RainViewer: {len(frames)} frames, nearest ts={nearest.timestamp} ({drift:+d}s)")
    return provider, nearest.timestamp


# -- tile picking --------------------------------------------------------------


def rank_tiles(grid: meteofrance_render.Grid, zoom: int | None) -> list[tuple[tuple, float, bytes]]:
    """Render the matrix and sort tiles by rain coverage, wettest first."""
    matrix = tiles.tile_matrix(
        tuple(settings.RADAR_BBOX), settings.RADAR_ZOOM_MIN, settings.RADAR_ZOOM_MAX
    )
    if zoom is not None:
        matrix = {t for t in matrix if t[0] == zoom}
    ranked = []
    for z, x, y in sorted(matrix):
        png = meteofrance_render.render_tile(grid, z, x, y)
        if png is None:
            continue  # fully transparent — no rain at all
        coverage = _opaque_fraction(png)
        ranked.append(((z, x, y), coverage, png))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def _opaque_fraction(png: bytes) -> float:
    from PIL import Image

    alpha = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[:, :, 3]
    return float((alpha > 0).mean())


# -- composition ---------------------------------------------------------------


def _checkerboard(size: int, square: int = 16):
    from PIL import Image

    board = Image.new("RGBA", (size, size))
    px = board.load()
    for j in range(size):
        for i in range(size):
            px[i, j] = CHECKER[((i // square) + (j // square)) % 2]
    return board


def compose(left_png: bytes | None, right_png: bytes | None, labels: tuple[str, str]):
    """Two tiles over a checkerboard, side by side, with captions."""
    from PIL import Image
    from PIL import ImageDraw

    size = meteofrance_render.TILE_PX
    canvas = Image.new("RGBA", (size * 2 + GUTTER, size + LABEL_H), (0x18, 0x18, 0x18, 0xFF))
    draw = ImageDraw.Draw(canvas)
    for idx, (png, label) in enumerate(zip((left_png, right_png), labels, strict=True)):
        ox = idx * (size + GUTTER)
        cell = _checkerboard(size)
        if png is not None:
            cell.alpha_composite(Image.open(io.BytesIO(png)).convert("RGBA"))
        else:
            draw.text((ox + 6, LABEL_H + size // 2), "(empty tile)", fill=(0xAA, 0xAA, 0xAA, 0xFF))
        canvas.paste(cell, (ox, LABEL_H))
        draw.text((ox + 4, 5), label, fill=(0xEE, 0xEE, 0xEE, 0xFF))
    return canvas


def describe(grid: meteofrance_render.Grid, z: int, x: int, y: int) -> str:
    """Rain-rate stats for one tile, so the colours can be checked against numbers."""
    row, col, valid = meteofrance_render.tile_index(z, x, y, grid.geo)
    raw = grid.data[row, col]
    detected = valid & (raw != grid.nodata) & (raw != grid.undetect)
    mm_h = (raw.astype(np.float64) * grid.gain + grid.offset) * 12.0
    wet = mm_h[detected & (mm_h > 0)]
    if wet.size == 0:
        return "no wet cells"
    return (
        f"wet cells={wet.size:5d}  "
        f"mm/h min={wet.min():.3f} p50={np.median(wet):.3f} "
        f"p99={np.percentile(wet, 99):.2f} max={wet.max():.2f}"
    )


# -- main ----------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=3, help="how many wettest tiles to compare")
    parser.add_argument("--zoom", type=int, default=7, help="restrict to one zoom (-1 = all)")
    parser.add_argument("--out", default=".compare", help="output directory")
    parser.add_argument("--tile", help="compare exactly this tile, as z/x/y (skips ranking)")
    args = parser.parse_args()

    mf_ts, grid = await fetch_meteofrance()
    rv, rv_ts = await fetch_rainviewer_frame(mf_ts)

    if args.tile:
        z, x, y = (int(v) for v in args.tile.split("/"))
        png = meteofrance_render.render_tile(grid, z, x, y)
        ranked = [((z, x, y), _opaque_fraction(png) if png else 0.0, png)]
    else:
        zoom = None if args.zoom < 0 else args.zoom
        ranked = rank_tiles(grid, zoom)
        if not ranked:
            raise SystemExit("No rain anywhere in the current frame — try again later.")
        print(f"\n{len(ranked)} tiles with rain; wettest:")
        for (z, x, y), cov, _ in ranked[: args.top]:
            print(f"  {z}/{x}/{y}  coverage={cov:6.2%}")
        ranked = ranked[: args.top]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print()
    async with rv.tile_client() as client:
        for (z, x, y), cov, mf_png in ranked:
            rv_png = await rv.get_tile(rv_ts, z, x, y, client=client)
            image = compose(
                rv_png,
                mf_png,
                (f"RainViewer  {z}/{x}/{y}  ts={rv_ts}", f"Météo-France  {z}/{x}/{y}  ts={mf_ts}"),
            )
            path = out / f"cmp_z{z}_x{x}_y{y}.png"
            image.save(path)
            print(f"{path}  coverage={cov:.2%}  rainviewer={'hit' if rv_png else 'EMPTY'}")
            print(f"    MF {describe(grid, z, x, y)}")


if __name__ == "__main__":
    asyncio.run(main())
