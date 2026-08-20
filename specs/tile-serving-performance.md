# Tile-serving performance: measured baseline and optimization plan

**Status:** proposal. Nothing here is implemented.
**Raised:** 2026-08-20, from the first real load measurement of production (v1.0.1).
**Tool:** [`scripts/tile_bench.py`](../scripts/tile_bench.py) — per-tier load generator.
**Tier numbering** follows the design doc: 1 Nginx static, 2 Redis, 3 the frame row,
4 upstream.
**Related:** design doc [§5.2.1 The tile miss ladder](rain-lightning-radar-design.md),
`TODO.md`.

This document exists because the load test produced one clear, actionable finding —
**the Django tile fallback saturates at ~700 req/s while Nginx serves ≥1,458 req/s on
the same box** — and the fix for it is a design change (a new on-disk contract between
the archiver and Nginx) rather than a tuning knob. The smaller recommendations are
recorded here too so the ranking is explicit and the rejected options are on the record.

---

## 1. The measured baseline

Run on 2026-08-20 against production over the WAN, closed loop (`--mode ramp`),
Météo-France frames from the previous day, ~76,000 requests total.

### Tier 1 — Nginx static (`static_200`)

| concurrency | req/s | note |
|---|---|---|
| 8 | 458 | |
| 24 | 1,056 | |
| 120 | **1,458** | 34.7 MB/s ≈ **278 Mbit/s**, p99.9 170 ms |
| 504 | 332 | congestion collapse, p99.9 **7.0 s** |

At c120 the p99.9 of 170 ms shows no distress, and 278 Mbit/s is a suspiciously round
number. **This tier was most likely bandwidth-bound, not server-bound** — 1,458 req/s is
a *floor* on Nginx's capability, not a measurement of it. §6 says how to settle that.

Average tile size follows from those two figures: 34.7 MB/s ÷ 1,458 req/s ≈ **23.8 KB
per tile**.

### Tiers 2–3 — Django fallback (`empty_204`)

| concurrency | req/s | latency |
|---|---|---|
| 8 | 368 | |
| 24 | **712** | p50 32 ms |
| 120 | 679 | p50 110 ms, p90 161 ms |
| 504 | 251 | 12 `ServerTimeoutError`, p99.9 **11 s** |

Throughput plateaus at **~700 req/s, reached at only 24 concurrent connections**, then
*falls* while latency triples. Little's Law confirms saturation rather than a
measurement artefact: 24 ÷ 712 = 33.7 ms and 120 ÷ 679 = 177 ms both match the observed
p50s. Past the knee, added concurrency buys queueing and nothing else.

The generator burned 0.4 cores at c504, so the collapse at that step is server-side on
both pools, not the client giving up.

### What the run also proved

