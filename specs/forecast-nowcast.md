# 1-hour rain forecast: PIAF research and findings

**Status:** shipped dark behind `FORECAST_ENABLED`; enabling it in production is a
separate, deliberate step (the `deploy` skill). **The design that shipped is
[design doc §5.6](rain-lightning-radar-design.md)** (with §4.1, §6.2, §12, §13.10).
**Raised:** 2026-11-11.

This document is the record behind that design: why the forecast comes from
Météo-France's PIAF rather than our own extrapolation (§1–§2), the constraints and
product decisions it was built to (§3, §5), and the Phase 0 measurements that fixed
its numbers (§8). The code cites it by section number, so **section numbers are
stable** — §4 and §6, whose plan the design doc now supersedes, are kept as short
stubs. Every fact is tagged with how it was established.

---

## 1. Decision

**Fetch Météo-France PIAF; do not run our own extrapolation.**

PIAF (*Prévision Immédiate Agrégée Fusionnée*) is a 5-minute, ~1 km, 0–3 h rain forecast
that Météo-France publishes for free on the same API portal we already use. For the first
hour it *is* radar extrapolation — the method we would have implemented — blended with the
AROME-PI model so that growth, decay and relief are partly accounted for. Taking it means
no forecasting algorithm to maintain, no new heavy dependency (`pysteps`), and a cadence
that already matches `METEOFRANCE_FRAME_INTERVAL`.

The fallback, had Phase 0 shown PIAF unusable (format, latency, quota, subscription),
was plain optical-flow extrapolation of our own Météo-France rain grid with `pysteps`
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
  5 → 180 min in 5-min steps (the API actually serves 5 → 195 min, §8.3), a new run every 5 min. Quantity: rain accumulated over each
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
**Corrected with the Phase 0 measurements (§8)**; the confidence column says which rows
were measured.

| Item                 | Value                                                                                                                                                                                                              | Confidence                                             |
| :------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----------------------------------------------------- |
| Base URL             | `https://api.meteofrance.fr/pro/piaf/1.0`. `GetCapabilities` advertises operation hrefs on `https://public-api.meteofrance.fr/pro/piaf/wcs/…` (another host, no version): build URLs ourselves, never follow those | **measured** (Phase 0)                                 |
| Price / quota        | free; 50 requests/min. No rate-limit headers are returned; the spike peaked at ~6 req/min and was never throttled                                                                                                  | portal metadata; quota not exercised                   |
| Auth                 | OAuth2 bearer token from our existing application and `METEOFRANCE_TOKEN_URL`; one token serves DPRadar and PIAF. **Minting a token usually revokes the application's previous one** within ~2–3 s (§8.1)          | **measured**                                           |
| Raw data             | WCS 2.0.1 `GetCapabilities` / `DescribeCoverage` / `GetCoverage` → GRIB2 (`application/wmo-grib`) or GeoTIFF (`image/tiff`); the two carry bit-identical values                                                    | **measured**                                           |
| Images               | WMS 1.3.0 `GetMap`, PNG, EPSG:4326 only                                                                                                                                                                            | portal metadata; used once in the spike for thumbnails |
| Service names        | `MF-NWP-HIGHRES-PIAF-001-FRANCE-WCS` / `-WMS`                                                                                                                                                                      | **measured**                                           |
| Parameter            | exactly one, `TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE` — despite the name, an **accumulation in mm** (§8.4)                                                                                              | **measured**                                           |
| Periods              | five coverages per run: `_PT5M`, `_PT15M`, `_PT30M`, `_PT1H`, `_PT3H`. We need `_PT5M`                                                                                                                             | **measured**                                           |
| Coverage ID          | `TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE___2026-11-11T11.10.00Z_PT5M` — three underscores before the run, run with dots, period suffix always present                                                    | **measured**                                           |
| Runs                 | one every 5 min, none missing in 4.5 days                                                                                                                                                                          | **measured**                                           |
| Lead times (`_PT5M`) | **39**: 300 … 11 700 s, i.e. 5 → **195** min (the service title says 180)                                                                                                                                          | **measured**                                           |
| Grid                 | regular lat/lon 0.01°, 1651 × 1051 cell centres, lon −6 → 10.5, lat 51.5 → 41, row 0 = north. Cropped to `RADAR_BBOX`: 1571 × 1031                                                                                 | **measured**                                           |
| Granularity          | one run, one lead time, one 2D field per request                                                                                                                                                                   | **measured**                                           |
| Subsetting           | `subset=time(300)` and `subset=time(2026-11-11T11:15:00Z)` return identical bytes; `subset=lat(a,b)` / `subset=long(a,b)` are inclusive of both edges                                                              | **measured**                                           |
| GRIB shape           | GRIB2, one message, templates 3.0 (`regular_ll`), 4.8 (statistically processed), 5.0 (simple packing, 16 bit), **no bitmap**; 3.24 MB per `RADAR_BBOX` frame                                                       | **measured**                                           |
| Retention            | every run since 00:00 UTC four days back (1287–1299 runs listed); the oldest run did not move during the session, so whole days are probably purged at once                                                        | **measured** (purge rule inferred)                     |
| Errors               | unpublished run → **404** `NoSuchCoverage` (131 of 131 probes); **403 never seen** except for the missing subscription (`900908`); revoked token → 401 `900901`; once, a **200 carrying the wrong payload** (§8.5) | **measured**                                           |
| Licence              | Etalab Licence Ouverte 2.0; **must mention the source and the date of last update**                                                                                                                                | licence document on the API page                       |

