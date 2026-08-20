"""Base settings to build other settings files upon."""

from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

# repo root (rainradar/)
BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent
# vanilla-JS frontend; also the /static/ source root
FRONTEND_DIR = BASE_DIR / "frontend"
env = environ.Env()

READ_DOT_ENV_FILE = env.bool("DJANGO_READ_DOT_ENV_FILE", default=False)
if READ_DOT_ENV_FILE:
    # OS environment variables take precedence over variables from .env
    env.read_env(str(BASE_DIR / ".env"))

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
# Overridden to True in local.py; production and test inherit this safe default.
DEBUG = False
TIME_ZONE = "Europe/Paris"
LANGUAGE_CODE = "en-us"
USE_I18N = True
USE_TZ = True

# DATABASES
# ------------------------------------------------------------------------------
# In containers/prod the entrypoint exports DATABASE_URL (PostgreSQL 17). For
# host-side test runs without Postgres, fall back to sqlite.
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    ),
}
# No ATOMIC_REQUESTS: radar views are async and perform no DB writes.
# DEFAULT_AUTO_FIELD is unset: Django 6's default is already BigAutoField.

# URLS
# ------------------------------------------------------------------------------
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# APPS
# ------------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
]
THIRD_PARTY_APPS = []
LOCAL_APPS = [
    "radar",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# PASSWORDS
# ------------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# MIDDLEWARE
# ------------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # ETag/If-None-Match for the JSON APIs: they answer with Cache-Control:
    # no-cache (store-but-revalidate), and without a validator every
    # revalidation is a full 200 re-download. This turns unchanged payloads
    # into empty 304s. Streaming responses (the lightning SSE) are exempt.
    "django.middleware.http.ConditionalGetMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# STATIC
# ------------------------------------------------------------------------------
# The vanilla-JS frontend is the static root: /static/... -> frontend/...
# In prod Nginx serves these; in dev Django's staticfiles does.
STATIC_ROOT = str(BASE_DIR / "staticfiles")
STATIC_URL = "/static/"
STATICFILES_DIRS = [str(FRONTEND_DIR)]
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# TEMPLATES
# ------------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# SECURITY
# ------------------------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
# Django defaults to "same-origin", which strips the Referer on cross-origin
# requests — OpenStreetMap's tile servers then reject the base-map tiles with
# "Referer is required". Send the origin cross-origin instead (web default).
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

# ADMIN
# ------------------------------------------------------------------------------
ADMIN_URL = "admin/"
ADMINS = ['"Hervé Le Roy" <hleroy@hleroy.com>']
MANAGERS = ADMINS

# LOGGING
# ------------------------------------------------------------------------------
# The "json" formatter (structured one-line JSON) is registered here and
# selected by production.py; local/dev keeps "verbose" for readability.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s",
        },
        "json": {
            "()": "radar.logging_json.JsonFormatter",
        },
    },
    "filters": {
        "suppress_cancelled_error": {
            "()": "radar.logging_json.SuppressCancelledError",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["suppress_cancelled_error"],
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
}

# REDIS
# ------------------------------------------------------------------------------
# Same hostname in local and prod (the compose service is always called "redis"
# on the default project network). Override in a concrete settings module if needed.
REDIS_URL = "redis://redis:6379/0"
REDIS_SSL = False

# CONFIGURATION vs SECRETS
# ------------------------------------------------------------------------------
# Application configuration lives in code (the constants below), not in env. Only
# secrets, deployment wiring (DATABASE_URL / REDIS_URL / ALLOWED_HOSTS), and the
# two *per-container role* flags (ARCHIVER_ENABLED / LIGHTNING_ENABLED, which can
# differ between the web and archiver containers of the same image) are read from
# the environment. Per-environment differences (dev vs prod) belong in local.py /
# production.py, never in .env files.

# RADAR — live view
# ------------------------------------------------------------------------------
RADAR_PROVIDER = "rainviewer"
RAINVIEWER_API_URL = "https://api.rainviewer.com/public/weather-maps.json"
RAINVIEWER_TILE_HOST = "https://tilecache.rainviewer.com"
# Upstream-supplied tile hosts are honoured only if https and within this domain
# suffix; otherwise we fall back to RAINVIEWER_TILE_HOST (SSRF defense-in-depth).
RAINVIEWER_ALLOWED_HOST_SUFFIX = ".rainviewer.com"
RADAR_TILE_SIZE = 256
RADAR_COLOR = 2
RADAR_OPTIONS = "1_1"
# S,N,W,E
RADAR_BBOX = [41.2, 51.5, -6.0, 9.7]
RADAR_ZOOM_MIN = 3
RADAR_ZOOM_MAX = 7
FRAMES_CACHE_TTL = 60
# /api/radar/frames live-window response micro-cache TTL (s). Every client hits
# that endpoint on page load and on its periodic refresh, yet the payload only
# changes when a new frame lands (~10 min) or a gap opens — a short TTL makes
# the Postgres load independent of visitor count while staying imperceptibly
# fresh. The ?from=&to= historical variant is never cached.
FRAMES_LIVE_CACHE_TTL = 15

# METEOFRANCE — the Météo-France radar provider
# ------------------------------------------------------------------------------
# METEOFRANCE_ENABLED is a per-deployment feature flag (same value in web +
# archiver), so it is a settings constant, set in the concrete modules: False
# here (the dark default, leaving RainViewer the only source), True in local.py so
# dev exercises it and True in production.py. Its only env input is the secret
# application ID.
# RADAR_PROVIDER stays the DEFAULT provider (no ?provider= ⇒ that one).
METEOFRANCE_ENABLED = False
# The portal "application ID" = base64(consumer_key:consumer_secret), used as
# Basic auth on the token endpoint. Secret — never log it or the token. It comes from
# an OAuth2 credential; a portal *API key* is a different thing the app cannot use.
# To rotate it, see the `deploy` skill, "Rotating the Météo-France credential".
METEOFRANCE_APPLICATION_ID = env("METEOFRANCE_APPLICATION_ID", default="")
# NB: no ``/v1`` segment — the DPRadar catalog's own HATEOAS hrefs (self + produit
# links) sit under ``/public/DPRadar/`` without a version, so the base must match or
# the ``_resolve_href`` SSRF guard rejects every produit link.
METEOFRANCE_API_BASE_URL = "https://public-api.meteofrance.fr/public/DPRadar"
METEOFRANCE_TOKEN_URL = "https://portail-api.meteofrance.fr/token"  # noqa: S105
METEOFRANCE_ZONE = "METROPOLE"
METEOFRANCE_OBSERVATION = "LAME_D_EAU"
METEOFRANCE_MAILLE = 500
# Composite refactor: render a REFLECTIVITE "wet atmosphere" wash *under* the
# LAME_D_EAU rain, into the same single tile. A per-deployment sub-flag of
# METEOFRANCE_ENABLED, so it is a settings constant like its parent; False here
# ⇒ Météo-France tiles stay LAME_D_EAU-only, byte-for-byte (local.py and
# production.py both turn it on). Needs no new secret — it reuses
# METEOFRANCE_APPLICATION_ID, but it does cost disk (see CLAUDE.md, Production notes).
METEOFRANCE_REFLECTIVITY_ENABLED = False
# REFLECTIVITE is published at maille=1000 only (500/250 ⇒ HTTP 400), and comes back
# as BUFR rather than the HDF5 LAME_D_EAU serves at maille=500. Both the observation
# and the mesh therefore differ from the rain product, hence a separate pair of
# constants rather than reusing the one above.
METEOFRANCE_REFLECTIVITY_OBSERVATION = "REFLECTIVITE"
METEOFRANCE_REFLECTIVITY_MAILLE = 1000
# Wall-clock deadline for the whole best-effort reflectivity arm (catalog + product).
# Without it "best-effort" would only cover *errors*, not *latency*: the arm carries
# its own retry budget (~36 s of catalog attempts + ~101 s of product attempts) and the
# rain arm waits on it, so a merely slow REFLECTIVITE endpoint could stall a frame for
# over two minutes. Same rationale as PUSH_SEND_TIMEOUT. Must stay well under
# METEOFRANCE_FRAME_INTERVAL — the archiver's poll job is max_instances=1.
METEOFRANCE_REFLECTIVITY_DEADLINE = 20
# Seconds between Météo-France composites; the timeline + gap tolerance
# flow from this via the provider's frame_interval, never a hardcoded interval.
METEOFRANCE_FRAME_INTERVAL = 300
# How far the reflectivity mosaic may be from the rain frame it is composited under.
# Both products publish on the same cadence and were verified live to advertise the
# same validity_time, so the tolerance *is* one frame — derived, not re-typed, so it
# can never drift from the cadence above. Past it the wash is dropped rather than
# painted at the wrong instant into a 90-day archive it can't be separated from.
METEOFRANCE_REFLECTIVITY_MAX_SKEW = METEOFRANCE_FRAME_INTERVAL
# The MF poll job's cadence: each tick is one cheap catalog GET; the
# ~1.7 MB product downloads only when validity_time is new.
METEOFRANCE_POLL_INTERVAL = 60
# How long a *failed* frame stays failed before the provider will re-fetch it.
# The 62 tiles of a frame reach the single-flight memo in ~8 waves (they queue
# behind TILE_FETCH_CONCURRENCY), and without this every wave restarted the whole
# download+retry budget from scratch. Only has to outlast one archive_frame call,
# which the waves complete back-to-back — and must stay well under
# METEOFRANCE_POLL_INTERVAL so the next poll is a real retry, not a cached failure.
METEOFRANCE_FAILURE_COOLDOWN = 30

# RADAR — durable archive
# ------------------------------------------------------------------------------
# Archiver: gates run_archiver; True *only* in the archiver container. A
# per-container role flag ⇒ read from env, set in the compose ``environment:``
# blocks (true in ``archiver`` and nowhere else, explicitly false in ``django``)
# rather than in the shared ``.envs/*/.django`` file.
ARCHIVER_ENABLED = env.bool("ARCHIVER_ENABLED", default=False)
POLL_INTERVAL = 300  # radar poll cadence (s)
FRAME_INTERVAL = 600  # expected frame cadence (s)
# Consecutive failed polls before an ongoing gap is opened (avoids flap churn).
GAP_OPEN_AFTER_FAILURES = 2
RETENTION_DAYS = 90  # archive horizon
JANITOR_HOUR = 3  # UTC hour for the daily janitor
# Archive root on the durable tile volume; UTC-date-foldered tiles live below it.
TILE_ROOT = "/data/tiles"
# Default /api/radar/frames live window (s) and max ?from..to span (s, 36h).
LIVE_WINDOW_SECONDS = 7200
MAX_QUERY_SPAN_SECONDS = 129600
ARCHIVE_RANGE_CACHE_TTL = 60
# About-dialog /api/stats Redis cache TTL (s); shields Postgres from COUNT(*) on
# the partitioned lightning_strike table under repeated dialog opens.
STATS_CACHE_TTL = 60
# /metrics exposition Redis cache TTL (s); shields Postgres from the per-scrape
# aggregates (incl. the unbounded COUNT(*) over lightning_strike) — the endpoint
# is public, so scrape cadence is not under our control. Keep it below the
# scraper's interval so every scrape still observes fresh-enough values.
METRICS_CACHE_TTL = 30
# 20 GiB; backs the radar_storage_used_ratio metric.
STORAGE_CAPACITY_BYTES = 21474836480
TILE_FETCH_CONCURRENCY = 8
# Process-wide ceiling on *simultaneous* upstream tile fetches, shared across the
# archiver's batch and the on-demand view path. RainViewer's free tier 429s under
# a wide burst (a cold-cache page-load fans out ~60 tiles at once), so this caps
# concurrent connections regardless of how many requests arrive. Keep it small.
UPSTREAM_TILE_CONCURRENCY = 4
# Ceiling on *simultaneous* archive-row lookups from the tile fallback, per worker.
# Django's ASGI handler runs each request on its own thread and a DB connection is
# thread-local, so an unbounded fan-out of tile misses is an unbounded fan-out of
# Postgres connections — which is exactly how the fallback once exhausted
# max_connections and 500'd. With WEB_CONCURRENCY=4 this caps the whole tile path at
# ~16 connections regardless of how many requests arrive.
TILE_ARCHIVE_LOOKUP_CONCURRENCY = 4
# Total budget (s) for one archive-row lookup, covering the queue wait *and* the
# query. Past it the tile sheds with a non-cacheable 503 rather than piling up.
TILE_ARCHIVE_LOOKUP_TIMEOUT = 2.0
# Minimum spacing between consecutive upstream tile requests (s), process-wide.
# UPSTREAM_TILE_CONCURRENCY alone bounds *simultaneity*, not *rate*: four slots
# recycled over a keep-alive connection still emit hundreds of requests a minute,
# and a cold start has ~13 unarchived frames x 62 tiles to fetch back-to-back. This
# is the actual rate ceiling (0.05 => 20 req/s => a full cold-start backfill spread
# over ~40 s instead of a few seconds).
UPSTREAM_TILE_MIN_INTERVAL = 0.05
# How long to stop calling RainViewer after it answers 429, when it sends no
# Retry-After of its own. The gate *refuses* during this window rather than
# sleeping in-request, so nothing is held open waiting for it.
UPSTREAM_RATE_LIMIT_COOLDOWN = 60.0
# Ceiling on an honoured Retry-After. Upstream could otherwise park us for hours;
# past this we retry on our own schedule and let the poll re-open the window.
UPSTREAM_RATE_LIMIT_COOLDOWN_MAX = 300.0

# LIGHTNING
# ------------------------------------------------------------------------------
# LIGHTNING_ENABLED gates the Blitzortung ingester (in the single-replica
# archiver, alongside ARCHIVER_ENABLED) *and* whether the web container advertises
# the layer in /api/radar/frames. The web and archiver can hold different
# values (ingest without display), so it is a per-container role flag ⇒ read from
# env, True only where the role runs.
LIGHTNING_ENABLED = env.bool("LIGHTNING_ENABLED", default=False)
LIGHTNING_SOURCE = "blitzortung"
BLITZORTUNG_WS_URLS = [
    "wss://ws1.blitzortung.org",
    "wss://ws7.blitzortung.org",
    "wss://ws8.blitzortung.org",
]
# S,N,W,E ingest + history filter; matches the radar bbox.
LIGHTNING_BBOX = [41.2, 51.5, -6.0, 9.7]
LIGHTNING_QUEUE_MAXSIZE = 10000
LIGHTNING_BATCH_SIZE = 200
LIGHTNING_BATCH_INTERVAL = 1.0
LIGHTNING_RECENT_MAX = 2000
LIGHTNING_RECENT_SECONDS = 300
LIGHTNING_SSE_HEARTBEAT_SECONDS = 20
LIGHTNING_DISPLAY_HOURS = 12
LIGHTNING_REBIN_SECONDS = 60
# The strike pool has to cover whatever radar range /api/radar/frames just served,
# plus lightning.js's one-slice lead-in (SLICE_FALLBACK_S) — that is the definition of
# ensurePool(). Derived, not a standalone number: a hardcoded 86400 was smaller than
# both, so a full archived day (86100 s of frames + 600 s lead-in) always 400'd
# `range_too_large` and the layer silently came up empty. LIGHTNING_HISTORY_MAX_STRIKES
# is what actually bounds the response; the span cap only bounds the scan.
LIGHTNING_HISTORY_MAX_SPAN_SECONDS = MAX_QUERY_SPAN_SECONDS + 3600
LIGHTNING_HISTORY_MAX_STRIKES = 50000
# Lightning archive horizon; tracks the shared radar RETENTION_DAYS.
LIGHTNING_RETENTION_DAYS = RETENTION_DAYS
LIGHTNING_BACKOFF_MIN = 1.0
LIGHTNING_BACKOFF_MAX = 60.0

# STORM ALERTS — BACKGROUND DELIVERY (Web Push)
# ------------------------------------------------------------------------------
# PUSH_ALERTS_ENABLED is a per-deployment feature flag (same value in web +
# archiver), so it is a settings constant, set in the concrete modules: False
# here (⇒ foreground-only alerts, the default) and True in production.py. Being a
# constant, it is NOT readable from the environment — setting it in .envs/ is
# silently ignored. It gates BOTH the web subscribe/unsubscribe
# endpoints + push advert AND the archiver's push evaluator (which also needs
# LIGHTNING_ENABLED). Its only env inputs are the VAPID secrets. VAPID keys are
# self-generated (no vendor accounts); enabled-but-unset ⇒ ImproperlyConfigured
# (checked by require_vapid_keys in the concrete settings module).
PUSH_ALERTS_ENABLED = False
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")  # base64url applicationServerKey
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")  # base64url raw private key
VAPID_SUBJECT = env("VAPID_SUBJECT", default="")  # e.g. mailto:ops@example.com
# The server POSTs to a *stored* endpoint URL — an SSRF vector unless constrained.
# An endpoint's hostname must end with one of these suffixes (browser push services).
PUSH_ENDPOINT_ALLOWED_SUFFIXES = [
    "fcm.googleapis.com",  # Chrome / Chromium (Edge, Samsung, Brave…)
    "push.apple.com",  # Safari incl. iOS (web.push.apple.com)
    "push.services.mozilla.com",  # Firefox
    "notify.windows.com",  # legacy Edge / WNS
]
PUSH_MAX_SUBSCRIPTIONS = 500  # new-endpoint cap
PUSH_STALE_DAYS = 60  # janitor prune horizon
PUSH_SEND_CONCURRENCY = 5  # send semaphore
PUSH_SEND_TIMEOUT = 10  # per-send seconds
PUSH_TTL_SECONDS = 900  # push-service TTL
PUSH_SUBS_REFRESH_SECONDS = 60  # evaluator refresh


# FEATURE-FLAG FAIL-FAST GUARDS
# ------------------------------------------------------------------------------
# METEOFRANCE_ENABLED and PUSH_ALERTS_ENABLED are constants set per settings
# module; their paired secrets stay in env. These helpers are called at the end
# of each concrete settings module (local/production) AFTER the flag is set, so
# the check sees the final value — not base's dark default. Module-level (not a
# Django system check) so they fire for uvicorn AND management commands.
def require_meteofrance_credentials(*, enabled, application_id, reflectivity=False):
    if enabled and not application_id:
        _msg = "METEOFRANCE_ENABLED requires METEOFRANCE_APPLICATION_ID."
        raise ImproperlyConfigured(_msg)
    # The wash is a sub-flag: enabling it without its parent would silently do
    # nothing, since the whole Météo-France provider would be dark.
    if reflectivity and not enabled:
        _msg = "METEOFRANCE_REFLECTIVITY_ENABLED requires METEOFRANCE_ENABLED."
        raise ImproperlyConfigured(_msg)


def require_vapid_keys(*, enabled, public, private, subject):
    if enabled and not (public and private and subject):
        _msg = "PUSH_ALERTS_ENABLED requires VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY and VAPID_SUBJECT."
        raise ImproperlyConfigured(_msg)
