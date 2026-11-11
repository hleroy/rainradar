# Rain & Lightning Radar — Architecture & Design

**Status:** describes the shipped system.
**Scope:** self-hosted precipitation-radar and lightning replay app for France, with a
90-day on-disk archive.
**Deployment:** Docker Compose behind an existing host Traefik; RAID-backed tile volume;
an external Prometheus/Loki stack scrapes what the app emits.

This is the single design document for the project. It records *why* the system is
shaped the way it is — the invariants, the trade-offs, and the failure-isolation rules.
For what the code does line by line, read the code; for the working agreement
(commands, git workflow, non-negotiables), read [`../CLAUDE.md`](../CLAUDE.md).

---

## 1. Product summary

A web map showing animated precipitation radar over France (metropolitan territory
including Corsica), with:

- a **live view** replaying roughly the last two hours, refreshed continuously;
- a **90-day archive** any moment of which can be seeked to via a date sheet;
- a **lightning layer**, live over SSE and historical from the archive, synced to the
  radar cursor;
- a **1-hour rain forecast** (Météo-France PIAF, flag-gated) continuing the live timeline
  past the newest measurement, always marked as a forecast (§5.6);
- **storm proximity alerts** around a user-chosen anchor, in the foreground and
  (optionally) via Web Push while the app is closed;
- a **client-side video export** producing a short shareable MP4;
- an **installable PWA** with an offline app shell;
- **FR/EN** interface, no accounts, no ads, no trackers.

The single hardest architectural rule follows from the data licences and from not
wanting third parties to see our visitors: **the browser never contacts an upstream
data provider.** Every radar tile is archived or proxied by the backend and served from
our own paths; the Météo-France API is backend-only; lightning reaches the browser only
through our SSE and history endpoints. The one exception is the OpenStreetMap base map,
which the browser fetches directly — that is OSM's intended use and is credited
accordingly.

---

## 2. Stack

| Layer                 | Choice                                                                 |
| :-------------------- | :--------------------------------------------------------------------- |
| Frontend              | Vanilla ES2020 modules + vendored Leaflet — no build step, no bundler  |
| Base map              | OpenStreetMap tiles (the only cross-origin request the browser makes)  |
| Radar transport       | REST frame index + static PNG tile GETs, all same-origin               |
| Lightning transport   | SSE (live) + REST (history), both same-origin                          |
| Reverse proxy         | Traefik (pre-existing on the host; not declared in this repo)          |
| Static / tile serving | Nginx                                                                  |
| Backend               | Django 6, native async views (**no Channels**)                         |
| ASGI server           | Uvicorn                                                                |
| Scheduler             | APScheduler `AsyncIOScheduler`, in-process, archiver container only    |
| Database              | PostgreSQL 17 (Django ORM + migrations)                                |
| Cache / pub-sub       | Redis 7, persistence off (RDB + AOF disabled)                          |
| Redis client          | redis-py (`redis.asyncio`)                                             |
| Tile storage          | Flat PNG files on the durable volume                                   |
| Radar sources         | RainViewer (default) · Météo-France DPRadar (flag-gated)               |
| Forecast source       | Météo-France PIAF, GRIB2 decoded by hand (flag-gated)                  |
| Lightning source      | Blitzortung.org WebSocket                                              |
| Grid rendering        | numpy + pyproj + Pillow + h5py (no GDAL)                               |
| Web Push              | pywebpush + self-generated VAPID keys                                  |
| Video export          | WebCodecs via vendored mediabunny, in-browser                          |
| Packaging             | uv (`uv.lock` committed; lock generated in-container)                  |
| Containers            | Docker Compose                                                         |
| Observability         | Prometheus-text `/metrics` + structured JSON logs (scraped externally) |
| License               | AGPL-3.0-or-later                                                      |

**Why no Django Channels.** The only pushed stream is lightning, and SSE over a
long-lived async HTTP response subscribing to Redis pub/sub covers it. Django's native
ASGI handles that directly; the channel-layer machinery would add a dependency and a
second message bus for no gain.

**Why no build step.** The frontend is a handful of ES modules and a vendored Leaflet.
Skipping bundling removes an entire toolchain from the deploy path; the cost is that
static assets carry no content hash, which Nginx compensates for with
`Cache-Control: no-cache` + ETag revalidation.

---

## 3. Architecture overview

```
                          Browser (vanilla JS + Leaflet)
                                     │
                                     │ same-origin HTTP + SSE
                          ┌──────────▼───────────┐
                          │       Traefik        │  TLS, routing
                          └──────────┬───────────┘
                                     │
                          ┌──────────▼───────────┐
                          │        Nginx         │  SPA shell, /static,
                          │                      │  /tiles + /forecast
                          │                      │  (static), proxy
                          └──────┬────────┬──────┘
                    tile hit     │        │  /api, /metrics, health,
                    (static)     │        │  tile miss (try_files)
                                 │        │
                        ┌────────▼──┐  ┌──▼────────────┐
                        │ tile files│  │    Django     │
                        │  /data    │  │ (Uvicorn ASGI)│
                        └────────▲──┘  └──┬─────────┬──┘
                                 │        │         │
                                 │  ┌─────▼───┐ ┌───▼──────┐
                                 │  │  Redis  │ │ Postgres │
                                 │  └─────▲───┘ └───▲──────┘
                          writes │        │         │
                          ┌──────┴────────┴─────────┴──────┐
                          │           Archiver             │  not publicly exposed
                          │  APScheduler (exactly 1)       │
                          │   ├ radar poll (RainViewer)    │
                          │   ├ radar poll (Météo-France)* │
                          │   ├ forecast poll (PIAF)*      │
                          │   ├ retention janitor (daily)  │
                          │   └ partition maintenance      │
                          │  Long-lived tasks              │
                          │   ├ Blitzortung WS ingest*     │
                          │   ├ batch writer*              │
                          │   └ push evaluator*            │
                          └──────────────┬─────────────────┘
                                         │ outbound only
        ┌────────────────┬───────────────┼──────────────────┐
        │                │               │                  │
  RainViewer API   Météo-France*    Blitzortung WS*    Push services*
  (poll 5 min)     (radar 1 min,    (persistent)       (FCM/APNs/Mozilla)
                    PIAF 20 s)

  * = flag-gated
```

---

## 4. Services and failure domains

| Service    | Role                                                     |   Public    |           Replicas           |
| :--------- | :------------------------------------------------------- | :---------: | :--------------------------: |
| `traefik`  | TLS termination + routing                                |     yes     | host-level, not in this repo |
| `nginx`    | SPA shell, static assets, tile files, proxy to Django    | via Traefik |              1+              |
| `django`   | Async REST + SSE (Uvicorn)                               | via Traefik |              1+              |
| `archiver` | APScheduler jobs, lightning ingest, push evaluator       |   **no**    |        **exactly 1**         |
| `redis`    | Cache + pub/sub                                          |     no      |              1               |
| `postgres` | Source of truth for frames, gaps, strikes, subscriptions |     no      |              1               |

**The `archiver` must never scale beyond one replica.** It is the only container that
runs APScheduler, the only WebSocket consumer, and the only push evaluator. A second
replica would produce duplicate upstream fetches, duplicate strike rows, and duplicate
notifications. This is enforced structurally: the scheduler bootstrap refuses to start
unless `ARCHIVER_ENABLED` is set, and that variable is set **true** in the archiver
service's compose block and nowhere else (the `django` service pins it false in its
own compose block; it never appears in the shared `.envs/*/.django`).

### 4.1 Five failure domains

The system deliberately separates five concerns so a fault in one cannot degrade the
others. This is the organising principle behind the module layout (`radar/providers/`,
`radar/lightning/`, `radar/alerts/`, `radar/forecast/`).

| Domain             | Runs in                                  | Gated by                                    | A failure here…                                         |
| :----------------- | :--------------------------------------- | :------------------------------------------ | :------------------------------------------------------ |
| RainViewer radar   | archiver poll job + Django fallback view | always on                                   | opens an archive gap; other domains unaffected          |
| Météo-France radar | its own archiver poll job                | `METEOFRANCE_ENABLED`                       | degrades that provider only; RainViewer keeps archiving |
| Lightning          | archiver ingest tasks (WS + writer)      | `LIGHTNING_ENABLED`                         | never blocks or crashes the radar poll                  |
| Storm-alert push   | archiver evaluator task                  | `PUSH_ALERTS_ENABLED` + `LIGHTNING_ENABLED` | never touches radar or lightning ingest                 |
| 1-hour forecast    | its own archiver job (`poll_forecast`)   | `FORECAST_ENABLED` + `METEOFRANCE_ENABLED`  | no forecast is advertised; both radar polls unaffected  |

Isolation is achieved by three mechanisms, in order of importance:

1. **Separate scheduler jobs and separate asyncio tasks.** Each radar provider gets its
   own `IntervalTrigger` job; lightning ingest, the batch writer, and the push evaluator
   are independent supervised tasks. A crash in one is caught, logged, and restarted
   without touching the others.
2. **Exception translation at the boundary.** Every provider maps its own failures onto
   the two Protocol exceptions (`FramesUnavailable`, `TileUpstreamError`), so callers
   need no provider-specific handling.
3. **Thin seams.** Radar and lightning touch in exactly three places: the `lightning`
   advert inside `/api/radar/frames`, the janitor's partition-drop clause, and
   `run_archiver` starting the ingest tasks. The forecast touches the radar side in
   four: the `forecast` advert, `run_archiver` scheduling its job, and two *passive*
   reads of the Météo-France provider (its token holder and its newest frame's
   coverage, §5.6). Keeping those lists short is what makes the isolation claim
   checkable.

The push evaluator sits strictly *downstream* of lightning ingest: it subscribes to the
already-published `lightning:strikes` Redis channel rather than tapping the WS loop, so
it is structurally incapable of slowing ingestion.

---

## 5. Radar providers

### 5.1 The abstraction boundary: everything speaks PNG tiles

Views, the cache, the archiver and the frontend never know which provider is active.
They talk to one interface, and that interface deals exclusively in PNG tiles:

```python
class RadarProvider(Protocol):
    name: str
    frame_interval: int  # seconds between consecutive frames

    # Frame = {timestamp: int(UTC), ref: str}
    async def get_frames(self) -> list[Frame]: ...

    # pooled client reused across one frame
    def tile_client(self) -> httpx.AsyncClient: ...

    # PNG bytes, or None for a legitimate empty
    async def get_tile(self, ts, z, x, y, *, client=None) -> bytes | None: ...

    # mandatory credit, HTML with a link
    def attribution(self) -> str: ...
```