Cost of a 1-hour forecast, as measured: 13 `GetCoverage` per run (leads 10–70 min, §8.5)
plus two or three 203-byte probes for the next run, i.e. ~3.2 requests/min against a
50/min quota — but **42 MB per run, ~12 GB per day**, because the server does not
compress (§8.9).

## 3. Constraints from this codebase

As written before Phase 0; §8.11 lists what the measurements changed.

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

## 4. Design

Superseded by [design doc §5.6](rain-lightning-radar-design.md), which records what
shipped; the pre-Phase-0 proposal that stood here is in git history. Two of its rules
are cited from the code and stand, as corrected by §8.11:

- **Retention.** The newest `FORECAST_RETAIN_RUNS` (3) complete runs stay on disk, so a
  client mid-animation never loses its tiles; the job prunes everything else every tick.
  A run of 13 frames is ~10–16 MB of tiles.
- **Staleness.** The advert drops valid times at or before the newest observed frame and
  past it + `FORECAST_HORIZON_SECONDS`, and the whole run if it is older than
  `FORECAST_MAX_RUN_AGE` (20 min: a run lands ~9 min old, so this tolerates exactly one
  missed run) or would leave a hole after the observation. No forecast is better than a
  stale one presented as current.

## 5. Product decisions

Decided by the maintainer on 2026-11-11.

1. **Which radar source shows the forecast?** The Météo-France source only. RainViewer
   tiles use a different palette, so the observed → forecast boundary would change colour
   scale mid-animation.
2. **Horizon.** 60 min, as a setting. PIAF goes to 180 min, but past the switch to
   AROME-PI the documented flicker and zone seams appear.
3. **Toggle.** A user toggle, **on by default**.
4. **Video export.** Forecast frames are excluded. If that ever changes, every forecast
   frame must burn in a "forecast" label next to the UTC timestamp.
5. **Historical mode.** Forecast only in the live window. Past runs are not kept, so
   there is nothing to show for an archived date.
6. **Radar coverage.** Decided 2026-11-11, after Phase 0. The forecast is shown only
   where the newest observed frame has radar coverage. PIAF fills the areas the radars
   do not reach (Bay of Biscay, northern Spain, open sea — about 11 % of `RADAR_BBOX`)
   with model rain; that rain is hidden, so the forecast never shows rain where the
   measurement cannot. The mask is the newest `LAME_D_EAU` frame's `nodata` cells (§8.4,
   §8.6).

### 5.1 Telling measurement from forecast

Decided by the maintainer on 2026-11-11.

The requirement: the user must always be able to tell whether the picture is measured or
forecast. Two cues:

- **Timeline.** The forecast segment has its own colour *and* a pattern (hatched or
  dashed, so it does not rely on colour alone), and a "now" tick marks the boundary at
  the newest observed frame.
- **State label.** The slot that shows the LIVE pill shows a forecast pill with the lead
  time ("PRÉVISION +25 min") whenever the cursor is past the tick, and the timestamp
  label carries the same word. Exposed to screen readers like the LIVE state. Because the
  pill sits on the map, it is also the cue for someone watching the animation rather
  than the scrubber.

