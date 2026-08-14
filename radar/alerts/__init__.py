"""Background storm-alert push (Web Push) — an isolated, additive subsystem.

The server-side counterpart to the foreground alerts in ``frontend/js/alerts.js``:
an evaluator (running **only** in the archiver) consumes the existing
``lightning:strikes`` Redis pub/sub channel and, for each stored
``PushSubscription``, applies the **same two-tier / re-arm logic** as the frontend
and sends a localized Web Push. It is a **third failure domain**, strictly
downstream of the pub/sub seam — it never touches radar or lightning ingestion.

Tier/re-arm/freshness constants are shared with the frontend (kept identical to
``alerts.js``): 30 km / 10 km rings, 30-min per-tier quiet window, 10-min freshness.
"""

from __future__ import annotations

import math

TIER_OUTER_KM = 30.0  # "storm approaching"
TIER_INNER_KM = 10.0  # "storm overhead"
REARM_S = 1800  # a tier re-arms after this many strike-free seconds
FRESH_S = 600  # ignore strikes older than this (the SSE replays a recent buffer)

_EARTH_KM = 6371.0
# 8-wind sectors, clockwise from North (indices match round(bearing / 45) % 8).
_DIRS = ("n", "ne", "e", "se", "s", "sw", "w", "nw")

# Tiers ordered inner-first so an inner strike is handled before the outer in a pass
# (mirrors the frontend); each entry is (id, radius_km).
TIERS = (("inner", TIER_INNER_KM), ("outer", TIER_OUTER_KM))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return _EARTH_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing8(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """8-wind compass sector from point 1 toward point 2 (flat-earth ok at ≤ 30 km)."""
    d_lon = math.radians(lon2 - lon1)
    d_lat = math.radians(lat2 - lat1)
    angle = math.atan2(d_lon * math.cos(math.radians(lat1)), d_lat)  # 0 = N, clockwise
    deg = math.degrees(angle)
    idx = round(((deg % 360) + 360) / 45) % 8
    return _DIRS[idx]
