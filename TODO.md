# TODO

Dated follow-ups that are neither issues nor spec material. Check dates when
touching the repo; delete entries once done.

- **Open (raised 2026-08-20):** act on the first real load measurement of
  production. `scripts/tile_bench.py` ramped v1.0.1 over the WAN (~76k requests, zero
  5xx): Nginx served **≥1,458 req/s** on the static tile path while the **Django tile
  fallback plateaued at ~700 req/s, reached at only 24 concurrent connections** — and
  both pools collapsed at 504 (p99.9 of 7 s / 11 s, client-side timeouts), because
  `/tiles/…` has none of the `limit_req` / circuit-breaker protection `/api/` has.
  The ranked plan, the numbers, the rejected options and a verified Nginx prototype are
  in [`specs/tile-serving-performance.md`](specs/tile-serving-performance.md). In short:
  **(R6)** re-measure from inside the compose network first — 34.7 MB/s ≈ 278 Mbit/s
  says the static tier was probably bandwidth-bound, so 1,458 is a floor, not a ceiling;
  **(R1)** let Nginx answer "nothing to draw" itself, via a zero-byte `.complete`
  sentinel the archiver writes next to `cache.add_archived` — that removes essentially
  all of historical navigation from Python, and is the only item here that changes the
  architecture; **(R2)** put a `limit_conn` on `@django_tiles` so overload sheds as fast
  429s; **(R4)** `open_file_cache` on the static path; **(R5)** palette-quantise the
  Météo-France PNGs (currently full RGBA, `optimize=False`, ~23.8 KB/tile) — which also
  bears on the storage-rate entry above. Delete this entry when the spec is either
  implemented or consciously dropped.

  Measured while writing it, and worth knowing independently of any of the above:
  Django's `MiddlewareMixin.__acall__` wraps every sync `process_request` /
  `process_response` in `sync_to_async(thread_sensitive=True)`, and our eight production
  middlewares define **13** of them. That is **~1.0 ms per request** — ~17% of the
  fallback's ~5.9 ms CPU budget — spent before any of our code runs, and it does **not**
  parallelise (300 concurrent requests in one event loop yield the same ~900 req/s as
  running them serially). Seven of those 13 hops belong to `Session` / `Csrf` /
  `Authentication` / `Messages`, which exist only for the Django admin — so the
  undecided admin entry below is now also a small performance decision (spec §3.3).

- **After 2026-08-16:** measure the real tile-storage rate now that Météo-France
  archives in production (enabled in v0.0.18, 2026-08-02) and decide whether
  anything needs doing. Baseline to compare against: RainViewer alone held 1.6 GB
  for 2026-06-22 → 2026-08-02 (~38 MB/day; ~4.3 KB/tile across 144 frames/day ×
  62 tiles), plateauing near 3.4 GB at `RETENTION_DAYS=90`. Météo-France runs at
  half the frame interval (288 frames/day) into the same 62 tiles, and the
  REFLECTIVITE wash makes each PNG denser — so the estimate is 77–150 MB/day, but
  the per-tile size of a composited tile is the unmeasured term. Two weeks of real
  data settles it. This is informational, not a capacity risk: Docker's
  `data-root` is `/data/docker` on the host, so `production_radar_tiles` sits on
  a 213 GB filesystem with ~135 GB free (the host's 77%-full `/` is the OS disk
  and holds no tiles). Even the high estimate plateaus around 17 GB. Delete this
  entry once the measured rate is known and unsurprising. Note the lever if the
  answer *is* surprising: Météo-France tiles are encoded as full RGBA with
  `optimize=False` and measured ~23.8 KB each, so palette quantisation would cut
  both the archive and the wire — `specs/tile-serving-performance.md` §3.5 has a
  measurement script.

- **After 2026-08-18:** remove the legacy tile-URL compatibility layer that
  shipped with provider-scoped tile paths — the `/tiles/{date}/{ts}/…` Django
  route alias (`config/urls.py`), the Nginx `^/tiles/(\d{4}-…)` →
  `/tiles/rainviewer/$1` rewrite (`compose/nginx/`), and the
  rainviewer legacy-path dual-read in the tile view (`radar/views.py`).
  By then every SW-cached client has auto-updated to the provider-scoped
  URLs and the archiver-startup layout migration has long since run.

- **Undecided (raised 2026-08-02):** the Django admin is unreachable in
  production and no decision has been made on whether to fix it, drop it, or
  leave it. It *is* wired — `django.contrib.admin` in `INSTALLED_APPS`, mounted
  at `config/urls.py` via `path(settings.ADMIN_URL, admin.site.urls)`, with two
  read-only ops views in `radar/admin.py` (`RadarFrame`, `ArchiveGap`; add /
  change / delete all forced `False`). It works in dev, where
  `ADMIN_URL = "admin/"`. In production `ADMIN_URL` comes from
  `DJANGO_ADMIN_URL`, which `scripts/gen-production-credentials.sh` generates as
  a random 16-byte path — but Nginx only proxies the literal `/admin/`
  (`compose/production/nginx/default.conf`), so `/admin/` reaches Django and
  404s (admin isn't mounted there) while `/{random}/` matches no `location` and
  falls through to `location /`'s `try_files /index.html =404`, serving the SPA
  shell without ever reaching Django. Both doors are shut. No superuser is
  created anywhere either (no `createsuperuser` in `/start`, the deploy skill,
  or the README). Options: set `DJANGO_ADMIN_URL=admin/` (cheap, trades away the
  obscure-path hardening); teach Nginx the random path via `envsubst`
  templating (keeps hardening, adds moving parts to CD); or accept the current
  state as unintended hardening and delete the dead Nginx `location /admin/`
  block. Low value either way — both admin views are read-only over data
  `/metrics` and `/api/radar/frames` already expose. One new input as of
  2026-08-20: dropping the admin also drops `SessionMiddleware`,
  `CsrfViewMiddleware`, `AuthenticationMiddleware` and `MessageMiddleware`, which
  cost ~0.54 ms of thread-executor round-trips on *every* request — see the load
  entry at the top and `specs/tile-serving-performance.md` §3.3.
