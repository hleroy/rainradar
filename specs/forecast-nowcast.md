# 1-hour rain forecast: research and implementation plan

**Status:** proposal. Nothing here is implemented.
**Raised:** 2026-11-11.
**Kick-off prompt:** [`forecast-kickoff-prompt.md`](forecast-kickoff-prompt.md).
**Related:** design doc [§4.1 failure domains, §5 providers, §6.2 on-disk layout,
§12 API contract, §13.1 What LIVE means](rain-lightning-radar-design.md).

Rain Radar shows measurements only. This document records the research into adding a
short forecast (the next hour), the decision to take it from Météo-France's PIAF product
rather than compute it ourselves, and a phased plan. Every fact below is tagged with how
it was established, because several load-bearing ones are still unverified — Phase 0
exists to close them before any design is frozen.

---

## 1. Decision

**Fetch Météo-France PIAF; do not run our own extrapolation.**

PIAF (*Prévision Immédiate Agrégée Fusionnée*) is a 5-minute, ~1 km, 0–3 h rain forecast
that Météo-France publishes for free on the same API portal we already use. For the first
hour it *is* radar extrapolation — the method we would have implemented — blended with the
AROME-PI model so that growth, decay and relief are partly accounted for. Taking it means
no forecasting algorithm to maintain, no new heavy dependency (`pysteps`), and a cadence
that already matches `METEOFRANCE_FRAME_INTERVAL`.

The fallback, if Phase 0 shows PIAF is unusable (format, latency, quota, subscription),
is plain optical-flow extrapolation of our own Météo-France rain grid with `pysteps`
(§2.1). It is strictly worse on quality and cost; it is recorded so the option is not
re-researched.

## 2. Research

### 2.1 What extrapolation can and cannot do at one hour

Radar extrapolation ("Lagrangian persistence"): estimate a motion field from the last few
frames (optical flow), then advect the latest frame forward. It moves existing rain; it
cannot create or dissolve it.

| Finding | Source |
| :-- | :-- |
| Best method (pysteps deterministic) decorrelates after ~25 min for 1 h events, 40 min for 3 h, 56 min for 6 h, 116 min for 24 h events (1,533 events, Netherlands) | Imhoff et al. 2020, *WRR* |
| Winter stratiform errors are 3× lower than summer convective errors | same |
| Most error is attributed to growth and dissipation, which extrapolation ignores | same |
| Downwind areas have 2× the skillful lead time of areas near the upwind domain edge | same |
| Light rain (0.1 mm/h) stays accurate to ~3 h; at 1 mm/h reliability drops beyond 1 h | pysteps-related slide deck (indicative) |
| Simple optical flow is comparable or superior to DWD's operational nowcaster | Ayzel et al. 2019, *GMD* (rainymotion) |
| A CNN (RainNet) beats optical flow to 60 min at 0.125–5 mm/h but loses at 10–15 mm/h, because it blurs detail below ~16 km | Ayzel et al. 2020, *GMD* |
| Generative models (DGMR, NowcastNet) are preferred by expert forecasters, but need years of training data and GPUs | Ravuri et al. 2021, Zhang et al. 2023, *Nature* |

Consequences for the product, whatever the source:

- **Trust decays inside the hour.** Roughly 30 min in summer storms, close to the full
  hour in frontal rain. The UI must present later frames as less certain.
- **Deep learning is out of scope.** Our 90-day archive is far too short to train on and
  there is no GPU.

### 2.2 PIAF — what it is

From the official product sheet (*Descriptif PIAF-Lame d'eau*, Météo-France, 2021-03-09,
attached to the API page) unless noted.

- Deterministic rain forecast over metropolitan France, 1 km (API: 0.01°), lead times
  5 → 180 min in 5-min steps, a new run every 5 min. Quantity: rain accumulated over each
  5-min step (`RR5MN`) — the same quantity as our `LAME_D_EAU` observation.
- `PIAF = α · extrapolation + (1 − α) · AROME-PI`. The extrapolation advects the PANTHERE
  radar mosaic with a motion field derived from the reflectivity mosaics at t0 and
  t0 − 10 min. AROME-PI outputs are 15-min and interpolated to 5-min.
- α is recomputed every 5 min over a sliding 6 h window (Gerrity score), **separately for
  6 geographic sub-zones**.
- **The first hour is mostly extrapolation**: the blend typically switches to the model
  after 45–90 min, over 10–25 min.