**The forecast is drawn on the map exactly like a measurement.** Two things were
considered and rejected:

- **Reduced opacity on the forecast layer.** Radar opacity is already a user setting; a
  second, automatic opacity would fight it. The forecast layer uses the user's opacity
  unchanged.
- **A different palette, saturation or texture baked into forecast tiles.** The palette
  is the legend, so altering it changes what an intensity appears to be, and a baked-in
  style cannot be tuned without re-rendering.

## 6. Phases

Phase 0 was a spike on the live API (§8). Phases 1–5 — decode and render; fetch,
store and schedule; API and serving; frontend; docs — then landed together as one PR.
The design doc describes the result.

The tests Phase 2 was required to have, as corrected by §8.11: a 404 for the expected
run is a retry, not a failure; a failing forecast job leaves both radar polls and
lightning untouched; a half-rendered run is never advertised; re-running a completed
run fetches nothing; a 200 with the wrong payload is rejected and not rendered; a 401
invalidates the shared token and retries once.

Live check, 2026-11-11 14:04 UTC: run 13:55Z published 544 s after its reference time,
13 frames, 42.1 MB downloaded, 656 tiles / 16 MB on disk, 3.4 s end to end.

**Still open:** the frontend was checked headlessly and in desktop Chromium, not on a
real phone; and the flag is not yet on in production.

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

---

## 8. Phase 0 findings

**Measured:** 2026-11-11, 11:08–12:50 UTC, against the live API with our existing
application (subscribed to PIAF that morning). Throwaway scripts ran in the django
container (Python 3.14, local settings); they and the samples live in the gitignored
`_meteofrance_probe/piaf_spike/`. About 390 PIAF requests in total, never more than ~6 a minute. No
credential or token was printed or written; the request log keeps URL, status, size and
timing only. Rain was widespread that day (convective cells in the north-east, a line
over the Ligurian Sea), which suits Q4 and Q6.

§8.1–§8.6 answer the six questions the plan started with (Q1–Q6); §2.3 above is
corrected in place.

### 8.1 Subscription and token (Q1)

- The OAuth2 token from `MeteoFranceAuth`, minted at the unchanged `METEOFRANCE_TOKEN_URL`
  with `METEOFRANCE_APPLICATION_ID`, works on `/pro/piaf/1.0` once the application is
  subscribed. **No PIAF-specific token or credential is needed**: one application token
  serves both APIs. Before the subscription the gateway answered
  `403 {"code":"900908", … "API Subscription validation failed."}` — the only 403 seen all
  session.
- **Minting a token usually revokes the application's previous token**, within ~2–3 s,
  on PIAF *and* DPRadar alike (`401 {"code":"900901","message":"Invalid Credentials"}`).
  Six controlled pairs of mints: the older token died in four. That first broke the spike
  itself: a second `MeteoFranceAuth` instance, inside the same process, knocked out the
  first one. With one shared instance, ~75 minutes and ~300 requests produced zero 401s.
- Consequence for the design: the forecast client must use **the same `MeteoFranceAuth`
  instance** as the observation provider in the archiver, and its 401 → invalidate →
  retry-once path is load-bearing, not an edge case. The invalidation must name the
  rejected token (`invalidate(stale)`, a no-op once a newer one is cached): with several
  leads in flight, a late 401 that cleared whatever was cached would re-mint over the
  token an earlier 401 had just minted, revoking it. Two related points, both outside
  this spike's remit: the web and archiver containers each mint their own token today,
  so they presumably already revoke each other's and lean on that retry; and if the local
  credential is the production application, the spike's own mints will have done the
  same to production's token.

### 8.2 `GetCapabilities` (Q2)

- One XML document of **1.98–2.01 MB**, growing ~1.4 KB per run; median response 0.5 s,
  slowest 14.8 s.
- It lists **every run since 00:00 UTC four days back** — 1287 runs at 11:20 UTC, one
  every 5 min, none missing — and **five coverages per run**, all of the single parameter
  `TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE`: accumulation periods `_PT5M`,
  `_PT15M`, `_PT30M`, `_PT1H`, `_PT3H`. We need `_PT5M`. Exact ID:
  `TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE___2026-11-11T11.10.00Z_PT5M`.
- Its `OperationsMetadata` advertises `https://public-api.meteofrance.fr/pro/piaf/wcs/…`
  — a different host, without `/1.0` — as the operation URLs. We build URLs ourselves and
  must not follow those.