`Frame.ref` is an opaque, provider-defined token — a URL path for RainViewer, a product
URL for Météo-France. Nothing above the provider interprets it.

Two properties of this boundary matter more than the interface itself:

- **The system never represents a geophysical grid.** Reprojection, colour mapping and
  tiling are internal details of the provider that needs them. No grid type flows
  through views, cache or API, so the heavy scientific dependencies stay quarantined in
  one module tree.
- **`frame_interval` is data, not a constant.** The archiver's aged-out-gap arithmetic,
  the poll cadence, and the frontend's gap tolerance all read it from the provider. No
  frame cadence is hardcoded anywhere, which is what let a 300-second provider join a
  system built around a 600-second one without touching the timeline code.

Both providers can be live concurrently server-side. `enabled_providers()` is the source
of truth for which are usable right now, `RADAR_PROVIDER` names the *default* one used
when a request carries no `?provider=`, and an unknown or disabled name is rejected with
HTTP 400 rather than silently falling back.

### 5.2 The two implementations

|                 | RainViewer                                   | Météo-France `DPRadar`                                                       |
| :-------------- | :------------------------------------------- | :--------------------------------------------------------------------------- |
| Role            | Default source, always enabled               | Opt-in, `METEOFRANCE_ENABLED`                                                |
| Upstream output | Display-ready XYZ **PNG tiles**              | Scientific grids: **ODIM HDF5** (rain), **BUFR** (reflectivity)              |
| Units           | Pre-coloured image                           | Physical — *lame d'eau* 1/100 mm, reflectivity dBZ                           |
| Geometry        | Web-Mercator tile pyramid                    | Cartesian mosaic (500 m / 1000 m), needs reprojection                        |
| Auth            | None                                         | OAuth2 client-credentials bearer token                                       |
| Frame index     | `weather-maps.json`, ~2 h of past frames     | `/mosaiques/{zone}/observations/{obs}` — **latest frame only**               |
| Frame cadence   | 600 s (`frame_interval`)                     | 300 s (`frame_interval`)                                                     |
| Poll cadence    | 300 s                                        | 60 s (cheap catalog GET; product downloads only on a new `validity_time`)    |
| Rate limit      | Undocumented; free tier **429s under burst** | 850 req / 5 min                                                              |
| Per-frame work  | 62 tile GETs                                 | 1 product download (2 with the wash) + one in-process render of all 62 tiles |
| Attribution     | "Weather data by Rain Viewer"                | "Données Météo-France (Licence Ouverte 2.0)"                                 |

The decisive difference: **RainViewer hands you tiles; Météo-France hands you raw
geophysical grids.** Putting the latter on a Leaflet map requires a render pipeline the
former does not, so the abstraction was designed around that asymmetry rather than
assuming the two sources look alike.

Two consequences of Météo-France being *latest-only* upstream:

- There is no backfill window. A frame missed is a frame lost, which is why the poll runs
  every 60 s against a cheap catalog endpoint while the ~1.7 MB product is downloaded only
  when `validity_time` advances.
- The web container's fallback tile view can only render frames *that worker* has polled.
  That is acceptable because the archiver persists every frame to disk; the fallback is a
  rare cache-miss path, not the primary tile source.
- …but only because the fallback settles a miss from the *archive* before ever asking
  upstream. A sparse archive stores nothing for an empty tile, so "no file on disk" is the
  normal state for most of the matrix on a quiet day, and every one of those tiles misses
  Nginx and reaches Django. For the newest frame `ts` is still upstream's latest, so each
  such miss would otherwise re-download the ~1.7 MB product and re-render all 62 tiles in
  the web container — the exact cost the archiver already paid.

#### 5.2.1 The tile miss ladder

`views.tile` answers a miss in four tiers, cheapest first. Every "nothing to draw" answer
carries the same `immutable` header as a real tile, so the browser asks once.

1. **Nginx static** — an archived non-empty tile never reaches Django at all.
2. **Redis** (`radar:{provider}:archived`, one `SISMEMBER`). `status='ok'` means every
   matrix tile was attempted and none errored, so each one is either on disk or on the
   row's `empty` list — and the archived set holds exactly the ok frames. "In the set,
   not on disk" therefore already means "nothing to draw", with no row lookup at all.
   This tier is the whole of historical navigation, and it costs Postgres nothing.
3. **The frame row's `empty` list** (`views._archived_empty`, one indexed query), reached
   only for a frame that is not yet fully archived — the live one, or a partial one still
   being retried.
4. **Upstream**, as above.

Tier 3 is **bounded and time-boxed** (`TILE_ARCHIVE_LOOKUP_CONCURRENCY`,
`TILE_ARCHIVE_LOOKUP_TIMEOUT`) by a loop-bound semaphore, because it is the one DB access
in the whole app reached once per *tile* rather than once per request. Django's ASGI
handler runs each request in its own `ThreadSensitiveContext` and a Django connection is
thread-local, so without that bound N concurrent tile misses are N concurrent Postgres
connections — and `/tiles/…` is deliberately unthrottled in Nginx. Unbounded, replaying an
archived day exhausted `max_connections` and turned every tile into a 500.

Both cache tiers are **best-effort**: a Redis hiccup falls through to tier 3, and a tier-3
timeout or DB error sheds with a **503 + `no-store`** (`views._tile_unavailable`) — never
a 500, and never a 204. 204 would claim the tile is empty when we do not know that, and
it is `immutable`, which would pin a blank tile in every visitor's cache for a year.
`tile_fallback` records the outcome as `archived_frame`, `archived_empty`, `gone`,
`fetched`, `empty`, `error` or `db_unavailable`.

A Redis flush (there is no persistence) simply misses tier 2 until `poll_radar`'s
cold-start branch rebuilds the set on the next poll — degrading to the bounded tier 3,
not to an outage.

> **Proposed, not implemented:** load testing on 2026-08-20 showed tiers 2–3 plateau at
> ~700 req/s while tier 1 serves ≥1,458. `specs/tile-serving-performance.md` proposes
> lifting the tier-2 answer into Nginx via an archiver-written `.complete` sentinel per
> frame directory. The ladder above is what runs today.

### 5.3 Upstream fetch throttling and backoff

RainViewer's free tier returns **HTTP 429** under a wide burst even though no limit is
documented, and two callers can burst: a cold-cache page load fans out the whole tile
matrix at once, and the archiver fetches all 62 tiles per frame. Left unbounded, both
saturate the limit and every tile fails — the archiver then writes nothing, the disk
cache never warms, and the next page load bursts again. The worst case is a **cold
start**: after any downtime the whole live window (~13 frames x 62 tiles) is unarchived
and queued back-to-back. Four mechanisms keep the backend inside the limit:

- **A process-wide concurrency cap.** One `asyncio.Semaphore`
  (`UPSTREAM_TILE_CONCURRENCY`, default 4) bounds *simultaneous* upstream tile fetches
  across both the on-demand view path and the archiver batch, independent of how many
  requests arrive. It wraps only the HTTP call, never the backoff sleep, so a waiting
  retry does not occupy a slot.
- **A process-wide rate cap.** `UPSTREAM_TILE_MIN_INTERVAL` (default 0.05 s ⇒ 20 req/s)
  spaces successive request *starts*. Concurrency alone is not a rate limit: four slots
  recycled over a keep-alive connection still emit hundreds of requests a minute, which
  is precisely how a cold start walks into a 429. Pacing is claimed *before* the
  semaphore, so a waiting request holds no slot. This is the high-leverage control — it
  bounds the send rate at the source instead of reacting after a 429.
- **A 429 cooldown, not a 429 retry.** A rate limit is never retried in-request:
  retrying into a limit is what produces the limit. A 429 instead opens a process-wide
  cooldown (`UPSTREAM_RATE_LIMIT_COOLDOWN`, or the server's `Retry-After`, capped by
  `UPSTREAM_RATE_LIMIT_COOLDOWN_MAX`) during which every tile fetch fails fast with
  `RateLimited` without touching upstream. Because the gate *refuses* rather than
  sleeps, a long `Retry-After` is honoured in full at no cost — nothing is held open —
  and the on-demand view returns 502 immediately instead of stalling. A 429 is never
  mistaken for an empty tile, and a 429 on the *frames* endpoint is treated as a fetch
  failure rather than parsed as frames JSON.
- **Abandon the batch when throttled.** `RateLimited` (a `TileUpstreamError` subclass,
  so existing handlers are unaffected) short-circuits the rest of the frame's tiles and
  ends the poll's backfill loop. Grinding a whole cold-start window into a 429 wall only
  deepens the limit while every tile fails anyway; the untried tiles stay out of the
  archived set and are retried on the next poll, past the cooldown.

Both the gate and the async Redis client are **bound to the running event loop and
rebuilt when it changes**. Uvicorn keeps one loop for the process lifetime, but Django's
WSGI `runserver` creates a fresh loop per request — reusing a client from a closed loop
raises "Event loop is closed". The cooldown deadline is deliberately *not* loop-bound: a
monotonic deadline outlives a loop rebuild, and forgetting an active throttle would
resume the hammering.

### 5.4 The Météo-France render pipeline

Entirely internal to `MeteoFranceProvider`. The stages:

```
fetch product (HDF5)  →  parse ODIM grid  →  reproject + sample per tile
                                          →  colour-map through a LUT
                                          →  encode PNG, one per matrix tile
```

Design choices worth recording:

- **No GDAL.** The mosaic is a regular grid carrying a PROJ string in its metadata, so
  reprojection is a plain vectorised coordinate transform via pyproj. Per-tile index
  arrays and the transformer are cached process-wide; because the grid geometry is fixed,
  each of the 62 tiles has its mapping computed once for the process lifetime.
- **Single-flight per frame.** All 62 tile requests for one timestamp share a single
  memoised fetch-decode-render task, so a cold frame costs one download and one render no
  matter how many tiles are asked for concurrently. Only the two most recent frames stay
  memoised.
- **Failures are memoised too, but only briefly.** A failed task cannot stay memoised —
  that would poison the frame for its whole latest-only window. Evicting it *immediately*
  was worse: the archiver admits a frame's 62 tiles through a `TILE_FETCH_CONCURRENCY`-wide
  semaphore, so they reach the memo in waves, and each wave found the memo empty and
  started a fresh download with its own full retry budget — measured at 27 product
  downloads for one failing frame, and as many times the wall-clock, which pushes a frame
  past its own 5-minute cadence while `max_instances=1` blocks the next poll. The failure
  is therefore held for `METEOFRANCE_FAILURE_COOLDOWN` (30 s): long enough that every wave
  of one `archive_frame` shares the single outcome, short enough that the next poll a
  minute later is a genuine retry. Like the RainViewer 429 cooldown, the deadline is
  monotonic and deliberately *not* loop-bound.