- Radar quality below a 74 % threshold ⇒ AROME-PI alone.
- AROME-PI lands ~30 min after each round hour; until then PIAF uses the previous run.
- **Documented visual artefacts:** an abrupt switch between predictors; discontinuities at
  sub-zone borders when zones switch at different times; flicker in animation once
  AROME-PI dominates (the 15 → 5 min interpolation).

### 2.3 PIAF — how it is served

From the portal's API metadata (`PrevisionImmediatePrecipitations` v1.0, fetched
2026-11-11) and the generic user guide *API Ciblée Modèles* (Confluence, updated
2025-12-10). The guide's examples are all AROME; nothing in it is PIAF-specific.

| Item | Value | Confidence |
| :-- | :-- | :-- |
| Base URL | `https://api.meteofrance.fr/pro/piaf/1.0` | portal metadata |
| Price / quota | free; 50 requests/min | portal metadata |
| Auth | OAuth2 bearer token (also API key — we stay OAuth2-only) | portal + guide |
| Raw data | WCS 2.0.1 `GetCapabilities` / `DescribeCoverage` / `GetCoverage` → GRIB2 (`application/wmo-grib`) or GeoTIFF (`image/tiff`) | portal metadata |
| Images | WMS 1.3.0 `GetMap`, PNG, EPSG:4326 only | portal metadata |
| Service names | `MF-NWP-HIGHRES-PIAF-001-FRANCE-WCS` / `-WMS` | portal metadata |
| Layer | `TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE` | portal metadata (WMS example) |
| Granularity | one run, one lead time, one 2D field per request | guide |
| Coverage ID | `PARAM__LEVEL___RUN[_PERIOD]`, run as `2025-02-04T00.00.00Z` (dots), period e.g. `_PT1H` for accumulations | guide (AROME examples) |
| Subsetting | `subset=time(<seconds or ISO>)`, `subset=lat(a,b)`, `subset=long(a,b)` | guide |
| GRIB shape | GRIB2, one message, `regular_ll`, `grid_simple` packing | guide (AROME example) |
| Retention | 5 days, real time only, no archive | guide |
| Errors | **403 = data not yet on the backend** (not an auth failure); 404 = unknown coverage or inconsistent time/level; 400 = bad parameter | guide |
| Licence | Etalab Licence Ouverte 2.0; **must mention the source and the date of last update** | licence document on the API page |

Cost of a 1-hour forecast: one `GetCapabilities` plus 12 `GetCoverage` per run, i.e.
~2.6 requests/min against a 50/min quota.

### 2.4 What is still unverified (Phase 0)

1. **Subscription and token.** Whether our existing application's OAuth2 token works on
   `/pro/piaf` once subscribed, and whether `METEOFRANCE_TOKEN_URL` is unchanged.
2. **Publication latency.** The guide only says "au plus tôt". If a run lands 6 min late,
   its first lead time is already in the past. This decides how much of the hour is real.
3. **The exact coverage ID** (parameter name, period suffix) and the PIAF grid
   (extent, dimensions, row order).
4. **GRIB details.** Packing, units (mm per 5 min vs mm/h), and how "no data" is encoded
   versus "no rain" — our renderer's `nodata`/`undetect` distinction depends on it.
5. **`GetCapabilities` size and cost**, since it is the only way to discover a new run.
6. **Whether the forecast lines up with the observation.** PIAF's lead-time-0 neighbour
   (our latest `LAME_D_EAU` frame) should match in position and intensity; a visible jump
   at the observed/forecast boundary would need handling.

## 3. Constraints from this codebase

- **Forecast is a new, fifth failure domain.** A PIAF outage, a 403 storm, a decode error
  or a slow render must never touch the RainViewer poll, the Météo-France observation
  poll, lightning or alerts. Its own scheduler job, its own flag, its own exception
  boundary.
- **It is not a `RadarProvider`.** That protocol means "past frames, idempotently
  archived for 90 days". Forecast frames are superseded every 5 minutes. Forcing them
  through the provider would entangle the archive's gap accounting, `RadarFrame` rows and
  retention with data that is disposable by design. Mirror the lightning seam instead: a
  small `ForecastSource` protocol under a new `radar/forecast/` package.
- **Forecast tiles must not live under `/tiles/…`.** That tree is served `immutable` and
  keyed by frame timestamp. A forecast for valid time T changes with every run, so its
  URL must carry the run: `/forecast/{run_ts}/{valid_ts}/{z}/{x}/{y}.png` is unique per
  (run, valid) and can then be `immutable` too. A separate root on disk keeps the
  janitor, the storage gauge maths and `radar:{provider}:archived` untouched.
