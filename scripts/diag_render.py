#!/usr/bin/env python
"""Dev diagnostic: DPRadar catalog inventory + grid-vs-tile resolution ratios."""

from __future__ import annotations

import asyncio
import math
import os
import sys
from pathlib import Path

import django
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from django.conf import settings

from radar.providers.meteofrance_auth import MeteoFranceAuth


async def catalog() -> None:
    auth = MeteoFranceAuth()
    token = await auth.get_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for path in (
            "/mosaiques/METROPOLE/observations",
            "/mosaiques/METROPOLE",
            "",
        ):
            url = settings.METEOFRANCE_API_BASE_URL + path
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                print(f"{path or '/'} -> {exc}")
                continue
            print(f"\n=== GET {path or '/'} -> {resp.status_code}")
            print(resp.text[:2000])


def geometry() -> None:
    from radar import tiles

    matrix = tiles.tile_matrix(
        tuple(settings.RADAR_BBOX), settings.RADAR_ZOOM_MIN, settings.RADAR_ZOOM_MAX
    )
    print("\n=== tile pixel ground resolution vs 500 m grid (lat 46.5, tile centre)")
    lat = 46.5
    for z in sorted({t[0] for t in matrix}):
        n_pix = 256 * 2**z
        m_per_px = 40075016.686 / n_pix * math.cos(math.radians(lat))
        ratio = m_per_px / 500.0
        n_tiles = len([t for t in matrix if t[0] == z])
        print(
            f"  z={z}  tiles={n_tiles:3d}  {m_per_px:8.1f} m/px  "
            f"ratio={ratio:6.2f} cells/px  cells covered per px={ratio**2:9.1f}  "
            f"mip level={max(0, int(math.log2(ratio)))}"
        )


async def main() -> None:
    geometry()
    await catalog()


if __name__ == "__main__":
    asyncio.run(main())