- Discovery does **not** need this document. A run is downloadable 10–30 s *before* it is
  listed (§8.5), runs are on a fixed 5-minute clock, and a coverage that does not exist
  yet answers 404. Probing the next expected coverage ID directly (a `GetCoverage` with a
  0.05° subset returns 203 bytes) is cheaper — polling this document once a minute would
  download 2.9 GB a day just to learn the run time — and it also removes the need to
  parse coverage IDs out of upstream XML. Keep `GetCapabilities` for a cold start or
  after a long outage, if at all.

### 8.3 `DescribeCoverage` (Q3)

- `_PT5M`: a `ReferenceableGridCoverage`, 3-D (long, lat, time). Spatial grid **1651 ×
  1051 cell centres at 0.01°**, lon −6 → 10.5, lat 51.5 → 41; origin (−6, 51.5) with a
  −0.01 latitude offset, i.e. **row 0 is the north edge** — the convention
  `meteofrance_render` already uses. `RADAR_BBOX` (41.2–51.5 °N, −6–9.7 °E) is inside it;
  cropped, the grid is 1571 × 1031.
- **39 lead times**, `300 … 11700` s — 5 to **195** min, not the 180 the service title
  and the product sheet give. The envelope runs from run + 5 min to run + 195 min. (`_PT1H`
  has 28 lead times, 3600 … 11700 s.)
- Range: `uom kg m-2` with definition GRIB2 `0-1-7` (precipitation *rate*); native format
  GRIB. The unit is right, the definition is not, and the GRIB message disagrees with both
  (§8.4).

### 8.4 One lead time as GRIB and as GeoTIFF (Q4)

Run 11:10Z, lead 5 min, cropped to `RADAR_BBOX`:

|                              | GRIB2 (`application/wmo-grib`)                                                                                                     | GeoTIFF (`image/tiff`)                                                                                       |
| :--------------------------- | :--------------------------------------------------------------------------------------------------------------------------------- | :----------------------------------------------------------------------------------------------------------- |
| Size, `RADAR_BBOX` crop      | 3 239 605 B (full grid: 3 470 605 B)                                                                                               | 12 977 510 B (4×)                                                                                            |
| gzip -9 / server compression | 234 KB / none (no `Content-Encoding`, although httpx asks for gzip)                                                                | 333 KB / none                                                                                                |
| Encoding                     | one message; template 5.0 simple packing, 16 bit, `R = 0`, `D = 0`, `E` chosen **per response** (−12 … −16 seen)                   | GDAL's conversion of the same GRIB: float64, uncompressed, one strip per row, **strips stored out of order** |
| Georeferencing               | template 3.0, sphere R = 6 371 229 m, `La1 = 51.5`, `Lo1 = 354.0` (0–360: −6°), `Di = Dj = 0.01`, scan mode 0 (W→E, row 0 = north) | tie point (−6, 51.5) at pixel centre (`PixelIsPoint`), 0.01° scale                                           |
| Values                       | mm per 5 min, value = `X · 2^E`                                                                                                    | bit-identical to the GRIB                                                                                    |
| Unit metadata                | category 1 / **parameter 52 = precipitation rate, kg m⁻² s⁻¹** — wrong for the values                                              | GDAL: `TPRATE [kg/(m^2*s)]` — inherits the same error                                                        |
| No-data                      | **no bitmap** (indicator 255): every cell is a number ≥ 0                                                                          | no `GDAL_NODATA` tag, no NaN                                                                                 |
| Readable with what we ship   | numpy, ~100 lines                                                                                                                  | **no** — Pillow raises `UnidentifiedImageError` (no 64-bit float mode)                                       |

