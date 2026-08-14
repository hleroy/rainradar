"""The pluggable lightning source interface.

Mirrors the radar ``RadarProvider`` pattern: lightning sits behind a
``LightningSource`` so the (undocumented, obfuscated) Blitzortung wire protocol
can be swapped later without touching ingest, views, or the frontend. A source
is a thin "bytes → :class:`Strike`" unit: it owns WS connect, handshake, frame
decode and JSON parse, and yields **bbox-unfiltered** strikes from ONE
connection. bbox filtering, queueing and persistence are the supervisor's job
, never the source's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Protocol
from typing import runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager


@dataclass(frozen=True)
class Strike:
    """One decoded lightning strike (source-agnostic).

    ``struck_at`` is epoch **seconds** (sub-second float), UTC — the supervisor
    converts it to a tz-aware datetime for the partitioned table. ``intensity``
    is a relative **proxy** (e.g. detecting-station count), never a calibrated
    amperage, and may be ``None``.
    """

    struck_at: float
    lat: float
    lon: float
    intensity: int | None


class LightningSourceError(Exception):
    """The connection dropped or became unusable; the supervisor reconnects."""


@runtime_checkable
class LightningSource(Protocol):
    """A swappable lightning data source."""

    name: str

    def attribution(self) -> str:
        """Mandatory HTML credit snippet shown while the layer is active."""
        ...

    def connect(self, url: str) -> AbstractAsyncContextManager[AsyncIterator[Strike]]:
        """Open ONE connection; yield an async iterator of decoded, bbox-unfiltered strikes.

        Used as ``async with source.connect(url) as strikes: async for s in strikes``.
        Raises :class:`LightningSourceError` when the connection ends or fails.
        """
        ...