- **CPU work off the event loop.** Decode and render run in `asyncio.to_thread`.
- **The colour LUT is anchored on rain rate (mm/h)** and matches RainViewer's "Universal
  Blue" palette, so switching sources does not change how heavy rain reads. The 5-minute
  accumulation is converted to an hourly rate before mapping.
- **Output-space smoothing.** The composite is a 500 m step function, visibly blocky next
  to RainViewer's. A modest Gaussian in tile space removes the blockiness without melting
  cells into blobs.

### 5.5 The reflectivity wash

`METEOFRANCE_REFLECTIVITY_ENABLED` (off by default) adds a second product per frame — the
REFLECTIVITE mosaic — rendered as a "wet atmosphere" wash *under* the rain and flattened
into the same single tile. It exists because LAME_D_EAU's smallest quantum is already
0.12 mm/h, so the light drizzle and virga a reflectivity field shows are simply absent
from the rain product, and no amount of blurring invents them.

Four rules govern it:

1. **Reflectivity is averaged in linear Z, never in dB.** dBZ is logarithmic and the
   mosaic's no-echo floor is −40, so blurring dBZ directly drags every echo toward that
   floor: a lone 20 dBZ cell lands at −36 dBZ at its own centre and vanishes. The
   smoothing step converts to linear Z, blurs, and converts back. Which domain a LUT
   smooths in is a property of the LUT, and the rain LUT must stay linear — mm/h already
   is.
2. **Best-effort throughout.** Any failure in the reflectivity arm — catalog, download,
   BUFR decode, deadline overrun, or a mosaic too far from the rain frame's instant —
   degrades the frame to byte-identical rain-only output rather than losing it. The arm
   is time-boxed as a whole, because "best-effort" has to cover *slowness* as well as
   failure: it carries its own retry budget and the rain arm waits on it, so an unbounded
   arm would let a merely sluggish endpoint stall a frame past its 5-minute cadence.
3. **Two independent staleness checks.** The catalog's `validity_time` is upstream's
   claim; the BUFR message's own nominal time is the payload's. A catalog advertising a
   fresh validity while serving a stale product passes the first and fails the second.
   Both layers are flattened into one tile, so a stalled wash would write hours-old
   moisture into a 90-day archive indistinguishably.
4. **The decoder validates the message template, originating centre, and local table
   version**, so upstream schema drift raises rather than mis-decoding.

A consequence accepted at sign-off: the wash palette reuses the rain LUT's own warm low
stops, so the composite reads as one continuous ramp from haze through drizzle into rain
rather than as two separable signals. **Never describe the wash in UI copy as a distinct
measurement, and never let it drive an alert.** Its presence is a measurement; its extent
is a presentation choice (the chosen blur paints roughly 2.8× the unblurred echo area).

### 5.6 The 1-hour forecast (not a provider)

A short forecast continues the live timeline: Météo-France's **PIAF** (*Prévision
Immédiate Agrégée Fusionnée*), a 5-minute, 0.01° rain forecast published every 5
minutes on the same API portal as DPRadar. For its first hour PIAF is radar
extrapolation blended with the AROME-PI model — what we would otherwise have computed
ourselves with `pysteps`, at worse quality. The research, the product decisions and
the Phase 0 measurements behind it are in [`forecast-nowcast.md`](forecast-nowcast.md);
this section records the shape that shipped.

**It is a fifth failure domain, and deliberately not a `RadarProvider`.** A provider
means "past frames, archived idempotently for 90 days"; a forecast run is superseded
every 5 minutes and never archived. Routing it through the provider would entangle gap
accounting, `RadarFrame` rows and retention with disposable data. It mirrors the
lightning seam instead: a `ForecastSource` protocol in `radar/forecast/base.py`, the PIAF
client behind it, and one archiver job, `poll_forecast` (`max_instances=1`, every 20 s),
scheduled only when `FORECAST_ENABLED` and `METEOFRANCE_ENABLED` are both on. Its
upstream failures map onto `ForecastUnavailable` and stop at the job; any other error
while building a run (a full disk, a render error, a bug) fails that run the same way —
cooled down for `FORECAST_FAILURE_COOLDOWN` and counted as `crashed` — rather than
escaping to `_safe` and being retried every tick. No Postgres row is written.

**One tick.** Runs sit on a fixed 5-minute clock and land ~9 minutes after their
reference time (529–581 s measured), so the job computes the run that should exist and
probes it directly — the 2 MB `GetCapabilities` document is never fetched, and no
coverage ID is ever read from upstream XML. **404 means "not published yet"** and costs
a ~600-byte answer; it is a retry, never a failure. Once a run answers, its 13 lead
times (10–70 min: lead 5 is always already measured by then) download under a
concurrency cap, decode, render and land on disk frame by frame; only when every frame
is done is the run published. A run that fails is left alone for 60 s, so a persistent
fault costs one download a minute rather than one per tick. Steady state: ~3 small
probes and 13 × 3.2 MB per run — the server does not compress, so **~12 GB a day**,
about 20× the observation poll.

**Decoding trusts nothing upstream says about itself.** The payload is one GRIB2
message, simple packing, 16 bits, no bitmap — a big-endian `uint16` array — so
`grib_decode.py` is hand-written in the style of `bufr_decode.py` rather than pulling in
~45 MB of native ecCodes. Its value is the validation: centre, templates, grid geometry,
reference time and valid time are all pinned, because the service once answered a
request with somebody else's GRIB under a 200, and because three labels (coverage
name, GRIB parameter, `DescribeCoverage`) call the field a *rate* in kg m⁻² s⁻¹ when the
numbers are **millimetres accumulated over 5 minutes** — the same quantity as
`LAME_D_EAU`, converted ×12 to mm/h. Drift raises; it never mis-decodes.

**Rendering reuses the observation pipeline, masked to the observation's coverage.**
The decoded field goes through `meteofrance_render`'s rain LUT and tile loop, so
measured and forecast frames read as one picture. PIAF has no "no data": outside the
radar network it carries model rain (Bay of Biscay, northern Spain, open sea — ~11 % of
the bbox). That rain is hidden: the forecast is masked with the `nodata` cells of the
newest `LAME_D_EAU` frame, which the Météo-France provider keeps for exactly this
(`latest_coverage()`), so the forecast never shows rain where the measurement cannot.
No run is published against a coverage more than an hour old. A run keeps the coverage
its first frame was masked to until it is published, even across several ticks, so its
frames never mix masks and its record's `coverage_ts` is exact; frames found on disk
without that pin (an archiver restart mid-run) are redone rather than mixed.

