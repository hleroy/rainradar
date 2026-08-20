#!/usr/bin/env python3
# Host-run load generator: prints freely, opens https URLs, long argparse main().
"""Per-tier load generator for the tile endpoint.

/tiles/... is served by paths whose costs differ by orders of magnitude, so a
single blended number would say nothing about the server. This script first
probes real tile URLs and sorts them into pools by what actually answered
(static_200 = Nginx file, empty_204 = Django fallback), then loads each pool
separately. Two modes: `ramp` (closed loop, steps concurrency up to find max
throughput) and `rate` (open loop, Poisson arrivals, honest latency at a given
offered load). Load is spread over P processes x 1 asyncio loop x C keep-alive
connections; the report prints the CPU cores the generator itself burned --
if that nears your core count, the server numbers are a floor, not a result.

Frames are sampled from a past day and --skip-recent drops the newest ones, so
the upstream-fetch path is never reached (it would pull Meteo-France products).
Non-localhost targets require --allow-prod: the archiver shares the production
host, and a Meteo-France frame missed while starved is lost permanently.

Needs aiohttp on the host (not an app dependency):

    uv pip install --system aiohttp uvloop

EXAMPLES
    # Classify tiles from yesterday and write rrbench_pool.json, no load
    ./scripts/tile_bench.py --mode discover

    # Find the knee against the local dev stack (default steps 8..256)
    ./scripts/tile_bench.py --mode ramp

    # Production, Nginx pool only, custom concurrency steps
    ./scripts/tile_bench.py --base https://rainradar.hleroy.com --allow-prod \\
        --pool static_200 --steps 8,24,120,504

    # Honest percentiles at 400 req/s against the Django fallback
    ./scripts/tile_bench.py --mode rate --rate 400 --pool empty_204 --duration 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import multiprocessing as mp
import os
import random
import resource
import ssl
import sys
import time
from array import array
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:  # host-only tool, so aiohttp is deliberately not in pyproject
    sys.exit("tile_bench needs aiohttp on the host: uv pip install --system aiohttp uvloop")

try:
    import uvloop
except ImportError:  # optional; the stock loop works, just slower
    uvloop = None


# -- tile matrix ---------------------------------------------------------------
# Mirrors radar/tiles.py. Verified against the running app: the default France
# bbox over zooms 3-7 yields exactly 62 tiles, which is the count the repo pins.


def _lon2x(lon: float, z: int) -> int:
    return int((lon + 180.0) / 360.0 * (1 << z))


def _lat2y(lat: float, z: int) -> int:
    r = math.radians(lat)
    return int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * (1 << z))


def tile_matrix(bbox: list[float], zmin: int, zmax: int) -> list[tuple[int, int, int]]:
    """bbox is [S, N, W, E] as /api/radar/frames reports it."""
    south, north, west, east = bbox
    return [
        (z, x, y)
        for z in range(zmin, zmax + 1)
        for x in range(_lon2x(west, z), _lon2x(east, z) + 1)
        for y in range(_lat2y(north, z), _lat2y(south, z) + 1)
    ]


# -- phase 1: discover + classify ----------------------------------------------


def _ssl_ctx() -> ssl.SSLContext:
    # A shared context lets aiohttp resume TLS sessions across connections. Without
    # it every new connection pays a full handshake and you benchmark TLS, not the app.
    return ssl.create_default_context()


async def discover(
    base: str,
    provider: str,
    day_offset: int,
    frames_wanted: int,
    skip_recent: int,
    zooms: tuple[int, int],
    probe_conc: int,
) -> dict:
    """Fetch a past day's frames, build the real tile matrix, probe + classify."""
    connector = aiohttp.TCPConnector(limit=probe_conc, ssl=_ssl_ctx())
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
        day_start = (int(time.time()) // 86400 - day_offset) * 86400
        url = f"{base}/api/radar/frames?provider={provider}&from={day_start}&to={day_start + 86399}"
        async with s.get(url) as r:
            if r.status != 200:
                sys.exit(f"frames query failed: HTTP {r.status} for {url}")
            data = await r.json()

        frames = [f["timestamp"] for f in data.get("frames", [])]
        if not frames:
            sys.exit(
                f"no archived {provider} frames for the day starting {day_start}; try --day-offset"
            )
        # Drop the newest frames so we can never reach tier 3 (upstream).
        frames = frames[:-skip_recent] if skip_recent and len(frames) > skip_recent else frames
        step = max(1, len(frames) // frames_wanted)
        frames = frames[::step][:frames_wanted]

        bbox = data.get("bbox") or [41.2, 51.5, -6.0, 9.7]
        matrix = tile_matrix(bbox, *zooms)
        print(f"  frames sampled : {len(frames)}  (day starting {day_start})", file=sys.stderr)
        print(f"  tile matrix    : {len(matrix)} tiles from bbox {bbox}", file=sys.stderr)

        candidates = [
            f"{base}/tiles/{provider}/{time.strftime('%Y-%m-%d', time.gmtime(ts))}"
            f"/{ts}/{z}/{x}/{y}.png"
            for ts in frames
            for (z, x, y) in matrix
        ]
        random.shuffle(candidates)

        pools: dict[str, list[str]] = {"static_200": [], "empty_204": []}
        other = Counter()
        sem = asyncio.Semaphore(probe_conc)

        async def probe(u: str) -> None:
            async with sem:
                try:
                    async with s.get(u) as resp:
                        await resp.read()
                        if resp.status == 200:
                            pools["static_200"].append(u)
                        elif resp.status == 204:
                            pools["empty_204"].append(u)
                        else:
                            other[resp.status] += 1
                except Exception as exc:  # a probe failure is data
                    other[type(exc).__name__] += 1

        t0 = time.perf_counter()
        await asyncio.gather(*(probe(u) for u in candidates))
        dt = time.perf_counter() - t0

    print(f"  probed {len(candidates)} URLs in {dt:.1f}s", file=sys.stderr)
    for name, pool in pools.items():
        print(f"    {name:<12} {len(pool)}", file=sys.stderr)
    if other:
        print(f"    other        {dict(other)}", file=sys.stderr)
    return pools


# -- worker processes ----------------------------------------------------------
# One process, one event loop, C keep-alive connections. Results come back as
# (latencies array, status counter, bytes) over a Queue.


def _install_loop() -> None:
    if uvloop is not None:
        uvloop.install()


async def _session(conns: int) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        limit=conns,
        limit_per_host=conns,
        ttl_dns_cache=600,
        ssl=_ssl_ctx(),
        force_close=False,  # keep-alive: a connection per request exhausts ephemeral ports
        enable_cleanup_closed=True,
    )
    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=30, connect=10),
        headers={"User-Agent": "rrbench/1.0 (+load test)"},
        auto_decompress=False,  # do not spend client CPU on gzip we did not ask for
    )


