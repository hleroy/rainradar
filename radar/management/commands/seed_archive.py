"""``manage.py seed_archive`` — one synchronous backfill pass per provider (dev).

Runs a single :func:`radar.archiver.poll_radar` for **each enabled provider** so the
dev archive fills immediately (fast timeline testing) without waiting for the
scheduler's first tick. Safe to run repeatedly — archiving is idempotent. All
providers share one event loop so loop-bound state (Redis client, provider locks)
is built once.
"""

from __future__ import annotations

import asyncio

from django.core.management.base import BaseCommand

from radar import archiver
from radar.providers import enabled_providers
from radar.providers import get_provider


class Command(BaseCommand):
    help = "Backfill the archive once from each enabled provider's live window (dev seed)."

    def handle(self, *_args, **_options) -> None:
        results = asyncio.run(self._seed_all())
        for name, stats in results:
            self.stdout.write(
                self.style.SUCCESS(
                    f"seed_archive[{name}]: frames_seen={stats['frames_seen']} "
                    f"archived={stats['archived']} tiles_written={stats['tiles_written']}",
                ),
            )

    async def _seed_all(self) -> list[tuple[str, dict]]:
        results: list[tuple[str, dict]] = []
        for name in enabled_providers():
            stats = await archiver.poll_radar(get_provider(name))
            results.append((name, stats))
        return results