**Units: millimetres accumulated over the 5 minutes ending at the valid time** — the same
quantity as `LAME_D_EAU`, despite three labels saying otherwise (the coverage name "RATE",
GRIB parameter 52 "precipitation rate, kg m⁻² s⁻¹", `DescribeCoverage`'s `0-1-7`). The
evidence: template 4.8 declares an accumulation (statistical process 1) over 5 minutes
ending at `end_of_interval`; against the observation with the same window the domain mean
ratio is 1.00–1.10 and the wet-cell percentiles agree within 5 % (p50 0.60 vs 0.60 mm/h,
p99 15.4 vs 14.5 mm/h); and the smallest non-zero value is 0.0100 mm, LAME_D_EAU's 1/100 mm
quantum. Read as kg m⁻² s⁻¹ the values would be 14 000 mm/h. **A decoder must pin these
GRIB numbers and convert ×12, as `rate_field` does — it must not believe the labels.**

The time fields need the same care: in the GRIB, `forecastTime` is the *start* of the
window (lead − 5 min, in minutes) and the valid time is `end_of_interval`. Run 11:15Z,
lead 60 carries `forecast_time=55`, `end_of_interval=12:15`.

**"No radar coverage" is not encoded at all.** There is no bitmap, no NaN, no sentinel:
every cell is a number ≥ 0. Outside the radar network PIAF carries AROME-PI's model rain:
at lead 5, 33 % of the cells that `LAME_D_EAU` marks `nodata` are wet in PIAF, against
10 % of covered cells. `rate_field`'s `nodata`/`undetect` distinction therefore cannot be
derived from PIAF. The decision to keep the observation's no-coverage area blank (§5,
item 6) therefore takes its mask from the newest `LAME_D_EAU` frame's `nodata` cells.

The packing exponent `E` follows the requested field's maximum: the same dry field packed
at `E = −15` over the bbox and `E = −16` over a small box; the 11:25Z run needed `E = −12`
for its 9 mm (108 mm/h) Ligurian cell. A 16-bit word at `E = −12` tops out at 16 mm per
5 min. Every sample had 16 bits, including a 98.7 %-dry one; a constant field
(`nbits = 0`, legal GRIB2) was never seen but must be handled.

### 8.5 Publication latency (Q5)

76 minutes of polling in two stretches (11:29–12:20 and 12:21–12:46; the first one crashed
on the response described below). Every 20 s the poller probed the next expected run with
a tiny `GetCoverage`; every 60 s it fetched `GetCapabilities` and the `LAME_D_EAU` catalog.
Seconds are counted from the run's reference time; resolution is 20 s for "available" and
60 s for "listed".

| Run (UTC)          |        Data available | Listed in `GetCapabilities` | Last lead (195 min) available |  404s before available |
| :----------------- | --------------------: | --------------------------: | ----------------------------: | ---------------------: |
| 11:25              |                 529 s |                       562 s |                         564 s |                     11 |
| 11:30              |                 530 s |                       550 s |                         554 s |                     14 |
| 11:35              |                 530 s |                       561 s |                         564 s |                     14 |
| 11:40              |                 563 s |                       561 s |                         572 s |                     15 |
| 11:45              |                 559 s |                       557 s |                         572 s |                     15 |
| 11:50              |                 531 s |                       551 s |                         555 s |                     14 |
| 11:55              |                 581 s |                       604 s |                         607 s |                      2 |
| 12:00              |                 558 s |                       556 s |                         564 s |                     12 |
| 12:05              |                 558 s |                       555 s |                         564 s |                     15 |
| 12:10              |                 547 s |                       545 s |                         563 s |                     15 |
| 12:15              |                 544 s |                       573 s |                         576 s |                      8 |
| 12:20              |                 545 s |                       575 s |                         577 s |                     14 |
| 12:25              |                 545 s |                       573 s |                         577 s |                     14 |
| 12:30              |                 545 s |                       571 s |                         574 s |                     14 |
| 12:35              |                 545 s |                       568 s |                         571 s |                     14 |
| **range / median** | **529–581 s / 545 s** |       **545–604 s / 561 s** |                 **554–607 s** | **191 × 404, 0 × 403** |

- **A run is downloadable ~9 minutes after its reference time** — 529–581 s, median 545 s,
  very little jitter — and all 39 lead times appear together, within one probe.
- **"Not yet available" is 404, not 403.** All 191 probes of an unpublished run returned
  `404` with a WCS `ExceptionReport`, `exceptionCode="NoSuchCoverage"` (the body names an
  internal ID, `PRECIP__GROUND___…`). The guide's "403 = not yet on the backend" never
  happened, so a client cannot tell "not yet" from "wrong ID" by status alone: it has to
  know which run it expects.
- **Once, a 200 carried the wrong payload.** One of 74 `GetCapabilities` calls (12:20 UTC)
  answered `200`, `Content-Type: application/wmo-grib`, 3 432 177 bytes, after 7 s — a
  GRIB coverage instead of XML, and not one we had requested (its size matches none of
  ours). Status codes alone cannot be trusted; every response must be checked for type
  and, for GRIB, that its reference time and `end_of_interval` are the run and valid time
  we asked for.
