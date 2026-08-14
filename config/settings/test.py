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

# Fail-fast guards (METEOFRANCE_ENABLED and PUSH_ALERTS_ENABLED are False
# in base + test, so these will pass — kept for consistency).
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
