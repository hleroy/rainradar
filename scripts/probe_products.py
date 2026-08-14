#!/usr/bin/env python
"""Dev diagnostic: dump the ODIM structure of LAME_D_EAU vs REFLECTIVITE."""

from __future__ import annotations

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

from radar.providers.meteofrance_auth import MeteoFranceAuth

CACHE = Path("/app/.compare/raw")


async def grab(client: httpx.AsyncClient, headers: dict, observation: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{observation}.h5"
    base = f"{settings.METEOFRANCE_API_BASE_URL}/mosaiques/METROPOLE/observations/{observation}"
    resp = await client.get(base, headers=headers)
    resp.raise_for_status()
    links = resp.json().get("links", [])
    products = [link for link in links if "/produit" in link.get("href", "")]
    if not products:
        print(f"\n### {observation}: no product links; raw catalog:\n{resp.text[:1500]}")
        return b""
    for link in products:
        print(f"    available: {link.get('href')}  validity={link.get('validity_time')}")
    chosen = next((p for p in products if "maille=500" in p["href"]), products[0])
    href, validity = chosen["href"], chosen.get("validity_time")
    print(f"\n### {observation}  validity={validity}\n    {href}")
    product = await client.get(href, headers=headers)
    product.raise_for_status()
    cached.write_bytes(product.content)
    return product.content


def dump(observation: str, blob: bytes) -> None:
    import gzip

    import h5py

    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
        print("    (gzip-compressed payload, decompressed)")
    with h5py.File(io.BytesIO(blob), "r") as f:
        where = dict(f["where"].attrs)
        print(f"    /where: { ({k: where[k] for k in sorted(where)}) }")
        d1 = f["dataset1"]["data1"]
        what = dict(d1["what"].attrs)
        print(f"    /dataset1/data1/what: {what}")
        data = np.asarray(d1["data"][()])
        gain = float(what["gain"])
        offset = float(what["offset"])
        nodata = int(what["nodata"])
        undetect = int(what["undetect"])
        real = data[(data != nodata) & (data != undetect)]
        print(f"    dtype={data.dtype} shape={data.shape}")
        print(
            f"    raw: nodata={nodata} undetect={undetect} "
            f"valid cells={real.size} ({real.size / data.size:.2%})"
        )
        if real.size:
            phys = real.astype(np.float64) * gain + offset
            print(
                f"    physical: min={phys.min():.4f} p50={np.median(phys):.4f} "
                f"p99={np.percentile(phys, 99):.3f} max={phys.max():.3f}  "
                f"(gain={gain} => quantum={gain})"
            )
            print(f"    distinct raw values={np.unique(real).size}")


async def main() -> None:
    auth = MeteoFranceAuth()
    headers = {"Authorization": f"Bearer {await auth.get_token()}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        for observation in ("LAME_D_EAU", "REFLECTIVITE"):
            blob = await grab(client, headers, observation)
            if blob:
                dump(observation, blob)


if __name__ == "__main__":
    asyncio.run(main())