- `LAME_D_EAU` landed 123–305 s after its validity time over the same window, mostly
  ~130–145 s (60 s poll resolution; production measures 140–217 s). One product download failed once
  (`TileUpstreamError`) and succeeded on the next poll.

**What this means for the hour.** With the observation ~2.5 min and PIAF ~9 min behind
real time, when run R lands the newest observation is R+5 for most of the run's
5-minute life and R+10 for the rest. Lead 5 is therefore always in the past and lead 10 often is. To show
60 minutes past the newest observation, a run must supply **leads 10 to 70 min — 13
`GetCoverage` per run**, not 12 from lead 5.

### 8.6 Does the forecast line up with the observation? (Q6)

Run 11:15Z, each lead compared with the `LAME_D_EAU` frame covering the same 5 minutes,
on PIAF's 0.01° grid, inside radar coverage. "Persistence" is the newest observation on
screen when the run is published (11:20, R+5) held still — what the map shows today. CSI
is the critical success index (overlap ÷ union of the areas above the threshold). At +5
persistence *is* the observation, hence no figure. One run on one wet day: indicative.

| Lead | Valid (UTC) | CSI ≥ 0.1 mm/h PIAF / persistence | CSI ≥ 1 mm/h | CSI ≥ 5 mm/h | Mean PIAF / obs | Shift   |
| ---: | :---------- | :-------------------------------: | :----------: | :----------: | --------------: | :------ |
|   +5 | 11:20       |             0.74 / —              |   0.68 / —   |   0.58 / —   |            1.00 | 0       |
|  +10 | 11:25       |            0.69 / 0.75            | 0.63 / 0.63  | 0.52 / 0.48  |            1.04 | 0       |
|  +15 | 11:30       |            0.64 / 0.64            | 0.57 / 0.48  | 0.39 / 0.26  |            1.12 | 0       |
|  +30 | 11:45       |            0.56 / 0.50            | 0.47 / 0.34  | 0.32 / 0.19  |            1.08 | 0       |
|  +45 | 12:00       |            0.51 / 0.42            | 0.40 / 0.25  | 0.23 / 0.14  |            1.20 | ~1 km   |
|  +60 | 12:15       |            0.46 / 0.37            | 0.35 / 0.21  | 0.16 / 0.08  |            1.23 | ~2.5 km |

- **Position matches:** cross-correlation finds no offset up to +30 min and 1–2.5 km at
  +45/+60 — small next to the rain cells themselves.
- **Intensity matches** at the start (mean ratio 1.00) and drifts up to ~1.2 × by +60 min
  — one case, so indicative only.
- **Skill:** at ≥ 1 mm/h PIAF equals persistence at +10 and beats it from +15 on, by a
  widening margin (0.35 vs 0.21 at +60). Consistent with §2.1: useful for the hour,
  clearly degrading inside it.
- **The observed → forecast seam is small.** From the newest observation (R+5) to the
  first forecast frame (R+10) the overlap at 0.1 / 1 / 5 mm/h is 0.67 / 0.55 / 0.43,
  against 0.75 / 0.63 / 0.48 between two consecutive observations; domain mean 0.156 →
  0.155 mm/h. A slightly bigger step than a normal frame change, not a jump.
- **The coverage mask is the visible seam.** At R+10, 58 495 of the 175 308 cells where the
  observation has no coverage (Bay of Biscay, Spain, Italy, open sea) carry ≥ 0.1 mm/h of
  model rain in PIAF. Drawn as-is, rain would *appear* there the moment the cursor crosses
  into the forecast.

Images (`_meteofrance_probe/piaf_spike/data/q6/`, obs | PIAF | ≥ 0.5 mm/h agreement):
`run1120_lead5_vs_obs1125.png`, `boundary_obs1120_vs_run1115_lead10.png`,
`run1115_lead60_vs_obs1215.png`.

### 8.7 Through the existing render path

Not one of the six questions, but cheap to settle on real bytes. A PIAF field fits
`meteofrance_render`'s `GridGeo` as it is: `projdef="+proj=longlat +R=6371229 +no_defs"`,
`xscale = yscale = 0.01`, and the upper-left *edge* at (−6.005, 51.505) — half a cell out
from the first GRIB grid point, an easy off-by-half to get wrong. `transformer_for` is
then an identity transform. All 62 tiles rendered in 0.29 s; 56 were non-empty, 769 KB
in total, on a wet day.

