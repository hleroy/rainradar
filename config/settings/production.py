from .base import *  # noqa: F403
from .base import DATABASES
from .base import METEOFRANCE_APPLICATION_ID
from .base import REDIS_URL
from .base import VAPID_PRIVATE_KEY
from .base import VAPID_PUBLIC_KEY
from .base import VAPID_SUBJECT
from .base import env
from .base import require_meteofrance_credentials
from .base import require_vapid_keys

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env("DJANGO_SECRET_KEY")
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["rainradar.hleroy.com"])

# DATABASES
# ------------------------------------------------------------------------------
# 0, deliberately — persistent connections are a WSGI optimisation and are actively
# harmful here. Django's ASGI handler opens a fresh `ThreadSensitiveContext` per
# request (one single-worker thread pool each), and a Django connection is
# thread-local, so the next request lands on a different thread and can never reuse
# the one this request opened. A non-zero CONN_MAX_AGE therefore buys nothing and
# leaks: `close_old_connections` leaves the socket open at request end, the request
# thread then dies with its context, and the connection lingers until GC. Under the
# tile fallback's fan-out that walked straight into Postgres' max_connections and
# 500'd every tile — see the miss-ladder tiers in the design doc §5.2.
DATABASES["default"]["CONN_MAX_AGE"] = 0

# CACHES
# ------------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # Mimicking memcache behavior.
            # https://github.com/jazzband/django-redis#memcached-exceptions-behavior
            "IGNORE_EXCEPTIONS": True,
        },
    },
}

# SECURITY
# ------------------------------------------------------------------------------
# TLS is terminated by the host Traefik; honor its forwarded proto header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Traefik already redirects to HTTPS; Django redirect is opt-in (off by default).
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_NAME = "__Secure-sessionid"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_NAME = "__Secure-csrftoken"
# 6 days, per the original ramp-up plan (60s was proven in production).
SECURE_HSTS_SECONDS = 518400
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# Preload is only honoured by browsers'/hstspreload.org's list at max-age >=
# 31536000 (1 year); advertising it below that is a no-op. Default off until
# max-age is raised to a year — then flip to True.
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True

# STATIC
# ------------------------------------------------------------------------------
# Nginx serves frontend/ + /static; collectstatic writes to STATIC_ROOT.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# ADMIN
# ------------------------------------------------------------------------------
ADMIN_URL = env("DJANGO_ADMIN_URL")

# LOGGING
# ------------------------------------------------------------------------------
# Production emits structured one-line JSON to stdout so a later Promtail→Loki
# step can ship it; the archiver container shares these settings.
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
            "formatter": "json",
            "filters": ["suppress_cancelled_error"],
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "django.security.DisallowedHost": {
            "level": "ERROR",
            "handlers": ["console"],
            "propagate": True,
        },
    },
}

# Météo-France radar provider — enabled in production
# ------------------------------------------------------------------------------
# Per-deployment flag, so the same value must hold for web + archiver; both read
# this module. The paired secret (METEOFRANCE_APPLICATION_ID) comes from
# .envs/.production/.django on the host — see the fail-fast guard below.
METEOFRANCE_ENABLED = True
# ...including the REFLECTIVITE wash composited under the rain. Costs a second
# grid fetch per frame and extra tile bytes; degrades to rain-only on failure.
METEOFRANCE_REFLECTIVITY_ENABLED = True

# Background storm alerts (Web Push) — enabled in production
# ------------------------------------------------------------------------------
# Per-deployment flag like the two above, gating the web subscribe/unsubscribe
# endpoints + push advert AND the evaluator in the single-replica archiver (which
# also needs LIGHTNING_ENABLED, set on that container). NB: this is a settings
# constant by design — setting PUSH_ALERTS_ENABLED in .envs/ has no effect, since
# nothing reads it from the environment. Its paired VAPID_* secrets do come from
# there; see the fail-fast guard below.
PUSH_ALERTS_ENABLED = True

# Fail fast when a feature flag is enabled without its credential. All three flags
# are set above, so both guards are live: each one's paired env secret must be
# present in .envs/.production/.django or the container refuses to start.
require_meteofrance_credentials(
    enabled=METEOFRANCE_ENABLED,
    application_id=METEOFRANCE_APPLICATION_ID,
    reflectivity=METEOFRANCE_REFLECTIVITY_ENABLED,
)
require_vapid_keys(
    enabled=PUSH_ALERTS_ENABLED,
    public=VAPID_PUBLIC_KEY,
    private=VAPID_PRIVATE_KEY,
    subject=VAPID_SUBJECT,
)
