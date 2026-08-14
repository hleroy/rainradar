"""Blitzortung WebSocket adapter — the single quarantine point.

Everything Blitzortung-specific lives here: the failover endpoints, the hello
handshake, the custom frame **decode**, the JSON parse, and ``Strike``
construction. The wire protocol is undocumented/obfuscated — treat every detail
as *best-known, not guaranteed*. A single malformed frame logs ``parse_failed``
(WARNING) and is **skipped**, never fatal; the supervisor/SSE/history layers
stay protocol-agnostic, so a protocol change is a one-file fix here.

Blitzortung data is community, **non-commercial** use only and carries a
mandatory attribution ("Lightning data: Blitzortung.org and contributors", with
a link); see :meth:`BlitzortungSource.attribution` and the README. This app is
self-hosted, personal and non-commercial.

Decode: each server text frame is JSON compressed with a custom LZW-variant
dictionary scheme (the algorithm volunteers reverse-engineered from the site's
client). It must be expanded before :func:`json.loads`. For an all-ASCII input
the expansion is the identity, which keeps the pure decode unit-testable.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from radar.lightning.base import LightningSourceError
from radar.lightning.base import Strike
from radar.logging_json import emit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("radar.lightning")

# Mandatory credit, shown in the Leaflet attribution control while the layer is
# active. Non-commercial community data.
_ATTRIBUTION = (
    'Lightning data: <a href="https://www.blitzortung.org" '
    'target="_blank" rel="noopener">Blitzortung.org and contributors</a>'
)

# Subscription/hello text frame sent right after connect to start the stream.
_HELLO = '{"a":111}'

_SMALLINT_MAX = 32767  # intensity proxy is stored in a Postgres smallint
_NS_PER_S = 1_000_000_000
_LZW_BASE = 256  # code points < 256 are literals; >= 256 index the LZW dictionary


def _lzw_decode(data: str) -> str:
    """Expand Blitzortung's custom LZW-variant compression.

    Faithful port of the volunteer-reverse-engineered routine. All-ASCII input
    (every code point < 256) round-trips unchanged — the property the decode
    tests rely on.
    """
    if not data:
        return ""
    chars = list(data)
    dictionary: dict[int, str] = {}
    current = chars[0]
    prev = current
    result = [current]
    code = 256
    for ch in chars[1:]:
        point = ord(ch)
        entry = ch if point < _LZW_BASE else dictionary.get(point, prev + current)
        result.append(entry)
        current = entry[0]
        dictionary[code] = prev + current
        code += 1
        prev = entry
    return "".join(result)


def _intensity_proxy(obj: dict) -> int | None:
    """Detecting-station count as a relative proxy, capped to smallint."""
    stations = obj.get("sig")
    if stations is None:
        stations = obj.get("stations")
    if not isinstance(stations, list):
        return None
    return min(len(stations), _SMALLINT_MAX)


def decode_frame(raw: str | bytes) -> Strike:
    """Decompress + parse one server frame into a :class:`Strike`.

    Raises ``ValueError``/``KeyError``/``TypeError`` on any malformed frame; the
    caller turns that into a logged ``parse_failed`` skip (never a raise outward).
    """
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    obj = json.loads(_lzw_decode(text))
    # Required fields; a frame missing any of them is not a strike (control/keepalive).
    time_ns = obj["time"]
    lat = obj["lat"]
    lon = obj["lon"]
    if not isinstance(time_ns, (int, float)):
        msg = "non-numeric time"
        raise TypeError(msg)
    return Strike(
        struck_at=float(time_ns) / _NS_PER_S,  # ns epoch -> seconds (UTC)
        lat=float(lat),
        lon=float(lon),
        intensity=_intensity_proxy(obj),
    )


class BlitzortungSource:
    """Blitzortung implementation of :class:`~radar.lightning.base.LightningSource`."""

    name = "blitzortung"

    def attribution(self) -> str:
        return _ATTRIBUTION

    def _decode(self, raw: str | bytes) -> Strike | None:
        """Decode one frame; log + swallow a single bad frame."""
        try:
            return decode_frame(raw)
        except Exception as exc:  # noqa: BLE001 — one bad frame never tears down the stream
            emit(
                logger,
                logging.WARNING,
                "parse_failed",
                service="lightning",
                reason=type(exc).__name__,
            )
            return None

    async def _iter_strikes(self, ws) -> AsyncIterator[Strike]:
        # websockets' async-for raises ConnectionClosed when the peer goes away;
        # surface it (and any read error) as LightningSourceError so the
        # supervisor reconnects with backoff.
        import websockets  # noqa: PLC0415 — lazy: dep added in step 6, decode stays import-free

        try:
            async for raw in ws:
                strike = self._decode(raw)
                if strike is not None:
                    yield strike
        except websockets.ConnectionClosed as exc:
            raise LightningSourceError(str(exc)) from exc

    @asynccontextmanager
    async def connect(self, url: str) -> AsyncIterator[AsyncIterator[Strike]]:
        """Open one WS connection, send the hello, yield the strike iterator."""
        import websockets  # noqa: PLC0415 — lazy import (see _iter_strikes)

        try:
            async with websockets.connect(url, open_timeout=10, close_timeout=5) as ws:
                await ws.send(_HELLO)
                yield self._iter_strikes(ws)
        except LightningSourceError:
            raise
        except (OSError, websockets.WebSocketException) as exc:
            raise LightningSourceError(str(exc)) from exc