*Corrected in Phase 1:* the spike's "renders unchanged" was wrong in one place.
`mip_level` compared the Web-Mercator pixel size in metres with `xscale`, which is in
degrees here, so every zoom picked the coarsest pyramid level — a z7 forecast drawn from
~9 km cells. The spike's z5 check could not show it. Phase 1 compares a geographic grid
in longitude instead (every Web-Mercator pixel spans 360 / (256·2^z) degrees of it);
projected grids are untouched. With that, `GridGeo` needs no generalisation, but the
half-cell origin and the absent `nodata` live in the decoder.

### 8.8 Decode recommendation: a hand-written GRIB2 decoder

**Recommended: a small strict decoder in the style of `bufr_decode.py`, with no new
dependency.**

- **The payload makes it trivial.** One message, a handful of fixed-layout sections, simple packing, 16 bits,
  no bitmap: the data section is a big-endian `uint16` array, so decoding is
  `np.frombuffer(section7, ">u2") * 2.0**E` — **2.3 ms** for the whole bbox, bit-identical
  to a general bit-unpacking decode. There is nothing like BUFR's replicated-descriptor
  walk to optimise; the spike's section walker is ~100 lines.
- **The validation is needed whatever the library.** The service once returned the wrong
  payload with a 200 and its unit metadata is wrong in three places, so the decoder must
  pin: edition 2, discipline 0, centre 84, template 3.0 with the expected `Ni`, `Nj`,
  `La1`, `Lo1` (0–360), `La2`, `Lo2`, `Di`, `Dj`, scan mode 0 and shape of earth 6;
  template 4.8 with category 1, parameter 52, process type 2, one time range of
  statistical process 1 over 5 minutes; reference time equal to the requested run and
  `end_of_interval` equal to the requested valid time; template 5.0 with `R`, `E`, `D`
  read from each message (and `nbits = 0` handled as a constant field); bitmap indicator
  255; total length and `7777` consistent. ecCodes would decode the bytes but none of
  those checks.
- **GeoTIFF loses on every axis.** Four times the bytes (13 MB per frame, ~48 GB a day at
  13 frames per run), float64, strips out of order, and **Pillow cannot read it**
  (`UnidentifiedImageError`) — it would need `tifffile` or a hand-written TIFF reader
  anyway, while dropping the GRIB identification the validation relies on.
- **ecCodes is not worth it.** `eccodes` 2.50.0 is a pure-Python cffi binding; the native
  code comes from `eccodeslib` 2.50.0.34 (cp314 `manylinux_2_28` x86_64 and aarch64
  wheels, 9.0 MB) plus `eckitlib` 2.5.0.34 (36.2 MB), plus `cffi` and `findlibs` — ~45 MB
  of native libraries to decode a uint16 array. The current wheels were uploaded on
  2026-11-06, inside the 7-day Dependabot cooldown, and the binding finds the library at
  run time through `findlibs` rather than declaring it as a dependency.

### 8.9 Go / no-go

**Go for PIAF.** It works with our credential at no cost; runs arrive on a regular clock
~9 minutes after their reference time with little jitter; the field is the same quantity,
on a grid that renders through our pipeline unchanged; it lines up with `LAME_D_EAU`; and
it beats persistence from +15 minutes. The `pysteps` fallback is not needed.

Two costs are bigger than §2.3 assumed, and both belong in the Phase 2 design rather than
counting against the decision:

- **Bandwidth.** The server does not compress, so a run of 13 lead times is 42 MB, about
  **12 GB per day** — roughly 20× what the rain-observation poll downloads. `pysteps` would cost no
  bandwidth at all but more CPU, a heavy dependency and worse skill. If the volume
  matters, fetching every second run (a 10-minute forecast refresh) halves it.
- **The coverage mask** (§8.6) — since decided: masked to the newest observation's
  coverage (§5, item 6).

### 8.10 Test fixtures for Phase 1

Real responses from this spike, all lead 5, all `_PT5M`. The first three are committed
in `radar/tests/fixtures/`; the alternative was not needed.