**The token is shared, not duplicated.** Minting a Météo-France token usually revokes
the application's previous one within seconds, so the forecast client uses the
provider's own `MeteoFranceAuth` instance; a second instance in the same process would
knock out the first. A 401 invalidates that shared token and retries once — but only
the token *that request carried*: `invalidate(stale)` is a no-op once the cache holds a
newer one. Concurrent callers (the forecast's leads, the observation poll) get their
401s back at different moments, and a late one clearing the token another 401 had just
minted would mint again, revoking it under everyone else.

**Publication is atomic.** A frame's tiles are written atomically, then its `.done`
marker; the run's `run.json` record only after every frame is done, and the Redis
pointer `forecast:current` only after that (§6.2, §11). A half-written run is never
advertised, a retry re-fetches only frames without a marker, and a Redis flush is
repaired from disk on the next tick. The job keeps the newest three complete runs, so a
client mid-animation never loses its tiles, and deletes the rest itself — the
retention janitor never sees this tree.

**Serving decides staleness, not the job.** The `forecast` advert in
`/api/radar/frames` (§12) shows a run's frames only while they are worth showing:
never past `FORECAST_MAX_RUN_AGE` (20 min, one missed run), never at or before the
newest measured frame (once measured, the measurement wins), never beyond
`FORECAST_HORIZON_SECONDS` (60 min) past it, and never at all if the first remaining
frame would leave a hole after the newest measurement. No forecast is better than a
stale one shown as current. How the frontend keeps it apart from measurements is §13.10.

**What the forecast never does**: appear for RainViewer (its palette differs, so the
boundary would change colour scale mid-animation), appear for an archived date (past
runs are not kept), appear in the video export, drive a storm alert, or be described
in UI copy as a measurement.

---

## 6. Coverage, tile matrix and storage

### 6.1 The matrix is computed, never listed

```
RADAR_BBOX  = [41.2, 51.5, -6.0, 9.7]   # S, N, W, E — metropolitan France incl. Corsica
zoom levels = 3 … 7                      # 7 is RainViewer's max native zoom
tiles/frame = 62                         # DERIVED from the bbox, never hardcoded
```

The set of `(z, x, y)` tuples is computed from the bbox with the slippy-map formula at
startup, so the bbox stays the single source of truth. A test asserts the count is 62;
the Météo-France renderer iterates the same computed matrix. Changing coverage means
changing one constant.

The tile view also *restricts* requests to that matrix: an in-grid tile outside France
returns 404 rather than triggering an upstream fetch and an on-disk write, so the archive
cannot be inflated by crafted requests.

### 6.2 On-disk layout

Tiles live on disk, not in Redis. `radar/storage.py` is the single home for all tile path
maths and disk I/O, so the two rules below live in exactly one place.

```
{TILE_ROOT}/{provider}/{YYYY-MM-DD}/{ts}/{z}/{x}/{y}.png
            └ source ┘ └ UTC date ┘ └epoch┘ └ slippy indices ┘
```

- **The date directory is the UTC date of the frame timestamp.** All archive time maths
  is UTC and explicit (`time.gmtime`, `datetime.now(tz=UTC)`), independent of Django's
  display `TIME_ZONE`.
- **Writes are atomic**: a temp file named with pid + uuid, then `Path.replace()`. A
  half-written tile is never visible or served, and two concurrent writers of the same
  tile never share a temp name.

Nginx serves this tree directly with a strict numeric regex (path-traversal defence) and
`Cache-Control: public, max-age=31536000, immutable` — archived tiles never change — with
a `try_files` fallback to Django for tiles not yet written.

**Forecast tiles have their own root** (`FORECAST_ROOT`, `/data/forecast` on the same
volume), never under `TILE_ROOT`:

```
{FORECAST_ROOT}/{run_ts}/{valid_ts}/{z}/{x}/{y}.png
{FORECAST_ROOT}/{run_ts}/{valid_ts}/.done      every tile of that frame written
{FORECAST_ROOT}/{run_ts}/run.json              every frame done: the run's record
```

The archive tree is immutable per frame and swept by the janitor and the storage gauge;
forecast frames are superseded every run. Keying the path — and the URL,
`/forecast/{run}/{valid}/{z}/{x}/{y}.png` — by run **and** valid time makes each tile
unique and therefore `immutable` too, though the same valid time changes with every run.
Only non-empty tiles are written. Nginx serves them with the same strict regex, which
also keeps the markers unreachable, and sends misses to Django, which answers from disk
alone: 204 (`immutable`) for an empty tile of a done frame, an uncached 404 for anything
else. It never fetches upstream.

The forecast job prunes this tree **on every tick**, whatever the tick did: it keeps the
newest `FORECAST_RETAIN_RUNS` complete runs, the published one, and any run still in the
candidate window, and deletes the rest. A partial run older than that window can never
be finished, and since the storage gauge only measures `TILE_ROOT`, pruning only after a
publish would let runs that keep failing part-way fill the shared volume unseen.

### 6.3 Storage budget

Provisioned volume is 20 GiB (`STORAGE_CAPACITY_BYTES`), which backs the
`radar_storage_used_ratio` metric; the host alerting stack warns at 75 %. Radar tiles
dominate; lightning rows, Redis and Postgres overhead are rounding errors beside them.
Enabling the reflectivity wash costs additional disk, since more tiles are non-empty.
The forecast costs a few tens of MB at most (three runs of ~10–16 MB), outside
`TILE_ROOT` and so outside the storage gauge.

---

## 7. The archiver

### 7.1 Poll and backfill are the same code path

On archiver start **and** on every poll:

```
1. Fetch the provider's frame index
2. Diff its timestamps against the Redis set radar:{provider}:archived
3. For every timestamp not yet fully archived → fetch + store its tiles
4. Close any ongoing gap for this provider
5. Record an aged-out gap for frames that fell out of the upstream window unseen
```

Any archiver outage **shorter than the upstream backfill window** therefore self-heals on
restart; longer outages leave a permanent, logged gap. `restart: always` alone would not
achieve this — backfill is what makes restarts safe. Because backfill *is* the poll, the
resilience was never bolted on.

**Archiving is idempotent.** Tiles already on disk are skipped without an upstream fetch,
so a re-run after a partial archive only retries the holes. Only fully-archived frames
join the `archived` set, so partial and failed frames are retried on the next poll and
the existing-file check keeps that cheap. The set itself is reconstructible: on a cold
start (or after a Redis flush) it is rebuilt from the `ok` frame rows in Postgres.

An empty tile is a normal result, not a failure. RainViewer answers 404 for a
no-precipitation region; the renderer produces nothing for one. Neither is stored, and
neither marks the frame partial — which is also why a missing tile for an archived frame
is served as **204, not 404**: the tile is valid, there is simply nothing to draw. A 2xx
keeps the browser console clean while Leaflet renders a blank tile.

Empty tiles are, however, **recorded** in `radar_frame.empty`, and skipped on a re-run.
A published frame is immutable upstream, so a tile that came back empty once is empty
forever — but "no file on disk" alone cannot distinguish *empty* from *never fetched*.
Without the record, every retry of a partial frame re-downloaded its entire empty
portion of the matrix for as long as the frame stayed partial, which is exactly the
state a rate limit puts frames into. With it, a retry costs only the tiles that
actually failed.

### 7.2 Gap accounting

Two kinds of gap are recorded in `archive_gap`:

- **Ongoing** (`gap_end IS NULL`) — upstream is currently unreachable. Opened only after
  `GAP_OPEN_AFTER_FAILURES` consecutive failed polls, so one flaky poll does not churn
  open/close rows, and closed on the next success.
- **Aged out** (bounded) — frames between the last archived timestamp and the oldest
  timestamp still upstream were never collected and can no longer be. Recorded closed,
  and suppressed when an existing gap already covers the window so outages are not
  double-counted.

Gaps are per-provider, and the frontend receives those overlapping the requested window
so the timeline can render them as holes rather than pretending the data is continuous.

### 7.3 The retention janitor

Runs daily at `JANITOR_HOUR` UTC and is safe as a no-op:

- delete whole day directories older than `RETENTION_DAYS` (90) under **every** provider
  subtree present on disk, so a provider turned off after use is still pruned;
- delete the corresponding `radar_frame` rows and drop their timestamps from each
  provider's Redis archived set (timestamps collide across providers, so this must be
  provider-scoped);
- drop lightning month partitions past the horizon;
- prune push subscriptions untouched past `PUSH_STALE_DAYS`;
- reconcile the storage gauge with a full rescan, correcting the drift that accumulates
  from the O(1) per-poll increments.

The lightning and push clauses are individually caught and logged: a failure in either
must never fail radar retention.

---

## 8. Lightning

### 8.1 Ingestion: decouple reading from persisting

```
Blitzortung WS ──► asyncio.Queue(bounded) ──► batch writer task
  (read + decode +     (drop-oldest on         (bulk INSERT + one pipelined
   bbox filter)         overflow, counted)      Redis fan-out)
```

**The WS read loop must never block on persistence.** It never awaits `queue.put`; when
the queue is full it discards the *oldest* strike and counts the drop. Storms produce far
more strikes than anyone replays individually, so shedding the oldest is the right
trade — and a slow Postgres write or Redis publish can neither stall nor drop the
connection.

A DB write failure drops only the batch in hand and is logged; the writer keeps running,
so a Postgres blip never kills ingest. Reconnection uses capped exponential backoff with
jitter, cycling through several Blitzortung endpoints.

### 8.2 The adapter is a quarantine point

Blitzortung's protocol is undocumented, uses a custom LZW-variant compression, and offers
no SLA. All of it — the handshake, the decompression, the frame decode, the parse-failure
logging — lives behind one adapter module implementing a `LightningSource` interface, so
a protocol change touches one file. A single bad frame is logged and swallowed rather than
killing the connection.

`intensity` is stored as a **proxy, not a calibrated amperage**: Blitzortung exposes no
current, so the adapter records the detecting-station count (capped to smallint) or NULL.

Blitzortung's community data is **non-commercial**; the credit is mandatory whenever the
layer is active.

### 8.3 Serving

- **Live** — `/api/lightning/stream` is an SSE response that first replays the recent
  Redis buffer (so a fresh client sees the current storm immediately), then tails the
  `lightning:strikes` pub/sub channel. It touches only Redis and never holds a DB
  connection open for the stream's lifetime. Comment heartbeats keep the connection alive
  through proxies and surface dead clients. Nginx has an exact-match location for it with
  buffering off and a long read timeout, and Django sends `X-Accel-Buffering: no` as
  belt-and-suspenders.
- **History** — `/api/lightning/history` reads the partitioned table with a bounded span
  (24 h) and a strike cap, ordered newest-first before truncation so the cap keeps the
  *most recent* strikes, then reversed for the client.

### 8.4 Presentation is driven by the radar cursor

Because the map has a timeline (unlike a static strike snapshot), the lightning overlay
is slaved to the radar cursor: the frame at time *T* shows exactly the strikes in its own
slice `(previous frame, T]`. Scrubbing or playing animates rain and lightning in
lockstep. Strikes are drawn on a Canvas overlay that never intercepts map interactions.
A forecast frame (§5.6) is in the future, so its slice is empty: otherwise the first
one would show strikes that happened after the newest radar measurement as if they went
with the forecast rain. The strike history is fetched only up to that measurement.

That overlay is a hand-rolled `L.Layer`, so it has to opt into Leaflet's zoom
contract explicitly or it desynchronises from the tiles: it CSS-transforms the canvas
on **both** `zoomanim` (the animated wheel/double-click zoom) and `zoom` (a pinch,
which never animates), sized against the view its pixels were drawn for, then
re-projects them for real on `zoomend`. Handling only the end of the gesture leaves
the strikes frozen while OSM and the radar scale, and jumping into place at the end.

The transform and the pixels are one unit, so *every* repaint re-asserts it: a live
strike can land mid-gesture, and a pinch held still fires no further `zoom` to correct
the transform the previous draw-state left behind. Because the re-assert runs after the
draw-state is refreshed, it collapses to scale 1 at the current top-left — identical to
the translate `_reset` writes — whenever no zoom is in flight.

---

## 9. Storm alerts

Two independent delivery paths share one set of rules: **30 km ("storm approaching") and
10 km ("storm overhead") rings around a user-chosen anchor, a 30-minute per-tier quiet
window, and a 10-minute freshness cutoff** so a replayed buffer never produces a late
notification. The constants are duplicated between `frontend/js/alerts.js` and
`radar/alerts/__init__.py` and must stay identical.

### 9.1 Foreground (client-side)

`frontend/js/alerts.js` opens its **own** SSE connection and evaluates each arriving
strike against the anchor — deliberately independent of the lightning layer's visibility
toggle and of the radar cursor, because a user who armed alerts wants them whether or not
the layer is displayed. It is the most expendable failure domain: it never reaches into
the lightning module, and a WS drop, parse error, or Notification API failure can only
toast or skip a strike. It is hidden entirely without the backend lightning advert,
requests notification permission only inside the confirming gesture, adds no backend
endpoint and no upstream call, and is never drawn into the video export.

### 9.2 Background (Web Push)

Flag-gated behind `PUSH_ALERTS_ENABLED`, archiver-only, and structurally downstream of
ingest. The evaluator subscribes to `lightning:strikes` exactly as the SSE view does and
applies the same tier logic to each stored `PushSubscription`. Running in the
single-replica archiver is what makes the per-subscription throttle check-then-set in
Redis race-free — another reason never to scale that container.

Security and privacy properties, all load-bearing:

- **The stored endpoint URL is an SSRF vector**, since the server POSTs to it. Endpoints
  are validated against a hostname allow-list of known browser push services, matching on
  the parsed hostname only — never a substring of the URL — so
  `https://push.apple.com@evil.com` is rejected.
- **The anchor is coarsened to ~1 km (2 decimals) before storage.** There are still no
  accounts; the endpoint URL is the identity.
- **Unsubscribe deletes the row**, a dead endpoint (404/410) prunes it, and the daily
  janitor removes rows not refreshed within `PUSH_STALE_DAYS`.
- `radar/alerts/webpush.py` is the **only** module importing `pywebpush`. The library is
  synchronous, so every send goes through `to_thread` with a semaphore and a timeout.
