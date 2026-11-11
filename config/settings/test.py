"""With these settings, tests run faster."""

from .base import *  # noqa: F403
from .base import METEOFRANCE_APPLICATION_ID
from .base import METEOFRANCE_ENABLED
from .base import METEOFRANCE_REFLECTIVITY_ENABLED
from .base import PUSH_ALERTS_ENABLED
from .base import TEMPLATES
from .base import VAPID_PRIVATE_KEY
from .base import VAPID_PUBLIC_KEY
from .base import VAPID_SUBJECT
from .base import env
from .base import require_meteofrance_credentials
from .base import require_vapid_keys

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="Nv2fiz1e1kyTN7r3nPovVOxmbtJmwBOdkWmubjxQzD019NjB3CHk283jz1tHCtp4",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#test-runner
TEST_RUNNER = "django.test.runner.DiscoverRunner"

# PASSWORDS
# ------------------------------------------------------------------------------
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# CACHES
# ------------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "",
    },
}

# DEBUGGING FOR TEMPLATES
# ------------------------------------------------------------------------------
TEMPLATES[0]["OPTIONS"]["debug"] = True  # type: ignore[index]

# RADAR
# ------------------------------------------------------------------------------
# Pin the matrix to the canonical France (incl. Corsica) values so the archive
# tests assert the 62-tile invariant regardless of dev overrides in local.py.
RADAR_BBOX = [41.2, 51.5, -6.0, 9.7]
RADAR_ZOOM_MIN = 3
RADAR_ZOOM_MAX = 7
# No inter-request pacing against mocked upstreams: at the production 0.05 s every
# 62-tile archive test would spend 3 s in real sleeps. Tests that assert the pacing
# itself override this back to a non-zero value.
UPSTREAM_TILE_MIN_INTERVAL = 0.0

# REDIS — a database of its own (base uses /0). The suite flushes Redis before every
# test, and a dev stack running alongside writes live state there (the archiver's
# forecast pointer, frame caches): sharing /0 made tests flaky and wiped the dev cache.
REDIS_URL = "redis://redis:6379/1"

# FORECAST — pinned off, whatever base.py ships: it requires METEOFRANCE_ENABLED,
# which is off here, and the tests that need it switch both on themselves.
FORECAST_ENABLED = False

# Fail-fast guards (METEOFRANCE_ENABLED and PUSH_ALERTS_ENABLED are False in base,
# FORECAST_ENABLED is pinned False above, so these pass — kept for consistency).
require_meteofrance_credentials(
    enabled=METEOFRANCE_ENABLED,
    application_id=METEOFRANCE_APPLICATION_ID,
    reflectivity=METEOFRANCE_REFLECTIVITY_ENABLED,
    forecast=FORECAST_ENABLED,
)
require_vapid_keys(
    enabled=PUSH_ALERTS_ENABLED,
    public=VAPID_PUBLIC_KEY,
    private=VAPID_PRIVATE_KEY,
    subject=VAPID_SUBJECT,
)
