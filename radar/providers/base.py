"""Pluggable radar data-source interface.

Views and cache talk only to this interface; each provider owns its upstream
protocol and maps timestamp -> internal ref itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Protocol
from typing import runtime_checkable

if TYPE_CHECKING:
    import httpx


class FramesUnavailable(Exception):
    """Upstream frame index could not be fetched and no cached copy exists.

    Views map this to HTTP 503 ``{"error": "frames_unavailable"}``.
    """


class TileUpstreamError(Exception):
    """Tile upstream failed after retries (5xx/timeout). Views map to 502."""


class RateLimited(TileUpstreamError):
    """Upstream is throttling us (HTTP 429), or we are inside its cooldown.

    A subclass so every existing ``except TileUpstreamError`` keeps working
    unchanged; callers that *can* do better — the archiver aborts the rest of the
    batch rather than grinding through it — catch this narrower type first.

    ``retry_after`` is the upstream's own ``Retry-After`` in seconds when it sent a
    parseable one, else ``None``.
    """

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("upstream rate limit")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Frame:
    timestamp: int  # epoch seconds, UTC
    ref: str  # provider-opaque token (RainViewer: the `path`)


@runtime_checkable
class RadarProvider(Protocol):
    name: str
    # Seconds between consecutive frames from this provider. Drives the
    # archiver's aged-out-gap math, the poll, and the frontend's gap tolerance /
    # timeline density — so no frame cadence is ever hardcoded per provider.
    # RainViewer: settings.FRAME_INTERVAL (600); Météo-France: 300.
    frame_interval: int

    async def get_frames(self) -> list[Frame]:
        """Return the past-frame index, oldest -> newest."""
        ...

    def tile_client(self) -> httpx.AsyncClient:
        """A configured HTTP client to reuse across a batch of tile fetches.

        Used as an async context manager so a single connection pool serves all
        tiles of one frame instead of re-handshaking per tile.
        """
        ...

    async def get_tile(
        self,
        ts: int,
        z: int,
        x: int,
        y: int,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> bytes | None:
        """Return PNG bytes for a tile; None means a legitimate empty/404 tile.

        When ``client`` is given it is reused (and not closed); otherwise the
        provider opens and closes its own client for this single fetch.
        """
        ...

    def attribution(self) -> str:
        """Mandatory credit string (HTML snippet with clickable link)."""
        ...