| File                                                          |                   Bytes | What it is                                                                                    | Exercises                                                |
| :------------------------------------------------------------ | ----------------------: | :-------------------------------------------------------------------------------------------- | :------------------------------------------------------- |
| `piaf_rain_small_20261111T1125Z_lead5.grib2`                  |                  45 805 | run 11:25Z, +5 min, 42.75–44.25 °N × 8.0–9.5 °E (151 × 151), 38 % wet, max 9.02 mm (108 mm/h) | decode, `E = −12`, intense values, positive `Lo1`        |
| `piaf_dry_small_20261108T1200Z_lead5.grib2`                   |                  45 805 | run 2026-11-08 12:00Z, same box, 97 % zero, max 0.99 mm                                       | near-dry field, `E = −16` (exponent picked per response) |
| `piaf_dry_bbox_20261108T1200Z_lead5.grib2.gz`                 |  25 696 (3 239 605 raw) | same run, full `RADAR_BBOX` (1571 × 1031), 98.7 % zero                                        | exact bbox geometry, `Lo1 = 354` wrap, gzip              |
| *alternative:* `piaf_rain_bbox_20261111T1125Z_lead5.grib2.gz` | 221 686 (3 239 605 raw) | run 11:25Z, full `RADAR_BBOX`, rainy                                                          | a full-frame render test with real rain                  |

The small box is a `subset=lat(42.75,44.25)` / `long(8.0,9.5)` request, not a local crop;
it is cell-for-cell identical to the same crop of the bbox response. No completely dry
5-minute field exists in the 4.5-day retention window: the driest (2026-11-08 12:00Z,
found through WMS thumbnails) still has 1.3 % of the bbox wet, mostly AROME-PI drizzle.

### 8.11 What these findings change in §3–§6

- **§2.2 / §2.3** — 195-minute lead times, not 180; the corrections are in the table
  above.
- **§3 "A different host…"** — still true. The coverage ID no longer needs to come from
  upstream XML at all if discovery probes the run clock (§8.2). If `GetCapabilities` is
  kept, the strict ID pattern stands, and its advertised operation URLs must be ignored.
- **§3 "Reuse, don't fork, the render path"** — confirmed and narrower than feared: no
  `GridGeo` change (§8.7). But PIAF has **no `nodata`**, which the clause did not foresee.
- **§3 / §4 "Reuses `MeteoFranceAuth`"** — must be the *same instance* as the observation
  provider's in the archiver; two instances revoke each other's tokens (§8.1).
- **§3 decode candidates** — decided: hand-written GRIB2 (§8.8).
- **§4 "capabilities → newest run → 12 downloads"** — becomes: probe the next expected run
  on the 5-minute clock (404 = not yet), then **13 downloads, leads 10–70 min**.
  `GetCapabilities` (2 MB) only on a cold start, if at all.
- **§4 "download the lead times up to `FORECAST_HORIZON_SECONDS`"** — the horizon has to
  count from the newest *observed* frame, not from the run time: leads up to horizon + 10
  min.
- **§4 "Staleness … ~20 min"** — a run is already ~9 min old when it lands and ~14 min old
  when the next one is due, so 20 min tolerates exactly one missed run. Fine, but set
  it against the measured 545 s.
- **§4 "Retention" budget** — measured 769 KB of tiles per frame on a wet day, so ~10 MB
  per run of 13 frames, under the 15 MB estimate.
- **§4 Observability** — "403-not-ready count" becomes a 404 count. Add a counter for
  rejected payloads (wrong type, wrong run or valid time) and a gauge for bytes downloaded.
- **§4 / Phase 2** — new: validate every response's type and identity before rendering
  (§8.5); handle 401 by invalidate + one retry with the shared auth.
- **§5.1 "drawn on the map exactly like a measurement"** — unchanged, but it raised a
  question the decisions did not cover: outside radar coverage the observation is blank
  and PIAF is not. **Decided: the forecast is masked to the newest observation's
  coverage** (§5, item 6). The render step therefore takes that observation's `nodata`
  mask as an input.
- **§4 Attribution** — the "date of last update" the licence requires is the run's
  reference time, which every GRIB carries in section 1.
- **§6 Phase 1** — fixtures as in §8.10; the validation list in §8.8.
- **§6 Phase 2** — the test "403-not-ready is a retry, not a failure" becomes "404
  `NoSuchCoverage` for the expected next run is a retry"; add "a 200 with the wrong
  payload is rejected and not rendered" and "a 401 invalidates the shared token and
  retries once".