def _closed_worker(urls, conns, duration, warmup, seed, q) -> None:
    _install_loop()

    async def main() -> None:
        lat = array("d")
        codes: Counter = Counter()
        nbytes = 0
        sess = await _session(conns)
        async with sess:
            # Prime the pool so TLS handshakes are not charged to the measurement.
            # Each response MUST be read and released, or the connection stays
            # checked out and the pool starves at exactly the concurrency we set.
            async def _prime(u: str) -> None:
                async with sess.get(u) as r:
                    await r.read()

            await asyncio.gather(
                *(_prime(urls[i % len(urls)]) for i in range(conns)),
                return_exceptions=True,
            )
            t_start = time.perf_counter()
            record_from = t_start + warmup
            deadline = t_start + warmup + duration

            async def conn(rng: random.Random) -> None:
                nonlocal nbytes
                while True:
                    t0 = time.perf_counter()
                    if t0 >= deadline:
                        return
                    u = urls[rng.randrange(len(urls))]
                    try:
                        async with sess.get(u) as r:
                            body = await r.read()
                            if t0 >= record_from:
                                lat.append(time.perf_counter() - t0)
                                codes[str(r.status)] += 1
                                nbytes += len(body)
                    except Exception as exc:  # errors are the signal
                        if t0 >= record_from:
                            lat.append(time.perf_counter() - t0)
                            codes[type(exc).__name__] += 1

            await asyncio.gather(
                *(conn(random.Random(seed + i)) for i in range(conns)),  # noqa: S311
            )
        q.put((lat.tobytes(), dict(codes), nbytes, duration))

    asyncio.run(main())


