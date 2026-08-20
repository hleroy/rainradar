# CLAUDE.md

Guidance for working in this repo. Read this first.

## What this is

Rain Radar — a live precipitation radar webapp for France (OpenStreetMap base +
animated radar overlay, a live ~2h replay timeline, a **90-day on-disk archive**,
and a **live + historical lightning layer**).
**License: AGPL-3.0-or-later.**

For what's shipped and how the pieces fit, read [`README.md`](README.md) and the
design document below. The sections that follow cover only what the code can't tell
you: the invariants, the rationale, and the workflow.

## Source of truth

[`specs/rain-lightning-radar-design.md`](specs/rain-lightning-radar-design.md) is the
architecture and design reference: the subsystem breakdown, the failure-domain rules,
the data model, the API contract, and the reasoning behind each. Read it before
changing anything structural. Where it and the code disagree, the code is what runs —
fix the document in the same PR.

## Layout

`config/` is the Django project; `radar/` is the single app (`providers/`,
`lightning/`, and `alerts/` are the three data-source / failure-domain isolation
boundaries — see Non-negotiables); `frontend/` is vanilla ES2020 + vendored Leaflet
with no build step; `compose/` holds the Dockerfiles and nginx/postgres config.
`.claude/` holds the agent skills plus the SessionStart hook that provisions the
real 3.14 toolchain for Claude Code on the web (see Commands).

## Commands

`just` wraps docker compose (`COMPOSE_FILE` defaults to local) — run `just --list`
for the recipes. Or run the dev stack directly with
`docker compose -f docker-compose.local.yml up`, then open <http://localhost:8000>.

Tests and lint run **dockerized only** — never on the host, for reproducibility:

```bash
just pytest                                                              # full suite + coverage
docker compose -f docker-compose.local.yml run --rm django ruff check .
docker compose -f docker-compose.local.yml run --rm django ruff format .
```

These run inside the django container against the containerized Postgres + Redis.
External HTTP is mocked with `respx`, so the suite needs no internet. Coverage
target is **≥85%** on `radar/`.

### Python 3.14 only — read this before "fixing" a SyntaxError

`requires-python = "==3.14.*"`, and every environment that runs the code (both
Dockerfiles, CI, pre-commit) is on 3.14. Ruff formats to **PEP 758**, so

```python
except ValueError, TypeError:      # correct here — parentheses are optional in 3.14
```

is **valid, formatter-canonical code**, not a mistake. Any interpreter below 3.14
rejects it with `SyntaxError: multiple exception types must be parenthesized`, and
`ruff format` will re-strip the parentheses if you add them back. Adding them is
never the fix. This has already cost one session a bogus repair commit and a false
"main is broken" report — the giveaway was that CI was green the whole time.

So, in an environment whose ambient Python is older (Claude Code on the web ships
3.11):

- **Never** conclude the repo is broken from a host-Python parse. Use the 3.14
  interpreter the SessionStart hook provisions (`$RAINRADAR_PYTHON`, or
  `uv python install 3.14`), or check inside the container.