Zero 5xx across all ~76,000 requests, including the whole `empty_204` pool — the exact
path that was returning HTTP 500 the previous day (fixed in #7 / v1.0.1). The
`db_unavailable` shedding branch never fired.

### Caveats to carry forward

- `aiohttp` speaks HTTP/1.1; browsers reach production over HTTP/2 or HTTP/3 via
  Traefik. 504 concurrent *connections* is a harsher shape than 504 concurrent
  *streams*, so the c504 row is a stress case, not a traffic forecast.
- The client sat across the WAN. Even at c8 the `empty_204` pool implies ~22 ms per
  request, most of which is round-trip time. The *plateau* is a server property; the
  low-concurrency rows are not.

---

## 2. Where the ~700 req/s goes

Four uvicorn workers (`WEB_CONCURRENCY:-4`, `compose/production/django/start`) at
679 req/s is **~170 req/s per worker ≈ 5.9 ms of work per request** — for a response
that, post-#7, is one `SISMEMBER` and a 204 with no body.

A measurable share of that is Django's own ASGI plumbing, not our view. Django's
`MiddlewareMixin.__acall__` wraps every sync `process_request` / `process_response` in
`sync_to_async(thread_sensitive=True)`, and the ASGI handler gives each request its own
`ThreadSensitiveContext` — a single-worker `ThreadPoolExecutor`. The eight production
middlewares define 13 such methods between them.

Measured in the project container (Django 6.0.7, `asgiref`):

```
hops=  0    0.001 ms/request  ->  817,147 req/s per worker (serial)
hops= 13    1.007 ms/request  ->      993 req/s per worker (serial)
hops= 15    1.096 ms/request  ->      912 req/s per worker (serial)
hops= 15    1.109 ms/request  ->      902 req/s (300 concurrent in one loop)
```

Two things to read off this:

1. **~1.0 ms per request is spent on executor round-trips before any of our code runs**
   — roughly **17% of the 5.9 ms budget**.
2. **It does not parallelise.** 300 concurrent requests in one event loop yield the same
   ~900 req/s as running them one at a time, because each hop is a thread handoff the
   loop must schedule. Concurrency cannot dilute this cost; only removing hops can.

The tile view adds 1–2 more hops of its own (`sync_to_async(path.is_file)`, twice for
`rainviewer` because of the legacy dual-read).

The remaining ~4.9 ms is ordinary Django request/response construction, URL resolution,
uvicorn's HTTP parsing, and the Redis round-trip. There is no single hot spot to delete.
**That is the argument for R1: the cheapest Django request is the one Nginx answers.**

---

## 3. Recommendations, ranked

| # | Change | Expected gain | Cost / risk |
|---|---|---|---|
| **R1** | Answer "nothing to draw" from Nginx via a frame-complete sentinel | Removes ~all of historical navigation from Django | New on-disk contract; needs backfill (§4) |
| **R2** | `limit_conn` on `@django_tiles` | Overload sheds as fast 429s instead of 11 s timeouts | One directive; picks a number that must not bite real clients |
| **R3** | Trim the middleware stack | ~0.5 ms/request (~9%) if the admin is dropped | Blocked on the open admin decision in `TODO.md` |
| **R4** | `open_file_cache` on the static tile path | Fewer syscalls per static hit | Negative caching interacts with R1 (§3.4) |
| **R5** | Palette-quantise Météo-France PNGs | Fewer bytes on the wire *and* on disk | Needs measurement first; visual regression risk |
| **R6** | Re-measure from the host; tune `WEB_CONCURRENCY` | Turns R-numbers from estimates into facts | None — do this first |

### 3.1 R1 — Answer the 204 from Nginx

Detailed in §4. This is the only recommendation that changes the architecture, and the
only one that addresses the finding directly: the ~700 req/s ceiling stops mattering when
the requests that hit it no longer reach Python.

Scope of the win: the `empty_204` pool *is* historical navigation. On a sparse-archive
provider (Météo-France persists only non-empty tiles) most of the viewport misses the
static cache on any quiet day, so this is the common path, not an edge case.

### 3.2 R2 — Bound the Django fallback in Nginx

`compose/production/nginx/default.conf` documents its own asymmetry: *"tiles are NOT
under /api/ and stay unthrottled"*. `/api/` gets `limit_req` per-IP **and** a global
`api_global` circuit breaker precisely so a surge sheds as 429s rather than melting
request queues. The tile fallback has neither, and the c504 row is what that looks like:
throughput down 2.7×, p99.9 at 11 s, and client-side timeouts.

The static tier does **not** need this — at c120 Nginx was comfortable, and adding a
per-IP connection cap to `/tiles/…` risks throttling a legitimate browser (six HTTP/1.1
connections per host, multiplied by however many upstream connections Traefik opens).

So bound the **named fallback location only**, which is the fragile one:

```nginx
limit_conn_zone "global" zone=tile_fallback_conn:1m;

location @django_tiles {
  limit_conn tile_fallback_conn 64;   # ≈ the measured knee (24) with headroom
  ...
}
```

A 429 on a tile becomes a Leaflet `tileerror` and a blank tile — visibly degraded, but
bounded, and recovered on the next pan. That is strictly better than an 11-second hang
that also starves every other request in the worker.

Note the ordering: with R1 shipped, `@django_tiles` sees only live and partially-archived
frames, so 64 is generous. Ship R1 first, then size this against a re-measured knee.

### 3.3 R3 — Trim the middleware stack

Of the eight production middlewares, four exist only for the Django admin —
`SessionMiddleware`, `CsrfViewMiddleware`, `AuthenticationMiddleware`,
`MessageMiddleware` — and they account for **7 of the 13 hops**, ≈ 0.54 ms per request
on every tile, every API call and every SSE handshake. The app itself has no login, no
forms, and no session state.

This is **blocked on the open admin decision** already recorded in `TODO.md` (the admin
is currently unreachable in production from either door). If that decision is "drop it",
this becomes nearly free; if it is "keep it", these middlewares stay and R3 is off the
table in this form.

Do **not** attempt this by re-implementing Django's built-ins as async-native
middleware. `SecurityMiddleware` and `ConditionalGetMiddleware` earn their hops
(`ConditionalGetMiddleware` is what turns unchanged JSON into empty 304s), and hand-rolled
replacements would be a security-relevant fork of upstream code to save a few hundred
microseconds.

### 3.4 R4 — `open_file_cache` on the static tile path

Every static tile hit currently costs an `open` + `fstat` + `sendfile`, and every miss
costs a failed `open` before `try_files` falls through. The archive is immutable by
construction, so cached file handles cannot go stale:

```nginx
open_file_cache          max=20000 inactive=60s;
open_file_cache_valid    300s;
open_file_cache_min_uses 1;
open_file_cache_errors   on;
```

`open_file_cache_errors on` is the interesting half — it caches the *negative* lookup, so
a repeatedly-missing tile stops paying for a failed `open`. Two interactions to respect:

- A tile written later by the Django fallback (miss ladder tier 4) stays masked for up to
  `open_file_cache_valid`. Harmless: Django serves it correctly from disk in step 2
  meanwhile. But it means the fallback's write does not take effect at the Nginx layer
  immediately, which must be stated wherever this lands.
- With R1, the sentinel is checked by `if (-f …)`, which is **not** served by
  `open_file_cache`. Sizing `max` for tiles alone is correct.

Gain unquantified — measure with R6 before and after. It is cheap and reversible, which
is why it ranks above R5.

### 3.5 R5 — Shrink the tiles

`radar/providers/meteofrance_render._encode_png` writes full 32-bit RGBA with
`optimize=False`:

```python
Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=False)
```

The pixels come from a colour LUT, so the true colour count is far below 2²⁴ — the
composited reflectivity wash blends stops, but a palette PNG (`P` mode + `tRNS`) should
still be visually indistinguishable at a fraction of the bytes.

This pays twice: on the bandwidth-bound static tier (§1), and on the 90-day archive
(the storage-rate question already open in `TODO.md`).

**Measure before deciding.** On the production host, over real tiles:

```bash
cd /var/repos/rainradar
docker compose -f docker-compose.production.yml run --rm --entrypoint python django - <<'PY'
import io, pathlib, random
from PIL import Image
tiles = list(pathlib.Path("/data/tiles/meteofrance").rglob("*.png"))
sample = random.sample(tiles, min(300, len(tiles)))
orig = quant = 0
for p in sample:
    b = p.read_bytes(); orig += len(b)
    im = Image.open(io.BytesIO(b)).convert("RGBA")
    q = im.quantize(colors=255, method=Image.MEDIANCUT)
    out = io.BytesIO(); q.save(out, format="PNG", optimize=True); quant += out.tell()
print(f"{len(sample)} tiles  {orig/len(sample)/1024:.1f} KB -> {quant/len(sample)/1024:.1f} KB"
      f"  ({100*(1-quant/orig):.0f}% smaller)")
PY
```

Two hard constraints if this proceeds. Quantisation happens at **encode** time only —
the linear-Z averaging invariant (CLAUDE.md, design doc) is about the *sampling* stage and
must not be touched. And the archive is immutable: existing tiles are never re-encoded, so
the two encodings coexist for one retention window.

### 3.6 R6 — Re-measure honestly, then size the workers

Do this **before** any of the above, so every later number has a baseline.

```bash
# On the production host, from inside the compose network — no WAN, no Traefik, no TLS.
cd /var/repos/rainradar
docker compose -f docker-compose.production.yml run --rm --no-deps \
  --entrypoint sh django -c \
  'pip install -q aiohttp uvloop && python /app/scripts/tile_bench.py \
     --base http://nginx --allow-prod --steps 8,24,48,96,192 --duration 20'
```

That isolates the app from the WAN and settles whether tier 1's 1,458 req/s was
bandwidth or Nginx. It also re-derives the Django knee without RTT inflating the
low-concurrency rows.

Then check the host's core count (`nproc`) against `WEB_CONCURRENCY`. The tile fallback
is CPU-bound Python, so the ceiling scales roughly linearly with workers — but note the
coupling: peak Postgres connections from the tile path are
`WEB_CONCURRENCY × TILE_ARCHIVE_LOOKUP_CONCURRENCY`, today 4 × 4 = 16 against a
`max_connections` of 100. Raising workers past ~20 needs that arithmetic redone, and
the archiver, Postgres, Redis, Nginx and the host Traefik all share those cores.

---

## 4. R1 in detail — the frame-complete sentinel

### The idea

Miss-ladder tier 2 (design doc §5.2.1) answers "this tile is empty" from Redis, using the
fact that a `status='ok'` frame has every matrix tile either on disk or on the row's
`empty` list. That reasoning needs no database and no application state — only the
knowledge that *this frame is complete*. Nginx can hold that knowledge as a file.

**Contract:** when the archiver marks a frame `status='ok'`, it writes a zero-byte
sentinel at

```
{TILE_ROOT}/{provider}/{UTC-date}/{ts}/.complete
```

Nginx then answers any miss inside a frame directory that has a `.complete` as a
204 with the existing `immutable` header, and proxies to Django only when the sentinel is
absent.

### Verified Nginx rule

Prototyped on `nginx:1.31.3-alpine` — the exact image production runs — against a fixture
tree with one archived tile, one complete frame and one incomplete frame:

```nginx
location ~ "^/tiles/(?<prov>[a-z]+)/(?<tdate>\d{4}-\d{2}-\d{2})/(?<tts>\d+)/\d+/\d+/\d+\.png$" {
  root /data;
  add_header Cache-Control "public, max-age=31536000, immutable";
  add_header X-Robots-Tag "noindex" always;
  try_files $uri @tile_empty;
  access_log off;
}

location @tile_empty {
  add_header Cache-Control "public, max-age=31536000, immutable";
  add_header X-Robots-Tag "noindex" always;
  if (-f "/data/tiles/$prov/$tdate/$tts/.complete") { return 204; }
  proxy_pass http://django;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto $scheme;
}
```

Observed:

| request | result |
|---|---|
| archived tile | `200` + bytes (unchanged) |
| miss, frame has `.complete` | **`204` + `Cache-Control: public, max-age=31536000, immutable`** |
| miss, frame has no `.complete` | falls through to Django |

Notes on the rule:

- The location regex must switch from anonymous to **named** captures. Positional
  captures do not reliably survive the internal redirect into a named location; named
  ones do.
- `if` inside a location is safe here because the block contains only `return` — one of
  the two uses the Nginx documentation explicitly sanctions. Nothing else goes in it.
- `add_header` must be repeated in `@tile_empty`. Header inheritance stops at any level
  that declares its own `add_header`, and the `if`-block inherits from `@tile_empty`
  because it declares none. The 204 carrying `immutable` is the whole point — a
  `Cache-Control`-less 204 would be re-requested on every pan.
- The legacy `/tiles/{date}/…` location rewrites into this same subtree, so it inherits
  the behaviour with no extra rule.

### Backend changes

1. **`radar/storage.py`** — `mark_frame_complete(provider, ts, date=None)` creating the
   frame dir if needed and touching `.complete`; and `frame_is_complete(...)` for tests.
   Idempotent, like `write_tile`.
2. **`radar/archiver.py`** — call it in `archive_frame`, in the same `if status ==
   STATUS_OK:` branch that already does `cache.add_archived(...)`. Writing the sentinel
   only there is what makes it correct: a frame with any errored tile never gets one.
3. **Backfill.** Ninety days of already-archived frames have no sentinel and would keep
   falling through to Django forever. Extend the existing cold-start rebuild — the one
   that repopulates `radar:{provider}:archived` from `_ok_frame_timestamps`
   (`archiver.py`) — to also write any missing sentinel. It already enumerates exactly
   the right set of frames, it already runs on the archiver's first poll after a deploy,
   and it is already idempotent. **No new management command.**
4. **Nothing in `radar/views.py` changes.** The ladder stays exactly as it is. Nginx
   becomes a cache *in front of* the ladder, not a replacement for it — which is what
   keeps the dev stack (no Nginx) and the test suite working unmodified.

### Why this is safe

- **Degradation is downward and silent.** No sentinel ⇒ today's behaviour exactly.
  A partially-written sentinel is impossible (zero bytes). A deleted one costs a Django
  request.
- **Retention needs no change.** The sentinel lives *inside* the frame directory, so the
  janitor's day-directory purge removes it with everything else. `storage.day_dirs` and
  `provider_dirs` filter on directory names at the day level and never see it;
  `dir_size` walks it and adds zero bytes.
- **It cannot leak.** `.complete` does not match the tile location's
  `\d+\.png$` regex, and the terminal `location / { return 404; }` catches everything
  else. The file is unreachable over HTTP.
- **No new failure domain.** No new process, no new network dependency, no new
  dependency for the web container. If Nginx and the archiver disagree, the answer is a
  Django request.
- **The non-negotiables hold.** No new upstream call, no browser-visible upstream, no
  provider knowledge in Nginx (the provider is a path segment, exactly as today).

### The one real risk

A sentinel asserts that every missing tile in its frame is empty. If a tile file is
deleted or corrupted *after* the frame was marked complete, Nginx answers 204 for a tile
that has data — and caches it `immutable` for a year in that visitor's browser.

This is the **same** risk the Redis tier already takes (`radar:{provider}:archived` makes
exactly this claim), so R1 does not introduce a new class of failure — but it does move
the claim to a place with no TTL and no cold-start rebuild other than the archiver's.
Deleting individual tile files out from under a complete frame is not something any code
path does; if that ever changes, it must clear the sentinel too.

### Tests

- `storage`: sentinel path, idempotency, and that `day_dirs` / `provider_dirs` /
  `dir_size` are unaffected by its presence.
- `archiver`: `status='ok'` writes it; `status='partial'` does not; the cold-start
  rebuild backfills a missing one for an existing ok frame.
- Janitor: purging a day removes its sentinels.
- Nginx: the config is not covered by the Python suite. Pin the three-case behaviour
  table above as a shell-level check against the built image, or accept it as verified
  by the prototype and re-verify manually on deploy. **Do not claim CI coverage for it.**

### How to know it worked

Re-run §6's ramp against the `empty_204` pool. Success is the `empty_204` numbers
converging on the `static_200` numbers, and `tile_fallback` log events with
`result="archived_frame"` dropping to near zero for historical timestamps while
continuing for the live frame.

---

## 5. Rejected

- **Raising Postgres `max_connections`.** Considered and rejected in #7 for the same
  reason it stays rejected: it raises a ceiling instead of bounding demand, and adds a
  production knob with nothing keeping it in sync with `WEB_CONCURRENCY ×
  TILE_ARCHIVE_LOOKUP_CONCURRENCY`.
- **A psycopg connection pool (`OPTIONS={"pool": …}`).** Needs the `psycopg[pool]` extra
  (`pyproject.toml` pins `psycopg[c]`), still requires `CONN_MAX_AGE = 0`, and after #7
  the tile path's peak DB concurrency is 16 against a limit of 100. It solves a problem
  we no longer have.
- **Caching tiles in Redis.** Directly contradicts a CLAUDE.md non-negotiable ("Tiles
  live on disk, not Redis"), and Redis runs with persistence off precisely because it
  holds only small reconstructible state.
- **`proxy_cache` on `@django_tiles`.** Would cache 204s in Nginx without any archiver
  cooperation — but it caches by *time*, not by the archive's own notion of completeness,
  so it would need an invalidation story R1 does not. R1 is both simpler and exact.
- **Rewriting Django's built-in middleware as async-native.** See §3.3.
- **Serving tiles from a separate minimal ASGI app.** Would dodge the middleware stack,
  but forks the tile view away from the miss ladder and the provider abstraction to save
  ~1 ms. R1 removes the requests entirely instead.

---

## 6. Verification protocol

Every claim above is falsifiable with the tool in the repo. The order matters:

1. **Baseline (R6)** — ramp from inside the compose network, both pools. Record it here.
2. **Ship R1** — re-ramp. `empty_204` should approach `static_200`.
3. **Ship R2** — re-ramp with `--steps 8,24,120,504`. The c504 row should show 429s and a
   flat p99.9 instead of timeouts.
4. **R4, then R5** — one at a time, re-ramping between, because their effects overlap on
   the same tier.
5. **R3** — only after the admin decision, and it will not move the ramp much; it shows up
   as lower per-request CPU, which is measured by falling `client` cores in the report at
   a fixed offered rate (`--mode rate`), not by a higher plateau.

Run `--mode rate` at ~70% of each measured knee for latency percentiles worth quoting;
the ramp's percentiles suffer coordinated omission by construction.