def _open_worker(urls, rate, duration, warmup, seed, q) -> None:
    """Poisson arrivals at `rate` req/s. Latency is measured from the INTENDED
    arrival time, so client-side queueing shows up instead of being omitted."""
    _install_loop()

    async def main() -> None:
        lat = array("d")
        codes: Counter = Counter()
        nbytes = 0
        behind = 0  # arrivals we could not issue on time -> generator-bound
        rng = random.Random(seed)  # noqa: S311 — sampling, not crypto
        # Enough connections that the client is not itself the queue.
        conns = max(64, int(rate * 0.5))
        sess = await _session(conns)
        inflight: set[asyncio.Task] = set()

        async with sess:
            t_start = time.perf_counter()
            record_from = t_start + warmup
            end = t_start + warmup + duration

            async def one(u: str, intended: float) -> None:
                nonlocal nbytes
                try:
                    async with sess.get(u) as r:
                        body = await r.read()
                        if intended >= record_from:
                            lat.append(time.perf_counter() - intended)
                            codes[str(r.status)] += 1
                            nbytes += len(body)
                except Exception as exc:
                    if intended >= record_from:
                        lat.append(time.perf_counter() - intended)
                        codes[type(exc).__name__] += 1

            next_at = t_start
            while True:
                next_at += rng.expovariate(rate)
                if next_at >= end:
                    break
                slack = next_at - time.perf_counter()
                if slack > 0:
                    await asyncio.sleep(slack)
                else:
                    behind += 1
                t = asyncio.create_task(one(urls[rng.randrange(len(urls))], next_at))
                inflight.add(t)
                t.add_done_callback(inflight.discard)
            if inflight:
                await asyncio.wait(inflight, timeout=30)
        q.put((lat.tobytes(), dict(codes), nbytes, duration, behind))

    asyncio.run(main())


# -- orchestration + reporting -------------------------------------------------


def _pcts(sorted_lat: list[float], qs=(50, 90, 99, 99.9)) -> dict[float, float]:
    if not sorted_lat:
        return {q: float("nan") for q in qs}
    n = len(sorted_lat)
    return {q: sorted_lat[min(n - 1, int(q / 100.0 * n))] * 1000.0 for q in qs}


def _spawn(target, args_list) -> tuple[list[float], Counter, int, list]:
    q: mp.Queue = mp.Queue()
    procs = [mp.Process(target=target, args=(*a, q)) for a in args_list]
    for p in procs:
        p.start()
    results = [q.get() for _ in procs]
    for p in procs:
        p.join()
    lat: list[float] = []
    codes: Counter = Counter()
    nbytes = 0
    for r in results:
        lat.extend(array("d", r[0]))
        codes.update(r[1])
        nbytes += r[2]
    return lat, codes, nbytes, results


def _child_cpu() -> float:
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


def _cpu_cores_used(cpu_delta: float, wall: float) -> float:
    """Cores the GENERATOR burned. RUSAGE_CHILDREN accumulates over every child
    ever reaped, so a step must report its own delta or each later step inherits
    the CPU of all the earlier ones."""
    return cpu_delta / wall if wall > 0 else 0.0


def report(label, lat, codes, nbytes, wall, cpu_delta, offered=None, behind=0) -> None:
    lat.sort()
    p = _pcts(lat)
    n = len(lat)
    rps = n / wall if wall else 0.0
    ok = sum(v for k, v in codes.items() if k.isdigit() and 200 <= int(k) < 400)
    bad = n - ok
    cores = _cpu_cores_used(cpu_delta, wall)
    print(f"\n  {label}")
    print(
        f"    requests    {n:>10,}   {rps:>10,.0f} req/s"
        + (f"   (offered {offered:,.0f})" if offered else "")
    )
    print(f"    ok / other  {ok:>10,} / {bad:,}   {dict(sorted(codes.items()))}")
    print(f"    throughput  {nbytes / wall / 1e6:>10,.1f} MB/s")
    print(
        f"    latency ms  p50 {p[50]:.1f}   p90 {p[90]:.1f}   p99 {p[99]:.1f}   p99.9 {p[99.9]:.1f}"
    )
    print(
        f"    client      {cores:>10,.1f} cores"
        + (f"   BEHIND {behind:,} arrivals" if behind else "")
    )
    if cores > (os.cpu_count() or 1) * 0.8:
        print("    !! generator near CPU saturation - treat server numbers as a FLOOR")
    if behind:
        print("    !! generator could not keep up with the offered rate - lower --rate")


