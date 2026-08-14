# TODO

Dated follow-ups that are neither issues nor spec material. Check dates when
touching the repo; delete entries once done.

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
  entry once the measured rate is known and unsurprising.

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
  `/metrics` and `/api/radar/frames` already expose.

- **Open (raised 2026-08-02, partly fixed 2026-08-03):** the live frame-index
  refresh interval is still hardcoded and provider-agnostic —
  `REFRESH_MS = 5 * 60 * 1000` in `frontend/js/radar.js`, with
  `REFRESH_MAX_MS = 30 * 60 * 1000` as the backoff ceiling. It sits awkwardly
  against the CLAUDE.md non-negotiable that "the 5-min cadence flows from
  `provider.frame_interval` — no new hardcoded intervals", and it **aliases**
  against Météo-France: a 300 s client poll of a 300 s cadence can sit just
  before each publication and stay a full frame behind indefinitely.
  **Don't fix it by setting `refreshDelay = frame_interval`** — that reproduces
  the same aliasing on RainViewer, doubled. Use the cadence as an *anchor*:
  schedule the next refresh at `newest_ts + frame_interval + lag_margin`
  (clamped + jittered), then short retries until the frame lands. Measured
  2026-08-03 against production, frames become visible ~140–217 s (Météo-France)
  and ~94–339 s (RainViewer) after their own timestamp, so the margin is small
  and the anchored schedule costs *fewer* requests than today's 12/h.
  Also still open: whether a 30-min backoff ceiling is too aggressive for a view
  labelled "DIRECT" (`max(frame_interval, 10 min)` would be defensible), and
  whether returning to a visible tab should clear the backoff outright.

  Already fixed, and **not** what the original entry suspected: the refresh did
  run on time, but paused viewers never saw it. `refresh()` re-pinned the cursor
  to the frame that was showing, so the default (paused) state froze the picture
  for up to the whole 2 h window while the index kept refreshing — a phone whose
  screen slept for 40 min came back to a 40-min-old image under a blinking
  DIRECT pill. The cursor now follows the live edge unless the user deliberately
  scrubbed away, the pill only claims LIVE while the cursor is at that edge, and
  the button re-phases the cadence + clears the backoff. See design doc §13.1.