- **VAPID keys are self-generated** — no vendor accounts. Enabled-but-unset fails fast at
  settings import, so a misconfigured deployment cannot start looking healthy.
- The service worker's `push` handler shows the **server-rendered strings verbatim**;
  notification copy is localised server-side from the subscription's stored locale.

With the flag off, the endpoints 404, the advert reports disabled, the evaluator never
starts, and behaviour is exactly the foreground-only case.

---

## 10. Data model

```sql
-- One archived radar frame per (provider, timestamp)
radar_frame (
    id            BIGSERIAL PRIMARY KEY,
    timestamp     BIGINT,        -- UTC epoch seconds as the provider reports it
    provider      VARCHAR(20),
    collected_at  TIMESTAMPTZ,
    tile_count    SMALLINT,      -- PNGs actually present
    status        VARCHAR(10),   -- 'ok' | 'partial' | 'failed'
    missing       JSONB,         -- [{z,x,y}, ...] tiles that errored
    empty         JSONB,         -- [{z,x,y}, ...] tiles upstream had nothing to draw for
    UNIQUE (provider, timestamp)
)

-- Collection outages, per service and provider
archive_gap (
    id         BIGSERIAL PRIMARY KEY,
    service    VARCHAR(20),      -- 'radar' | 'lightning'
    provider   VARCHAR(20),
    gap_start  TIMESTAMPTZ,
    gap_end    TIMESTAMPTZ,      -- NULL = ongoing
    reason     TEXT,
    detail     JSONB
)

-- Strikes, monthly RANGE-partitioned by struck_at
lightning_strike (
    id         BIGSERIAL,
    struck_at  TIMESTAMPTZ NOT NULL,
    lat        REAL,
    lon        REAL,
    intensity  SMALLINT,         -- station-count proxy, or NULL
    PRIMARY KEY (id, struck_at)
) PARTITION BY RANGE (struck_at);

-- Web Push subscriptions (the app's only per-user state; still no accounts)
push_subscription (
    id            BIGSERIAL PRIMARY KEY,
    endpoint      TEXT UNIQUE,   -- push-service URL = the identity
    p256dh        VARCHAR(200),
    auth          VARCHAR(200),
    lat           DOUBLE PRECISION,  -- anchor, coarsened to 2 decimals
    lon           DOUBLE PRECISION,
    locale        VARCHAR(5),
    created_at    TIMESTAMPTZ,
    last_seen_at  TIMESTAMPTZ    -- refreshed on every upsert; drives janitor pruning
)
```

Notes:

- **`radar_frame`'s identity is `(provider, timestamp)`, not the timestamp.** Both
  providers emit epoch timestamps that collide on shared boundaries, so the PK is a
  surrogate and every frame query is provider-filtered.
- **`lightning_strike` is raw-SQL managed** (`managed = False` on the model). The
  partition key must be part of the primary key, which Django's ORM cannot express, so
  the table and its partitions are created by migration DDL and a partition helper. The
  ORM still inserts normally — the server generates `id` and `struck_at` routes each row
  to its month — and range reads on the parent let Postgres prune partitions.
- **Partitions are pre-created for months N+1 and N+2** by a daily job, so an insert never
  falls through to the DEFAULT partition on the 1st. The DEFAULT partition exists as a
  safety net and is never dropped.
- **All time integrity is UTC**: `USE_TZ = True`, every datetime column is `timestamptz`,
  containers run UTC, and both upstreams report UTC so there is no conversion at ingest.
  Django's display `TIME_ZONE` is irrelevant to any of it. The one local-time surface is
  the browser: every label renders in the viewer's zone, so the date sheet picks a
  **local** calendar day (local midnight → next local midnight, 23 h / 25 h across DST;
  `frontend/js/localtime.js`) and sends it as an epoch `from`/`to`. The API, the archive
  and the tiles' UTC date folder never see a local date.

Migrations must be **backward-compatible (expand/contract)** — they run against the live
database before old containers are swapped out.

---

## 11. Redis: a throwaway tier

Persistence (RDB and AOF) is **off**. Redis holds only small, fully reconstructible
state; Postgres and the filesystem are the sole sources of truth, so losing Redis costs
nothing but a cold cache.

| Key                                                                                                        | Type                 | TTL               | Holds                                                      |
| :--------------------------------------------------------------------------------------------------------- | :------------------- | :---------------- | :--------------------------------------------------------- |
| `radar:{provider}:frames_json`                                                                             | string               | 60 s              | Raw upstream frame-index body                              |
| `radar:{provider}:frames_live`                                                                             | string               | 15 s              | Assembled `/api/radar/frames` live response                |
| `radar:{provider}:archived`                                                                                | set                  | none              | Epoch seconds of fully-archived frames                     |
| `radar:{provider}:range_json`                                                                              | string               | 60 s              | Archive bounds for the date picker                         |
| `radar:{provider}:last_poll_ts` · `radar:last_poll_ts`                                                     | string               | none              | Last successful poll epoch (per-provider + global)         |
| `radar:{provider}:consec_failures`                                                                         | string               | none              | Consecutive failed polls, for gap-open hysteresis          |
| `radar:tile_dir_bytes`                                                                                     | string               | none              | Storage gauge, incremented per poll and rescanned daily    |
| `stats:json` · `metrics:text`                                                                              | string               | 60 s / 30 s       | Rendered `/api/stats` and `/metrics` payloads              |
| `lightning:strikes`                                                                                        | pub/sub channel      | —                 | Live strike fan-out (SSE views + push evaluator subscribe) |
| `lightning:recent`                                                                                         | list (capped 2000)   | none              | Recent strikes replayed to a joining SSE client            |
| `lightning:ws_connected` · `…_since` · `reconnects` · `queue_dropped` · `strikes_total` · `last_strike_ts` | string               | none              | Ingest gauges and counters                                 |
| `alerts:throttle:{sub_id}`                                                                                 | hash `{outer,inner}` | 2 × re-arm window | Last in-ring strike epoch per tier                         |
| `alerts:push_sent` · `push_failed` · `push_pruned`                                                         | string               | none              | Push delivery counters                                     |
| `forecast:current`                                                                                         | string               | none              | The published forecast run's record; rebuilt from disk     |
| `forecast:count:{not_ready,fetch_failed,rejected,crashed,bytes,runs_published}`                            | string               | none              | Forecast job counters                                      |

Keys are **provider-namespaced wherever they are provider-specific**, so switching
sources never serves state belonging to another one.

The micro-caches deserve a word. Every visitor hits `/api/radar/frames` on page load and
on each periodic refresh, yet the payload only changes when a new frame lands or a gap
opens; a 15-second TTL makes the Postgres load independent of visitor count while staying
imperceptibly fresh. `/metrics` and `/api/stats` are cached for the same reason and one
more: both run aggregate queries including an unbounded `COUNT(*)` over the partitioned
strike table, and `/metrics` is public, so scrape cadence is not under our control.

---

## 12. API contract

All JSON views are `async def`, carry `Cache-Control: no-cache` (store, but always
revalidate — the payloads have no content hash), and are covered by
`ConditionalGetMiddleware` so an unchanged payload revalidates as an empty 304. The SSE
stream sets its own headers and is exempt.

