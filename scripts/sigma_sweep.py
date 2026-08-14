#!/usr/bin/env python
"""Dev study: pick the smoothing sigma by comparing against the RainViewer tile."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import django
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from compare_tiles import _opaque_fraction
from compare_tiles import fetch_meteofrance
from compare_tiles import fetch_rainviewer_frame
from compare_zooms import paint
from compare_zooms import pyramid
from compare_zooms import strip

from radar.providers import meteofrance_render as mr


def render_sigma(grid, levels, z, x, y, sigma) -> bytes | None:
    lvl = mr.mip_level(z, y, grid.geo, len(levels))
    rate, geo = levels[lvl]
    row, col, valid = mr.tile_index(z, x, y, geo)
    sampled = rate[row, col].astype(np.float64)
    sampled = np.where(valid, sampled, np.nan)
    return paint(mr._gaussian_blur(sampled, sigma), valid)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tile", default="7/67/45")
    ap.add_argument("--sigmas", default="0,0.8,1.2,1.8,2.5")
    ap.add_argument("--out", default=".compare")
    args = ap.parse_args()
    z, x, y = (int(v) for v in args.tile.split("/"))
    sigmas = [float(s) for s in args.sigmas.split(",")]

    mf_ts, grid = await fetch_meteofrance()
    rv, rv_ts = await fetch_rainviewer_frame(mf_ts)
    levels = pyramid(grid, "mean")

    async with rv.tile_client() as client:
        rv_png = await rv.get_tile(rv_ts, z, x, y, client=client)
    rv_cov = _opaque_fraction(rv_png) if rv_png else 0.0

    cells = [(f"RainViewer {rv_cov:.2%}", rv_png)]
    print(f"RainViewer coverage: {rv_cov:.3%}")
    for sigma in sigmas:
        png = render_sigma(grid, levels, z, x, y, sigma)
        cov = _opaque_fraction(png) if png else 0.0
        cells.append((f"sigma={sigma} {cov:.2%}", png))
        print(f"  sigma={sigma:<4} coverage={cov:.3%}  ratio to RV={cov / max(rv_cov, 1e-9):.2f}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"sigma_z{z}_x{x}_y{y}.png"
    strip(cells).save(path)
    print(path)


if __name__ == "__main__":
    asyncio.run(main())