- **The browser never calls Météo-France.** Same rule as every other source: backend
  fetch, our own paths only. That also rules out handing WMS URLs to Leaflet.
- **A different host from the radar API.** `METEOFRANCE_API_BASE_URL` is
  `public-api.meteofrance.fr/public/DPRadar`; PIAF is `api.meteofrance.fr/pro/piaf/1.0`.
  It needs its own settings constant. We build PIAF URLs ourselves, but the coverage ID
  comes from upstream XML — validate it against a strict pattern before interpolating it
  into a URL.
- **Reuse, don't fork, the render path.** `meteofrance_render` already turns a grid of
  5-min accumulations into the 62-tile matrix with the signed-off rain LUT. The forecast
  should render through the same LUT so observed and forecast frames read as one picture.
  PIAF is a regular lat/lon grid, not the observation's projected grid; whether `GridGeo`
  accommodates that or needs a small generalisation is a Phase 1 finding.
- **No new heavy dependency without a spike.** There is no GRIB reader in the tree, and
  BUFR was hand-decoded with strict template validation rather than adding ecCodes. The
  candidates are: GeoTIFF via the Pillow we already ship; a small hand-written GRIB2
  simple-packing decoder in the style of `bufr_decode`; or `eccodes` wheels. Decide on
  real bytes in Phase 0.
- **The forecast never drives an alert**, and never appears in copy as a measurement.
- **Never log `METEOFRANCE_APPLICATION_ID` or tokens** — unchanged.
- **Settings constants, not env.** `FORECAST_ENABLED` and friends live in `base.py` /
  `production.py`; the feature ships dark and requires `METEOFRANCE_ENABLED`.
- **Service worker and Nginx.** `sw.js` must not intercept `/forecast/…`; prod Nginx needs
  an explicit `location` for it (the terminal `location /` 404s everything else).

## 4. Proposed design (to be confirmed after Phase 0)

**Backend**

- `radar/forecast/` — `base.py` (`ForecastSource` protocol, `ForecastUnavailable`),
  `piaf.py` (capabilities → newest run → 12 downloads → decode → render), decode module.
  Reuses `MeteoFranceAuth` and `meteofrance_render`.
- One archiver job, `poll_forecast`, `max_instances=1`, started only when
  `FORECAST_ENABLED and METEOFRANCE_ENABLED`. Per run: skip if already rendered
  (idempotent, same as archiving); download the lead times up to
  `FORECAST_HORIZON_SECONDS` (default 3600) concurrently under a small semaphore; render;
  write tiles atomically via `storage.py`; only then publish the run pointer.
- **Atomic run switch.** Redis holds one small key, the current run
  (`{run_ts, valid_ts[]}`), set after every tile of that run is on disk. A half-written
  run is never advertised. Reconstructible, so no persistence change.
- **Retention.** Keep the last 2–3 runs on disk so a client mid-animation never loses its
  tiles; prune older runs in the same job. Budget: 12 frames × 62 tiles × ~20 KiB ≈ 15 MB
  per run.
- **Staleness.** The advert drops any valid time ≤ the newest observed frame, and the
  whole forecast if the run is older than a threshold (~20 min). No forecast is better
  than a stale one presented as current.
- **API.** An additive `forecast` object in `/api/radar/frames`, like the `lightning`
  advert: `{enabled, run, frames: [{timestamp}], attribution, tile_base}`. Old clients
  ignore it. Timestamps stay raw epochs (§12.2).
- **Tiles.** `GET /forecast/{run_ts}/{valid_ts}/{z}/{x}/{y}.png`, Nginx-static,
  `immutable`. No upstream fallback in Django: a miss is a 404/204, never a fetch.
- **Observability.** Metrics for last successful run age, fetch failures, 403-not-ready
  count, publication lag; canonical JSON log events alongside the existing ones.

**Frontend**

- The timeline grows a forecast segment to the right of "now", visually distinct
  (different track style and a "Prévision" / "Forecast" label), with later frames
  de-emphasised to reflect §2.1.
- **LIVE keeps its meaning** (§13.1): the newest *observed* frame. Playing or scrubbing
  into the forecast leaves LIVE; the refresh anchor still derives from observed frames.