| Endpoint                                            | Returns                                                                               | Notes                                                                                                                                            |
| :-------------------------------------------------- | :------------------------------------------------------------------------------------ | :----------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/radar/frames[?provider=][&from=&to=]`     | `{frames, provider, attribution, providers[], gaps[], bbox, lightning{}, forecast{}}` | Live window (micro-cached 15 s) or a historical range (≤ 36 h, uncached)                                                                         |
| `GET /api/radar/latest[?provider=]`                 | `{timestamp}`                                                                         | Newest live frame straight from the provider                                                                                                     |
| `GET /api/radar/range[?provider=]`                  | `{earliest, latest}`                                                                  | Per-provider archive bounds for the date picker (Redis-cached 60 s)                                                                              |
| `GET /tiles/{provider}/{date}/{ts}/{z}/{x}/{y}.png` | PNG, `immutable`                                                                      | Nginx-static; `try_files` falls back to Django, which answers a known-empty tile 204 (also `immutable`) before going upstream                    |
| `GET /forecast/{run}/{valid}/{z}/{x}/{y}.png`       | PNG, `immutable`                                                                      | Nginx-static from `FORECAST_ROOT`; Django answers misses from disk only: 204 (`immutable`) in a done frame, else an uncached 404. Never upstream |
| `GET /api/lightning/stream`                         | SSE `event: strike`                                                                   | Replays the recent buffer, then tails Redis pub/sub; heartbeat comments; never buffered                                                          |
| `GET /api/lightning/history?from=&to=[&bbox=]`      | `{strikes[], truncated, attribution}`                                                 | ≤ 24 h span, ≤ 50 000 strikes (newest kept)                                                                                                      |
| `POST /api/alerts/subscribe`                        | `{ok:true}`                                                                           | Upsert a Web Push subscription by endpoint; 404 when the flag is off                                                                             |
| `POST /api/alerts/unsubscribe`                      | `{ok:true}`                                                                           | Deletes the row; idempotent                                                                                                                      |
| `GET /api/stats`                                    | About-dialog JSON                                                                     | Redis-cached 60 s; degrades to nulls, never 5xx                                                                                                  |
| `GET /metrics`                                      | Prometheus text                                                                       | Public by design; Redis-cached 30 s                                                                                                              |
| `GET /healthz` · `GET /readyz`                      | `{status}`                                                                            | Liveness (always 200) · readiness (Redis + DB reachable)                                                                                         |

### 12.1 `/api/radar/frames` is the bootstrap document

One request tells the frontend everything it needs to configure itself: the frame list,
the active provider and its attribution, **every** enabled provider with its label,
attribution and `frame_interval` (which drives the source switch and the gap tolerance),
the archive gaps overlapping the window, the coverage bbox (so Leaflet never requests
tiles outside the matrix), and the lightning advert — whether the layer exists, its
attribution, bbox, display window, and whether Web Push is available with which VAPID
key — and the forecast advert. A client that does not understand a field ignores it,
which is how the layer, the alerts, the source switch and the forecast were each added
without a breaking change.

The forecast advert is `{enabled, provider, run, frames: [{timestamp}], attribution,
tile_base}`. `enabled` is `FORECAST_ENABLED` and `provider` the radar source it extends
(`meteofrance`), so a client can tell "not served here" from "nothing to show right
now". `frames` is empty — with `run` and `tile_base` null — for any other provider, for a
historical range, and whenever the staleness rules of §5.6 drop the run. A frame's tiles
are `{tile_base}/{timestamp}/{z}/{x}/{y}.png`. `attribution` is the PIAF credit; the
licence's other requirement, the date of last update, is `run`, which the client
formats (§12.2).

### 12.2 Timestamps are raw epochs, never pre-formatted labels

The API returns `timestamp` and nothing else. Presentation belongs to the frontend, which
formats via `Intl` in the active locale. A server-side label would leak presentation into
the contract and break the moment the user switches language client-side.

### 12.3 Degradation rules

- Frames: if the archive query fails or returns nothing, the live window falls back to
  the provider directly so LIVE never goes blank; only a provider failure yields 503.
- Historical queries never fall back — an unreachable database is an honest 503.
- Tiles: unknown provider, out-of-matrix, or mismatched date → 404. Archived frame with
  no tile on disk → 204. Upstream error after retries → 502.
- The forecast advert reads only its Redis pointer. A Redis error or a mangled record is
  an advert with no frames, never a failed `/api/radar/frames`.
- Forecast tiles: done frame without the tile → 204; pruned run, unfinished frame,
  off-matrix tile or flag off → uncached 404.
- `/api/stats` never 5xxs; a failing piece renders as `null`.

---

## 13. Frontend

`frontend/` is vanilla ES2020 modules with vendored Leaflet and mediabunny, served as
static files. `main.js` wires the map and hands each subsystem its dependencies
explicitly, which keeps the additive modules genuinely removable.

### 13.1 What LIVE means

LIVE is a claim about the **cursor**, not about the loaded window: the pill blinks only
while the cursor sits on the newest frame, and `radar.js` keeps a single `following`
flag as the one writer for both that state and the pill (plus `aria-pressed`, so the
claim reaches screen readers and not just sighted users).

The periodic refresh follows from it. While following, a newly landed frame moves the
cursor to it — otherwise the default paused state pins the cursor to whichever frame the
page loaded with, and the view silently rots for up to the whole 2 h window while the
index keeps refreshing and the window-end label marches on. Once the user has
deliberately scrubbed or stepped away, their chosen frame is kept instead, until it ages
out of the live window and there is nothing left to stay on. Scrubbing back onto the
newest frame re-arms following, and so does the LIVE button — which additionally
re-anchors the refresh and clears any 429/503 backoff, because pressing it is a
"give me the current picture" gesture and a client parked at the backoff ceiling must
not stay there. Returning to a hidden tab is read as the same gesture, and clears the
backoff the same way.

**The refresh is anchored, not periodic.** A fixed period is the wrong shape here: one
equal to the cadence — or a multiple of it — aliases against publication, so a client
that happens to land just before each new frame stays a whole frame behind
*indefinitely*. That is what a hardcoded 5 min did against Météo-France's 300 s. So the
next look is scheduled off the newest frame's **own timestamp**:
`newest_ts + frame_interval + lag`, where `frame_interval` comes from the `providers`
advert (§12.1) — the frontend hardcodes no cadence of its own, and the same helper feeds
the gap tolerance.

`lag` is the delay between a frame's timestamp and the moment it is actually fetchable —
measured in production at ~140–217 s (Météo-France) and ~94–339 s (RainViewer), too wide
a spread to pick a constant that is neither wasteful nor late. It is therefore *tracked*:
each landed frame yields `now - newest_ts`, which is an upper bound on the true delay
(the client only looks at discrete moments), so a small decay is subtracted before
storing and the value is clamped. The estimate probes earlier each cycle; a wake that is
too early costs one short retry and ratchets it straight back up. It converges on each
provider's real behaviour with no provider-specific constant.

Simulated against the measured lag bands, the steady state is ~8 requests/h on RainViewer
(down from the fixed 12/h) and ~16/h on Météo-France (up from 12/h) — and on **both** it
sees every published frame, which the fixed period could not promise. Météo-France costs
slightly more because it publishes twice as often; that is the trade the aliasing fix buys,
and the endpoint is micro-cached 15 s and ETag-revalidated, so most of those requests are
empty 304s.

Three guards keep that from degenerating. **Jitter** on every computed delay is
load-bearing rather than cosmetic: every client anchors off the *same* `newest_ts`, so
without it they would all fetch on the same second. The retry chase is **bounded** by the
lag clamp — past the plausible publication window the frame is not late, the provider has
stalled or the archive has a gap, and the schedule falls back to cadence-spaced polling
so a stall can never become a permanent 30 s poll. And the backoff ceiling is
`max(frame_interval, 10 min)` rather than a flat 30 min, because a view labelled DIRECT
may not sit half an hour behind while it waits out a shedding server.

**The forecast does not move LIVE.** Forecast frames (§13.10) are appended after the
measured ones, and everything above — the live edge, "did a frame land?", the refresh
anchor — reads the newest *measured* frame. Stepping, scrubbing or playing past it
leaves LIVE; scrubbing back onto it re-arms following. A new forecast run alone, with no
new measurement, still triggers a rebuild of the timeline.

One failure mode is worth naming because it was invisible: `fetch` *rejects* on a
network-level error rather than resolving, and the rejection used to escape the async
timeout callback — so a single offline blip killed the self-scheduling chain for the life
of the page, and only a reload brought the live view back. A rejection is now treated as
a failed fetch like any other.

### 13.2 Mandatory attribution

Non-negotiable, because the data licences require it: the active radar provider's credit
(taken verbatim from the backend `attribution` field), "© OpenStreetMap contributors",
and the Blitzortung credit whenever the lightning layer is active — all in Leaflet's
attribution control. While a forecast frame is on screen the PIAF credit joins them,
followed by the run time ("mise à jour 14:15"): Etalab's Licence Ouverte requires both
the source and the date of last update. The credit strings are the one HTML-bearing data path from backend
to DOM, which is why the CSP on the shell document is the backstop for them.

### 13.3 Internationalisation

FR and EN today; a third language is one JSON file and no code change. Three separate
concerns:

1. **UI strings** — `/static/i18n/{lang}.json` plus a tiny loader and a `t(key)` helper,
   applied to elements via a `data-i18n` attribute. Strings stay decoupled from logic and
   editable without touching code.
2. **Locale-aware formatting** — the browser's native `Intl`, zero dependencies. This is
   why the API returns raw epochs (§12.2).
3. **Detection and selection** — saved choice in `localStorage`, else `navigator.language`
   mapped to fr/en, with a visible FR|EN toggle. `<html lang>` is updated on change so
   screen readers and hyphenation follow.

Modules that build rich content in JS (the lightning legend, the About dialog's prose,
the calendar's month names) expose a `refreshI18n()` hook, because the generic
`applyTranslations()` sets `textContent` and would strip embedded links.

### 13.4 Geolocation

Static one-shot `getCurrentPosition` (never `watchPosition`), triggered only by an
explicit button press, rendering the familiar blue dot with an accuracy halo. Rationale:
a stationary weather user does not need live tracking, and keeping the GPS radio off
saves mobile battery; a button press is a clear intent signal and avoids the reflexive
deny that load-time prompts provoke.

The browser owns the permission grant and can revoke it independently, so the app
persists only its own **intent** and reconciles against the live permission state on each
load — intent "on" plus a `prompt` state shows the button rather than auto-prompting;
intent "on" plus `denied` shows it in a blocked state conveyed as text, not colour alone.
Auto-centring fires **once**, on the first successful fix; later fixes move the dot but
never the map, which would otherwise fight a user panning to inspect weather elsewhere.
Every outcome falls back to a sensible France view. Geolocation requires a secure
context, satisfied in production by Traefik and in dev by `localhost`.

### 13.5 Video export

`clip.js` composites the current map view — OSM base, radar overlay, and per-frame
lightning — across a fixed window ending at the scrubber cursor into a short silent
looping H.264 MP4, entirely in the browser via WebCodecs.

It is strictly additive and reuses **only already-loaded data**: same-origin `/tiles/…`,
the loaded strike pool, and the existing OSM base read back from the HTTP cache (the base
layer is `crossOrigin: "anonymous"` precisely so the canvas stays readable). **No new
upstream call, no server compute, no task.** It burns the mandatory attribution and a UTC
timestamp into every frame, and never draws the geolocation marker. **Forecast frames
are never exported**: `radar.getExportData()` drops them, and a cursor in the forecast
exports the window ending on the newest measurement. Were that ever to change, every
forecast frame would have to burn in a "forecast" label beside its timestamp. A failed export is a
localised toast — never an uncaught exception, never a radar or lightning impact.

### 13.6 PWA and the service worker

`sw.js` is hand-written, classic, and precaches an **explicit static shell** — HTML, JS,
CSS, vendored libraries, i18n files, icons. It is GET-only and same-origin-only, and it
**never** intercepts `/tiles/…`, `/forecast/…`, the `/api/…` JSON, the lightning SSE, or
the cross-origin OSM base. It therefore adds no upstream request of any kind; the SSE stream
in particular must stay unbuffered and uncached.

Navigations are network-first with the cached shell as fallback; `/static/` assets are
stale-while-revalidate, storing only complete direct 200s under a query-stripped key.
When shell assets change, **both** the shell list and the cache version must be bumped;
old caches are pruned on activate.

Auto-update is silent: `skipWaiting` + `clients.claim`, and the page reloads on
`controllerchange` — but only on an actual update, never on first install, and deferred
while a video export is mid-render so a half-rendered share is not killed.

The worker is served at root scope (`/sw.js` with `Service-Worker-Allowed: /`) via a dev
Django route and a production Nginx location. **The PWA is progressive enhancement**: an
absent or failed registration leaves the app fully functional.

`/robots.txt`, `/sitemap.xml` and the Open Graph card are deliberately **not** in the
shell list: only crawlers and link scrapers fetch them, out of band, so precaching them
would cost every install bytes no user ever reads.

### 13.7 Crawlability

Search engines and link scrapers see the **served bytes**, and in production those bytes
come from Nginx with no templating layer anywhere in the path. Everything below is
therefore hand-written static content under `frontend/`, which `collectstatic` copies to
the document root exactly as it does `index.html` and `sw.js`.

**One canonical URL for the app.** The shell lives at `/` and switches language at
runtime, so there is no second URL to point an `hreflang` at; `/` carries a
self-referencing canonical and nothing else. The served `<html lang>` is **`fr`**, and
the served `<title>`/`<meta name="description">` are French — the audience is France, and
a default should be the majority case rather than an accident. The FR↔EN alternate group
lives on `/apropos` ↔ `/about`, where two real URLs exist; each lists **all three**
alternates (fr, en, x-default → the French page), because a group whose members do not
each name every member, themselves included, is discarded whole.

**The document title is set twice.** `index.html` ships the long search-facing title, and
`main.js` overwrites `document.title` once the dictionary loads. The rendered DOM is what
crawlers read, so those must not be the same string: `app.title` is the short `<h1>`
label, `app.document_title` the long one. Reusing the former would silently undo the
served title.

**Unknown URLs are a real 404.** `location /` previously ended in `try_files /index.html
=404` — a `try_files` that can never fall through, so every unknown path answered `200`
with the app shell. That is a soft 404: an unbounded supply of duplicate pages for
crawlers, and it swallowed `/robots.txt` before it could exist. The app has no
client-side routing, so the shell is now bound to `location = /` with a terminal
`location / { return 404; }` after it. Django's URLconf already had this shape.

**Data paths are shielded twice.** `robots.txt` disallows `/api/`, `/tiles/`, `/metrics`
and the health endpoints — the tile archive alone is roughly 1.6 M PNGs in a full
retention window, which is pure crawl budget for nothing. `robots.txt` stops the crawl;
an `X-Robots-Tag: noindex` on those same locations stops indexing of a URL discovered
from an inbound link, which `Disallow` alone does not.

**Structured data is inline JSON-LD** (`WebApplication` on `/`, `Article` on the
explainers). An `application/ld+json` block is an HTML *data block*: the parser returns
from "prepare the script element" before the CSP inline check is ever reached, so
`script-src 'self'` does not apply and no exemption is needed. If it ever has to become
executable, delete it rather than loosen the policy.

**The canonical origin is duplicated** across `index.html`, `apropos.html`, `about.html`,
`robots.txt` and `sitemap.xml`, because none of them can be templated. That duplication
is pinned by `radar/tests/test_frontend.py`, which asserts the five agree and that the
sitemap lists exactly the three documents' canonicals.

The Open Graph card (`frontend/img/og-image.jpg`, 1200×630) is generated by
`scripts/generate_og_image.py` from the explainer's own layer images, and burns the
mandatory OSM + Météo-France attribution into the pixels — the card travels outside the
app, where Leaflet's attribution control cannot follow it.

### 13.8 One-finger zoom

`onefingerzoom.js` adds the Google Maps shortcut: double-tap, keep the finger down on
the second tap, then slide — down to zoom in, up to zoom out. It exists because the map
is a one-handed, outdoors, phone-in-the-rain tool, and pinching needs two.

Leaflet 1.9 has no such handler, so the module recognises the gesture itself, and the
design is defensive on both sides of that recognition:

- **Before the slide it only listens.** Detection needs the *second* tap's touchstart,
  at which point it is still unknown whether the finger will slide or simply lift, so
  that touch is withheld from Leaflet's drag handler (`stopPropagation`) but never
  `preventDefault`ed — an unprevented tap still synthesises the click/dblclick pair that
  Leaflet's own double-tap zoom needs. A double-tap that just lifts therefore behaves
  exactly as before.
- **After the slide it owns the sequence.** Past an 8 px threshold the move and end
  events are prevented, which both stops the page reacting and cancels the synthetic
  dblclick — otherwise `doubleClickZoom` would fire a second, competing zoom on release.
- **It never switches a Leaflet handler off.** Pan, pinch and control taps are reached
  by the events it declines to claim, not by toggling handlers — nothing to leave
  wedged if the recogniser mis-fires. A second finger settles the zoom instantly and
  hands the sequence back, so a pinch can start; a throw anywhere inside aborts the same
  way. The shortcut is expendable, the map is not. (The published plugins do disable
  `dragging` and `doubleClickZoom` for the duration and re-enable them on `zoomend` —
  which `_moveEnd` fires *only* when the zoom actually changed, so a gesture that lands
  back on its starting level leaves the map un-draggable. That is the failure this rule
  exists to make impossible.)
- **It yields to animations already running.** `_stop()` cancels pan inertia and any
  in-flight `flyTo` before the anchor is read, and the gesture declines to arm at all
  while Leaflet is playing a zoom animation — the same precaution Leaflet's own pinch
  handler takes.

One asymmetry is inherited rather than avoided, and is worth knowing before anything
starts listening for `zoomstart`: `engage()` fires `_moveStart(true, false)`
unconditionally, while the instant hand-off — `settle(false)` — goes through `_resetView`,
which fires `zoomend` only when the zoom actually changed. A gesture engaged by a purely
horizontal slide (so `delta` is exactly 0) and then interrupted by a second finger
therefore leaves a `zoomstart` unpaired. Leaflet's own `TouchZoom` has the identical
hole, and nothing in `frontend/js/` listens to `zoomstart` today, so this is faithful
mirroring rather than a defect — but it is the same shape as the plugin trap above, and
a future `zoomstart`-gated optimisation would hang on it.

**Upstream.** Core wants this too — [Leaflet #10303](https://github.com/Leaflet/Leaflet/issues/10303)
requests the identical gesture and the identical direction mapping, but it is open,
untriaged and unclaimed (no PR, no milestone) as of 2026-08, which is why the field is
still plugin territory. The two published plugins were read closely before this was
written and neither is vendored: `cherniavskii/Leaflet.DoubleTapDragZoom` engages on a
100 ms hold rather than on movement, pivots on a drifting midpoint and does not clamp
during the drag; `petoc/Leaflet.DoubleTouchDragZoom` arms on any two taps within 300 ms
regardless of their duration or distance apart, and carries the `zoomend` re-enable trap
described above. Their zoom maths agree with ours to within a few pixels per level (138
and 144 px against our 150), and `_stop()` plus the `_animatingZoom` bail were taken
from them. **Recheck #10303 whenever the vendored Leaflet is bumped** — if it lands in
core, this module goes and the core option replaces it (there is a matching note at the
top of `onefingerzoom.js`).

The zoom itself mirrors Leaflet's own pinch handler: fractional `_move()` per animation
frame while the finger is down, one snapped `_animateZoom()` on release. It pivots on
the double-tapped point (a fixed anchor, unlike pinch's moving midpoint), so the feature
under the finger stays under the finger, and it clamps to the map's own zoom range.
Vertical travel is the only input — 150 px per zoom level — and the direction sits
behind a single named constant.

### 13.9 Leaflet's zoom contract

Anything we position ourselves in **layer points** — the lightning canvas (§8.4) and the
radar pane's bbox clip mask — has to be re-projected across a zoom, not just after one.
Leaflet gives its own layers three signals and we must answer the right ones:

- **`zoom`** fires continuously through a pinch, which never animates. The map's live
  state is already correct when it fires, so it can be read directly.
- **`zoomanim`** fires once at the start of the 250 ms wheel/double-click animation —
  and *before* `_animateZoom` commits the new projection, so the map's live state is
  still the old one. The event's own `center`/`zoom` are the only correct source, via
  `map._latLngToNewLayerPoint(ll, zoom, center)`.
- **`zoomend`** is the end of the gesture, far too late to be the only handler: for the
  whole animation the tiles scale while anything wired only to `zoomend` sits frozen,
  then jumps into place.

The animation itself is CSS, so the answer has to be too: an element gets a transform,
a `clip-path` gets a transition, both on Leaflet's own `0.25s cubic-bezier(0,0,0.25,1)`
(see `vendor/leaflet.css`). Those transitions are **not** motion-gated anywhere,
because they track an animation Leaflet plays unconditionally — gating them would
reintroduce the desync rather than remove motion. Both members of this list once had
the bug and both fixes are pinned by tests; a third such element should be added here.

### 13.10 The forecast on the timeline

The user must always be able to tell whether the picture is measured or forecast. The
map itself does not say: a forecast frame is drawn **exactly like a measurement** — the
same palette, which is the legend, and the user's own opacity. A reduced opacity would
fight that setting, and a different palette or texture would change what an intensity
appears to be. The cues are elsewhere:

- **The timeline.** The forecast segment of the track has its own colour **and** a
  dashed pattern, so it does not rely on colour alone, starting at a "now" tick on the
  newest measurement. The tick is drawn as two notches above and below the track,
  because in the default LIVE state the thumb rests exactly on it.
- **The pill.** Past the tick the LIVE pill becomes "PRÉVISION / +25 min" — the lead
  time counted from the newest measurement — with `aria-pressed="false"` and a spoken
  label, and stays the way back to live on click. It sits on the map, so it is also the
  cue for someone watching the animation rather than the scrubber.
- **The time label.** "Prévision" appears under the time, and in the scrubber bubble.

The forecast exists only in the live window and only with the Météo-France source. A
toggle in the settings popover ("Afficher la prévision", **on by default**,
persisted per browser) hides it; it is shown only when the backend advertises a
forecast. The layer cache in `radar.js` is keyed by tile URL rather than timestamp,
because the same time is first forecast and then measured, with different tiles, and
one must never be served for the other.

---

## 14. Configuration and feature flags

**Configuration lives in code** — settings constants in `base.py`, overridden in
`local.py` / `production.py`. Only three categories are read from the environment:

1. **Secrets** — `DJANGO_SECRET_KEY`, `METEOFRANCE_APPLICATION_ID`, `VAPID_*`.
2. **Deployment wiring** — `DATABASE_URL`, `REDIS_URL`, `ALLOWED_HOSTS`, `DJANGO_ADMIN_URL`.
3. **Per-container role flags** — `ARCHIVER_ENABLED`, `LIGHTNING_ENABLED`, which
   legitimately differ between the web and archiver containers of the *same image*.

Everything else that differs between dev and prod belongs in the concrete settings
module, not in a `.env` file. `.envs/` follows the per-service tree convention
(`.envs/.{environment}/.{service}`); the production tree is gitignored.

| Name                               | Kind                                      | Default | Gates                                                                                                   |
| :--------------------------------- | :---------------------------------------- | :------ | :------------------------------------------------------------------------------------------------------ |
| `ARCHIVER_ENABLED`                 | Per-container role flag (env)             | off     | `run_archiver` — set true in the `archiver` service and nowhere else                                    |
| `LIGHTNING_ENABLED`                | Per-container role flag (env)             | off     | Blitzortung ingest (archiver) **and** the layer advert / SSE / history (web)                            |
| `METEOFRANCE_ENABLED`              | Per-deployment flag (settings constant)   | off     | The Météo-France provider, its poll job, and its entry in the `providers` advert                        |
| `METEOFRANCE_REFLECTIVITY_ENABLED` | Sub-flag of the above (settings constant) | off     | The REFLECTIVITE wash composited under the rain                                                         |
| `PUSH_ALERTS_ENABLED`              | Per-deployment flag (settings constant)   | off     | Subscribe/unsubscribe endpoints, the push advert, and the archiver evaluator                            |
| `FORECAST_ENABLED`                 | Per-deployment flag (settings constant)   | off     | The forecast job, the `forecast` advert's frames and `/forecast/` tiles; requires `METEOFRANCE_ENABLED` |

The `Default` column is the value in `base.py`. Concrete settings modules override it
per deployment: `production.py` turns every per-deployment flag on — both
`METEOFRANCE_*` (the provider is live, wash included), `PUSH_ALERTS_ENABLED`
(background Web Push on top of the always-on foreground alerts) and `FORECAST_ENABLED`.
The forecast stays off in dev: a run costs 13 × 3.2 MB of downloads, ~12 GB a day, and
running it locally with the production credential revokes production's token (the
`deploy` skill). Because these are
settings constants, they are not readable from the environment: putting one in
`.envs/` looks like it works and is silently ignored.

**Every flag ships dark and off means byte-identical previous behaviour.** A flag enabled
without its paired secret raises `ImproperlyConfigured` at settings import — deliberately
module-level rather than a Django system check, so it fires for Uvicorn *and* for
management commands rather than letting a misconfigured deployment start looking healthy.

---

## 15. Security

- **No third-party contact from the browser** (§1), which is also a privacy property: no
  data provider ever sees a visitor's IP.
- **SSRF defence on every server-side fetch driven by external input.** RainViewer's
  upstream-supplied tile host is honoured only if it is HTTPS within the expected domain
  suffix, and its frame paths must match a strict pattern with no `/` or `.` in the token.
  Météo-France catalog hrefs must resolve under the configured API base. Push endpoints
  must match the hostname allow-list. In each case a tampered upstream must not be able to
  steer our fetches.
- **Path-traversal defence** on tiles at both layers: a strict numeric Nginx regex, and in
  Django a date-format check, a date-matches-timestamp check, and matrix membership.
- **Secrets are never logged** — not the Météo-France application ID, not access tokens,
  not VAPID keys.
- **A strict CSP on the shell document**, viable because the app has no inline scripts or
  styles: `self` for scripts/styles/connect, images additionally from the OSM tile hosts,
  `blob:` workers for the WebCodecs muxer, `frame-ancestors 'none'`. It is the backstop
  for the one HTML-bearing data path (attribution strings).
- **Rate limiting in Nginx**: a per-IP limit on `/api/`, a server-wide circuit breaker so
  a surge of *distinct* clients still sheds cleanly with fast 429s, and a per-IP
  concurrent-connection cap on the SSE stream (each one holds an ASGI worker and a Redis
  subscription). Tiles are not under `/api/` and stay unthrottled. Real client addresses
  are recovered from `X-Forwarded-For`, trusting only private-range hops.
- **`/metrics` is public by design.** It carries only non-sensitive operational counts and
  gauges. The former allow-list was ineffective anyway: behind Traefik the peer address is
  always a private container IP.
- **HSTS, secure cookie prefixes, nosniff, DENY framing** in production; TLS is terminated
  by Traefik and the forwarded-proto header is honoured.

---

## 16. Observability

The app **instruments only**. It emits:

- **`GET /metrics`** — hand-rolled Prometheus text (no client dependency): archived frame
  counts total / 24 h / partial / failed, per-provider frame counts and last-poll epochs,
  gap totals and an open-gap flag, archive bounds, tile-directory bytes and used ratio;
  lightning strike totals / 24 h / archived rows, WS connected flag and uptime, reconnects,
  queue drops, last-strike epoch, partition count; push subscription count with
  sent / failed / pruned counters; and, when the forecast is enabled, the published
  run's reference time, publication time and lag (`forecast_run_timestamp`,
  `forecast_published_timestamp`, `forecast_publication_lag_seconds`) plus its
  not-ready / fetch-failed / rejected / bytes / runs-published counters.
- **Structured one-line JSON logs to stdout** in production, with canonical events:
  `poll_start`, `poll_complete`, `frame_archived`, `fetch_failed`, `frame_failed`,
  `gap_opened`, `gap_closed`, `retention_run`, `job_crashed`, `tile_fallback`,
  `ws_connected`, `ws_disconnected`, `queue_overflow`, `batch_written`, `batch_failed`,
  `push_sent`, `push_failed`, `push_pruned`, `push_subscribed`, `push_unsubscribed`,
  `forecast_published`, `forecast_run_failed`, `forecast_pruned`, plus the Météo-France pair
  `frame_downloaded` / `frame_rendered`.
- **Météo-France frame cost** decomposes via those last two. `poll_complete.duration_ms`
  is a whole poll — catalog GET, both product downloads, decode, render and the disk
  writes of *every* frame that poll archived — so it can never answer "what does the
  reflectivity wash cost?". `frame_downloaded` carries `download_ms` + product sizes;
  `frame_rendered` carries `parse_ms` (ODIM/HDF5), `wash_decode_ms` (BUFR),
  `rain_pyramid_ms` / `wash_pyramid_ms`, `tiles_ms` (the 62-tile loop) with
  `wash_px_ms` broken out of it, `encode_ms` (PNG) and `total_ms`. The wash's marginal
  cost is `wash_decode_ms + wash_pyramid_ms + wash_px_ms`. On the rain-only path every
  `wash_*` field is a true zero — the degraded render skips the timing itself, so
  graceful degradation stays free of even a clock read.

**No Grafana, Loki, Promtail or Prometheus containers ship in this repo.** Dashboards and
alert rules live on the host observability stack, alongside the existing Traefik. The
alert conditions this instrumentation is designed to support: a gap opened (alert), radar
fetch failed three times (warning), sustained upstream 429 throttling (warning — the
concurrency cap may need lowering, §5.3), Blitzortung disconnected for more than five
minutes (critical), and storage above 75 % of capacity. With the forecast on, a run
older than ~20 minutes (`time() - forecast_run_timestamp > 1200`) means the advert has
gone empty and is worth a warning; a rising `forecast_rejected_total` means PIAF's
payload drifted and the decoder is refusing it, as designed; a rising
`forecast_crashed_total` means a run failed on something else — a full disk, a render
error, a bug — and its `forecast_run_failed` log (`reason=crashed`) carries the traceback.

---

## 17. Environments

Split settings (`base.py` / `local.py` / `production.py`) selected via
`DJANGO_SETTINGS_MODULE`, with two complete compose files rather than a base plus
override. Per-environment Dockerfiles live under `compose/`; note that the Postgres
Dockerfile sits under `compose/production/` and is referenced by the local compose file
too — there is no separate local Postgres image.

| Aspect         | local                                         | production                                            |
| :------------- | :-------------------------------------------- | :---------------------------------------------------- |
| Django server  | `runserver` (autoreload)                      | Uvicorn (ASGI) behind Nginx behind Traefik            |
| Source code    | Bind-mounted for live edit                    | Baked into the image                                  |
| Static + tiles | Served by Django                              | Served by Nginx (tiles `immutable`, shell `no-store`) |
| TLS / routing  | None (`localhost:8000`)                       | Traefik + Let's Encrypt                               |
| Météo-France   | Enabled, so dev exercises the render pipeline | Off by default; opt-in per deployment                 |
| Push alerts    | Opt-in                                        | Off by default; opt-in per deployment                 |
| Forecast       | Off (~12 GB/day of downloads); opt-in         | Off; opt-in per deployment                            |
| Redis          | Container, ephemeral                          | Container, ephemeral (no persistence)                 |
| Env files      | `.envs/.local/*`                              | `.envs/.production/*` (gitignored)                    |

`base.py` enforces `USE_TZ = True` in all environments, so there is no DST drift between
dev and prod. Dev polls faster so the archive fills quickly, and a seed command backfills
a few hours of frames so the scrubber has playable data on a first run. A `justfile`
wraps the compose invocations.

**No Celery** — APScheduler in the archiver covers the scheduling need without a broker,
a worker pool, and a beat process. **No allauth or email flows** — the app has no user
accounts.

---

## 18. Testing

The suite runs **dockerized only**, against containerized Postgres and Redis, for
reproducibility. External HTTP is mocked with `respx`, so the suite needs no internet.
Coverage must stay **≥ 85 %** on `radar/`, and `ruff check` must stay clean.

Logic is deliberately kept in importable async functions with the framework bootstrap
separated out — poll and retention live in `radar/archiver.py` while APScheduler wiring
lives in the management command; ingest and the batch writer are plain coroutines driven
in tests by a fake source and an in-memory queue. That separation is what makes the
resilience behaviour (backoff, queue overflow, gap hysteresis, partial frames) testable
without real waits or real sockets.

Several tests are load-bearing pins rather than incidental coverage, and should not be
"fixed" to match a code change without understanding what they protect: the 62-tile
matrix count, the single-flight guarantee (62 concurrent tile calls must produce one
download), the linear-Z-vs-dB smoothing split, the BUFR orientation and floor
fixtures, the PIAF GRIB fixtures and their validation (a wrong run or valid time, a
non-GRIB 200 and a drifted template are all rejected), and the forecast advert's
staleness rules.

---

## 19. Deployment

Deploys are **tag-triggered CD, not run by hand**. The full procedure — cutting a
version, rollbacks, the manual bring-up and recovery fallback, the service topology, and
how to enable the dark feature flags — lives in the `deploy` skill
([`../.claude/skills/deploy/SKILL.md`](../.claude/skills/deploy/SKILL.md)).

The one constraint that belongs here rather than in a runbook: **migrations must be
backward-compatible (expand/contract)**, because they run against the live database
before old containers are swapped out.

---

## 20. Deliberately not done

- **`watchPosition` / live position tracking** — a stationary weather user does not need
  it, and it keeps the GPS radio active.
- **User accounts** — nothing in the product needs identity. Push subscriptions are keyed
  by endpoint URL, not by a user.
- **Grid data exposed through the API** — geophysical arrays exist only transiently inside
  the one provider that produces them (§5.1).
- **A build step for the frontend** — see §2.
- **Observability containers in this repo** — see §16.
- **Redis persistence** — see §11.
- **Our own nowcasting** (`pysteps` optical flow, or a learned model) — PIAF is the same
  method blended with a model, for free; see [`forecast-nowcast.md`](forecast-nowcast.md) §1.
- **The forecast in the archive or the video export** — past runs are not kept, and an
  exported clip carries no on-map "forecast" cue (§5.6, §13.5).