def run_ramp(urls: list[str], args) -> None:
    """Closed loop, stepping concurrency up until throughput stops rising.

    Read the steps together, not individually: the knee is where req/s plateaus
    while latency keeps climbing (Little's Law -- latency = concurrency / throughput
    once saturated). Latency here is optimistic by construction; use run_rate for
    percentiles you intend to quote.
    """
    for total_conns in (int(v) for v in args.steps.split(",")):
        per = max(1, total_conns // args.procs)
        nproc = min(args.procs, total_conns)
        t0, c0 = time.perf_counter(), _child_cpu()
        lat, codes, nbytes, _ = _spawn(
            _closed_worker,
            [(urls, per, args.duration, args.warmup, 1000 * i) for i in range(nproc)],
        )
        report(
            f"concurrency {nproc * per:<5} ({nproc}p x {per}c)",
            lat,
            codes,
            nbytes,
            time.perf_counter() - t0 - args.warmup,
            _child_cpu() - c0,
        )


def run_rate(urls: list[str], args) -> None:
    """Open loop at a fixed offered rate -- the honest-latency mode."""
    per_rate = args.rate / args.procs
    t0, c0 = time.perf_counter(), _child_cpu()
    lat, codes, nbytes, raw = _spawn(
        _open_worker,
        [(urls, per_rate, args.duration, args.warmup, 1000 * i) for i in range(args.procs)],
    )
    report(
        f"open loop @ {args.rate:,.0f} req/s offered",
        lat,
        codes,
        nbytes,
        time.perf_counter() - t0 - args.warmup,
        _child_cpu() - c0,
        offered=args.rate,
        behind=sum(r[4] for r in raw),
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--provider", default="meteofrance", choices=("meteofrance", "rainviewer"))
    ap.add_argument("--allow-prod", action="store_true", help="required for a non-localhost target")
    ap.add_argument("--mode", choices=("discover", "ramp", "rate"), default="ramp")
    ap.add_argument("--pool", choices=("static_200", "empty_204", "both"), default="both")
    ap.add_argument("--pool-file", default="rrbench_pool.json")
    ap.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument(
        "--steps",
        default="8,16,32,64,128,256",
        help="closed-loop concurrency steps (total across processes)",
    )
    ap.add_argument("--rate", type=float, default=500.0, help="open-loop arrivals/s (total)")
    ap.add_argument("--duration", type=float, default=10.0, help="measured seconds per step")
    ap.add_argument("--warmup", type=float, default=2.0, help="discarded seconds per step")
    ap.add_argument("--day-offset", type=int, default=1, help="days back to sample frames from")
    ap.add_argument("--frames", type=int, default=8, help="how many frames to sample")
    ap.add_argument(
        "--skip-recent",
        type=int,
        default=6,
        help="drop the N newest frames so tier 3 (upstream) is unreachable",
    )
    ap.add_argument("--zooms", default="3,7")
    ap.add_argument("--probe-conc", type=int, default=32)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    host = urlparse(base).hostname or ""
    if host not in ("localhost", "127.0.0.1", "::1") and not args.allow_prod:
        sys.exit(
            f"refusing to load {host} without --allow-prod\n"
            "NOTE: the archiver shares the production host, and Météo-France is\n"
            "latest-only -- a frame it misses while starved is lost permanently."
        )

    zooms = tuple(int(v) for v in args.zooms.split(","))

    pool_path = Path(args.pool_file)
    if args.mode == "discover" or not pool_path.exists():
        print(f"[discover] {base}", file=sys.stderr)
        _install_loop()
        pools = asyncio.run(
            discover(
                base,
                args.provider,
                args.day_offset,
                args.frames,
                args.skip_recent,
                zooms,
                args.probe_conc,
            )
        )
        with pool_path.open("w") as fh:
            json.dump(pools, fh)
        print(f"[discover] wrote {args.pool_file}", file=sys.stderr)
        if args.mode == "discover":
            return
    else:
        with pool_path.open() as fh:
            pools = json.load(fh)
        print(
            f"[pool] reusing {args.pool_file}: "
            + ", ".join(f"{k}={len(v)}" for k, v in pools.items()),
            file=sys.stderr,
        )

    names = list(pools) if args.pool == "both" else [args.pool]
    for name in names:
        urls = pools.get(name) or []
        if not urls:
            print(f"\n== {name}: pool empty, skipping ==")
            continue
        print(f"\n== {name}  ({len(urls)} distinct URLs, {args.procs} processes) ==")

        if args.mode == "ramp":
            run_ramp(urls, args)
        else:
            run_rate(urls, args)


if __name__ == "__main__":
    mp.set_start_method("fork")
    main()