- Lightning shows nothing for future cursor positions.
- Attribution control gains the Météo-France forecast credit **with the run time**
  whenever a forecast frame is displayed — the licence requires the update date.
- i18n strings in both locales; `STATIC_SHELL` + `CACHE_VERSION` bumped if shell assets
  change.

## 5. Open product decisions

Each has a recommended default so work is not blocked; confirm or override.

1. **Which radar source shows the forecast?** Recommended: Météo-France source only in
   v1. RainViewer tiles use a different palette, so the observed → forecast boundary
   would change colour scale mid-animation.
2. **Horizon.** Recommended: 60 min, as a setting. PIAF goes to 180 min, but past the
   switch to AROME-PI the documented flicker and zone seams appear.
3. **Opt-in or default?** Recommended: a toggle, off by default for the first release.
4. **Video export.** Recommended: exclude forecast frames in v1; if included later, every
   forecast frame must burn in a "forecast" label next to the UTC timestamp.
5. **Historical mode.** Recommended: forecast only in the live window. Past runs are not
   kept, so there is nothing to show for an archived date.

## 6. Phases

**Phase 0 — spike (no production code).** Subscribe the application to the PIAF API on
the portal (a manual step for the maintainer). Then, with a throwaway script: obtain a
token, call `GetCapabilities`, download one lead time as GRIB and as GeoTIFF, and answer
every item in §2.4. Measure publication latency over ~1 hour of runs. Deliver: findings
appended to this document, a decode recommendation, sample files as test fixtures, and a
go / no-go on PIAF versus the `pysteps` fallback. **Stop here for review.**

**Phase 1 — decode and render.** The decode module with strict validation (centre,
template, grid, units — drift raises instead of mis-decoding), the lat/lon grid through
`meteofrance_render`, and tests against the Phase 0 fixtures. Pure functions, no network.

**Phase 2 — fetch, store, schedule.** `ForecastSource` + PIAF client (respx-mocked),
storage layout, atomic run switch, pruning, the archiver job behind `FORECAST_ENABLED`,
metrics and logs. Tests for: 403-not-ready is a retry not a failure; a failing forecast
job leaves both radar polls and lightning untouched; a half-rendered run is never
advertised; re-running a completed run fetches nothing.

**Phase 3 — API and serving.** The `forecast` advert, the Django dev route and prod Nginx
`location`, staleness rules, `sw.js` exclusion. Contract tests.

**Phase 4 — frontend.** Timeline segment, LIVE semantics, attribution with run time,
toggle, i18n, export behaviour per decision 4.

**Phase 5 — docs and rollout.** Update the design doc (fifth failure domain, §6.2, §12,
§13, §14), `CLAUDE.md` non-negotiables, README, and the `deploy` skill's dark-flag
section. Ship dark; enable in production as a separate, deliberate step.

Each phase is one PR with a Conventional Commit title, CI green, coverage ≥ 85 %.

## 7. Sources

- Imhoff et al. 2020, *Spatial and temporal evaluation of radar rainfall nowcasting
  techniques on 1,533 events*, WRR — <https://www.knmi.nl/research/publications/spatial-and-temporal-evaluation-of-radar-rainfall-nowcasting-techniques-on-1-533-events>
- Pulkkinen et al. 2019, *Pysteps v1.0*, GMD — <https://gmd.copernicus.org/articles/12/4185/2019/>
- Ayzel et al. 2019, *rainymotion v0.1*, GMD — <https://gmd.copernicus.org/articles/12/1387/2019/>
- Ayzel et al. 2020, *RainNet v1.0*, GMD — <https://gmd.copernicus.org/articles/13/2631/2020/>
- Ravuri et al. 2021, *Skilful precipitation nowcasting using deep generative models of
  radar*, Nature — <https://pmc.ncbi.nlm.nih.gov/articles/PMC8481123>
- Zhang et al. 2023, *NowcastNet*, Nature — <https://pmc.ncbi.nlm.nih.gov/articles/PMC10356617>
- PIAF API page — <https://portail-api.meteofrance.fr/web/fr/api/PrevisionImmediatePrecipitations>
  (product sheet and licence are attached under "Documentation" / "Licence")
- User guide *API Ciblée Modèles* — <https://confluence-meteofrance.atlassian.net/wiki/spaces/OpenDataMeteoFrance/pages/854032416>
- Etalab Licence Ouverte 2.0 — <https://www.etalab.gouv.fr/wp-content/uploads/2017/04/ETALAB-Licence-Ouverte-v2.0.pdf>