- **Never** run whatever `ruff` is on `PATH` — a different version invents findings
  (`RUF100` against preview rules it doesn't enable). Use the pin:
  `uvx $RAINRADAR_RUFF check .` (`ruff@0.16.1`, from `pyproject.toml`).
- Docker is unavailable in a web session, so the suite genuinely cannot run there.
  A clean parse and a clean `ruff check` are **not** a green suite — CI is the gate.

## Non-negotiables (don't regress these)

- **The browser must NEVER call `tilecache.rainviewer.com`, the Météo-France API,
  or Blitzortung directly.** All radar tiles (both providers) are archived/proxied by
  the backend and served from `/tiles/{provider}/{date}/{ts}/{z}/{x}/{y}.png`
  (Nginx-static in prod, Django fallback for misses); the Météo-France API base + token
  endpoint are backend-only; lightning reaches the browser only via our SSE + history
  endpoints. The frontend only ever references our own paths.
- **Provider abstraction:** views/cache never know which radar provider is active. New
  radar sources go behind `RadarProvider` (`radar/providers/`). Météo-France is a
  **fourth failure domain** — one grid fetch per frame, **two when
  `METEOFRANCE_REFLECTIVITY_ENABLED`** (rain + reflectivity, concurrent, both inside the
  same per-`ts` single-flight memo; the 62-concurrent-calls ⇒ N-downloads regression test
  is load-bearing), and its poll/auth/render failures can never touch the RainViewer poll,
  lightning, or alerts. The 5-min cadence flows from `provider.frame_interval` — no new
  hardcoded intervals. **Never log `METEOFRANCE_APPLICATION_ID` or access tokens**;
  catalog product hrefs are validated against the API base (SSRF).
- **Reflectivity is averaged in linear Z, never in dB.** The REFLECTIVITE wash
  (`METEOFRANCE_REFLECTIVITY_ENABLED`, off by default) renders *under* the rain into one
  flattened tile. dBZ is logarithmic and the mosaic's no-echo floor is -40, so blurring
  it directly drags every echo toward that floor — a lone 20 dBZ cell lands at -36 dBZ at
  its own centre and vanishes. `meteofrance_render._smooth` converts to linear Z, blurs,
  converts back; `Lut.log_domain` is what selects that, and the rain LUT must stay
  `False` (mm/h is already linear). A regression test pins both halves. The wash is
  best-effort: any reflectivity failure degrades to **byte-identical rain-only output**
  rather than losing the frame, and `bufr_decode` validates the message template, centre
  and local table version so upstream drift raises instead of mis-decoding. Note the
  signed-off palette reuses the rain LUT's own warm low stops, so the composite reads as
  one continuous ramp — never describe the wash in UI copy as a distinct measurement, and
  never let it drive an alert.
- **The tile matrix is computed from `RADAR_BBOX`** (62 tiles, zoom 3–7 for the
  default France incl. Corsica bbox). Never hardcode the list — a test asserts the
  count is 62; the Météo-France render iterates `radar.tiles`, never a hardcoded list.
- **Tiles live on disk, not Redis.** `storage.py` owns the provider-scoped layout
  (`{TILE_ROOT}/{provider}/{UTC-date}/{ts}/{z}/{x}/{y}.png`) and the atomic temp+rename
  write.
  Archiving is **idempotent** (on-disk tiles are skipped, so a re-run only retries
  gaps) — don't regress that.
- **Single scheduler / single WS consumer.** APScheduler **and** the Blitzortung
  ingester run **only** in the `archiver` container (gated by `ARCHIVER_ENABLED` /
  `LIGHTNING_ENABLED`, set there and nowhere else). **Never scale `archiver` beyond
  1 replica** — duplicate schedulers/WS connections cause duplicate fetches/strikes.
- **Lightning is a separate failure domain.** A WS drop, parse error, DB hiccup, or
  queue overflow in lightning must **never** block or crash the radar poll. Keep the
  three radar↔lightning seams thin (the `/api/radar/frames` `lightning` advert, the
  janitor's partition-drop clause, `run_archiver` starting the ingest tasks).
- **All archive time math is UTC** and explicit (`time.gmtime`,
  `datetime.now(tz=UTC)`), independent of Django's display `TIME_ZONE`. The tile
  date directory is the **UTC** date of the frame timestamp.
- **Mandatory attribution stays visible:** RainViewer credit (from the backend
  `attribution` field) + “© OpenStreetMap contributors”, plus the Blitzortung
  credit (`lightning.attribution`) whenever the lightning layer is active — all in
  Leaflet's attribution control. RainViewer's free terms and Blitzortung's
  non-commercial community terms require it.
- **The video export is client-side and additive.** `frontend/js/clip.js` runs
  entirely in the browser via WebCodecs/`mediabunny`; it **reuses only
  already-loaded tiles/strikes** (same-origin `/tiles/…`, the loaded lightning pool,
  the existing OSM base) — **no** new upstream call, no server compute, no task. It
  **burns the mandatory attribution + a UTC timestamp into every frame** and never
  draws the geolocation marker. A failed export is a localized toast — never a
  radar/lightning impact, never an uncaught exception.
- **The foreground storm-alert path is entirely client-side.**
  `frontend/js/alerts.js` opens its **own** SSE connection to
  `/api/lightning/stream` and evaluates each strike against a user-chosen anchor —
  **independent of the lightning layer's visibility toggle and the radar cursor**.
  It is a **third failure domain**: it never reaches into `lightning.js` and a WS
  drop, parse error, or Notification API failure can only toast or skip a strike,
  never touching radar or the lightning layer. It is **hidden** without the backend
  lightning advert, requests notification permission only inside the confirm gesture,
  is **never** drawn in the video export (like the geolocation marker). The foreground
  path adds **no** backend endpoint or upstream call.
- **Background storm alerts (Web Push) are a third failure domain,
  archiver-only, flag-gated.** The push evaluator (`radar/alerts/`) runs **only** in
  the single-replica archiver (gated by `PUSH_ALERTS_ENABLED` + `LIGHTNING_ENABLED`) —
  **never scale it** (duplicate evaluators ⇒ duplicate notifications). It consumes the
  existing `lightning:strikes` Redis pub/sub channel **downstream** of ingest
  (`ingest.py`/`fanout_strikes` are untouched); a crash there can never affect radar or
  lightning, and `run_archiver`'s `_supervise` restarts it. `radar/alerts/webpush.py` is
  the **only** module importing `pywebpush` (synchronous → always `to_thread` +
  semaphore + timeout). Stored push endpoints are **hostname-allow-listed** (SSRF), the
  anchor is **coarsened to ~1 km** before storage, unsubscribe **deletes** the row, and
  the daily janitor prunes stale rows — all flag-gated so `PUSH_ALERTS_ENABLED` off
  leaves alerts foreground-only. VAPID keys are self-generated (no vendor accounts);
  enabled-but-unset ⇒ fail fast. The `sw.js` `push` handler shows the server-rendered
  strings verbatim; the fetch handler stays untouched.
- **The service worker caches only the static shell.** `frontend/sw.js` precaches
  the explicit `STATIC_SHELL` (HTML, JS, CSS, vendored libs, i18n, icons) and is
  GET-only / same-origin-only — it **never** intercepts `/tiles/…`, the `/api/…`
  JSON, the lightning SSE (`/api/lightning/stream` stays unbuffered & uncached), or
  the cross-origin OSM base, so it adds **no** upstream request. When shell assets
  change, bump **both** `STATIC_SHELL` and `CACHE_VERSION` (old caches pruned on
  `activate`). It's served at root scope (`/sw.js`, `Service-Worker-Allowed: /`) via
  a dev Django route in `config/urls.py` + a prod Nginx `location`; auto-update is
  silent (`skipWaiting` + `clients.claim`, reload on `controllerchange`, never on
  first install, deferred while a video export renders). The PWA is **progressive
  enhancement** — an absent or failed SW leaves the app fully functional, just
  without install or offline support.
- **Provider/source abstraction:** views/cache never know which provider or
  lightning source is active. New radar sources go behind `RadarProvider`
  (`radar/providers/`); new lightning sources behind `LightningSource`
  (`radar/lightning/`).
- **The canonical origin `https://rainradar.hleroy.com` is hardcoded in five
  files** — `index.html`, `apropos.html`, `about.html`, `robots.txt`,
  `sitemap.xml` — because production serves all of them as plain static files
  with no templating layer. A test pins that they agree and that the sitemap lists
  exactly the three documents' canonicals; **change the domain in all five or not
  at all**. Related invariants (see design doc §13.7): the app has **one** URL, so
  `/` carries a self-referencing canonical and **no** `hreflang` — the FR↔EN group
  belongs to `/apropos` ↔ `/about`, where each page must list all three alternates
  including itself. `index.html` ships the long search-facing `<title>`, and
  `main.js` overwrites it from **`app.document_title`**, never `app.title` — the
  rendered DOM is what crawlers read. In prod Nginx the shell is `location = /`
  with a terminal `location / { return 404; }` after it; **never** restore a
  `location /` catch-all `try_files /index.html`, which can't fall through and
  turns every unknown URL into a 200 soft-404.
- **Coverage ≥85% on `radar/`**; keep `ruff check` clean.

## Git workflow

- **Never commit directly to `main`.** Every change — feature or fix — starts on a
  new branch off the latest `main` (`git switch -c <type>/<short-desc>`, matching the
  Conventional Commit type, e.g. `feat/video-export`).
- **Open a PR** for the branch and let CI run. **Verify the CI results are green**
  (dockerized test suite, ruff, coverage ≥85%) before considering the work mergeable;
  fix and push until CI passes.
- **The PR title MUST be a Conventional Commit** (`type: summary`). We squash-merge,
  so the PR title — not your local commit messages — becomes the squashed commit's
  subject on `main` (GitHub appends `(#NN)`). A separate CI check
  (`.github/workflows/pr-title.yml`) **fails the PR until the title is valid**; this
  is the gate the local `commit-msg` hook can't be, because that hook only sees
  individual local commits and never runs at all for PRs opened from Claude Code on
  the web. **When you open or rename a PR, give it a `type:`-prefixed title.**
- **Land via squash + rebase onto `main`** — squash the branch's commits into one
  Conventional-Commit-titled commit and rebase it onto `main` (no merge commits).
  Keep `main` linear.

## Conventions

- **Commit messages — and PR titles — follow
  [Conventional Commits](https://www.conventionalcommits.org)** — `type: summary`,
  with `type` one of `feat, fix, refactor, chore, docs, test, style, perf, ci, build,
  revert`. Two enforcement layers, kept in sync: locally a `commit-msg` pre-commit
  hook (`conventional-pre-commit`) checks each commit; in CI, `pr-title.yml` checks
  the PR title (the subject that actually lands via squash-merge). Keep both type
  lists identical when you change them. Enable the local hooks once with
  `pre-commit install --hook-type commit-msg --hook-type pre-push`.
- Views are `async def` and call only the provider/source interfaces, `radar.cache`,
  the archive models, and `radar.storage` — never a specific upstream.
- Config lives in settings constants (base/local/production), not in env. Only
  secrets (`DJANGO_SECRET_KEY`, `METEOFRANCE_APPLICATION_ID`, `VAPID_*`), deployment
  wiring (`DATABASE_URL`, `REDIS_URL`, `ALLOWED_HOSTS`), and the two per-container
  role flags (`ARCHIVER_ENABLED`, `LIGHTNING_ENABLED`) are read from the environment.
  `.envs/*` contains only those; per-environment differences belong in local.py /
  production.py. Production env files are gitignored.
- Redis holds only small, reconstructible state (no tile bytes — disk is the tile
  store), so RDB/AOF persistence stays **off**. Keys are provider-namespaced where
  provider-specific: `radar:{provider}:frames_json` (TTL), `radar:{provider}:archived`
  (SET, no TTL), `radar:range_json`, the storage/last-poll gauges, and the
  `lightning:*` pub/sub channel + recent buffer + counters. See `radar/cache.py`.
- The async Redis client (and the upstream-tile concurrency semaphore) are rebuilt
  when the running event loop changes — needed because WSGI `runserver` uses a fresh
  loop per request (uvicorn uses one). See `radar/cache.py` and
  `radar/providers/rainviewer.py`.

## Production notes

Deploys are **tag-triggered CD, not run by hand** — the full procedure (what
"deploy" means as an instruction, rollbacks, the manual recovery fallback, the
service topology, and enabling the dark `METEOFRANCE_*` / `PUSH_ALERTS_ENABLED`
flags) lives in the `deploy` skill: [`.claude/skills/deploy/SKILL.md`](.claude/skills/deploy/SKILL.md).
Migrations must be **backward-compatible (expand/contract)** — they run against the
live DB before old containers are swapped.

## Observability

The app **instruments only** — it emits a Prometheus-text `/metrics` endpoint
(`radar/metrics.py`, **public by design** — it carries only non-sensitive
operational counts/gauges, and is Redis-cached against scrape/DB load) and structured
JSON logs to stdout (`radar/logging_json.py`), ready to be scraped by an external
Prometheus/Loki stack. **No Grafana/Loki/Promtail/Prometheus containers ship in this
repo** — the dashboards and the alert rules (gap opened, fetch-failed
3×, sustained 429, Blitzortung down >5 min, 75%-storage) live on the host
observability stack (alongside the existing Traefik), not here. See the README's
**Observability** section for the metric series and canonical log events.
