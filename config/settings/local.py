from .base import *  # noqa: F403
from .base import METEOFRANCE_APPLICATION_ID
from .base import MIDDLEWARE
from .base import PUSH_ALERTS_ENABLED
from .base import VAPID_PRIVATE_KEY
from .base import VAPID_PUBLIC_KEY
from .base import VAPID_SUBJECT
from .base import env
from .base import require_meteofrance_credentials
from .base import require_vapid_keys

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = True
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="MRSPnt7Z4cp7n6cjco3FzdikPIykcOlo2hLtSVj5vId12wgJyUtSMRaQ4vPadFWa",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = ["localhost", "0.0.0.0", "127.0.0.1"]  # noqa: S104

# Dev-only: never let the browser cache the un-hashed SPA assets, so frontend
# edits always load on reload (no stale ES modules). Prod is unaffected.
MIDDLEWARE = [*MIDDLEWARE, "config.middleware.NoCacheStaticMiddleware"]

# CACHES
# ------------------------------------------------------------------------------
# Django's cache framework is unused by the radar app (it talks to Redis
# directly via radar/cache.py); locmem keeps dev self-contained.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "",
    },
}

# Radar archive — dev overrides
# ------------------------------------------------------------------------------
# Poll faster so the dev archive fills quickly.
POLL_INTERVAL = 120

# Météo-France radar provider — enable in dev
# ------------------------------------------------------------------------------
METEOFRANCE_ENABLED = True
# ...including the REFLECTIVITE wash, so dev exercises the two-product composite.
METEOFRANCE_REFLECTIVITY_ENABLED = True

# Fail-fast guards
# ------------------------------------------------------------------------------
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
