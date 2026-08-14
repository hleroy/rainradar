"""Archive data model.

Two tables back the durable radar archive: ``radar_frame`` indexes every
archived frame (one row per RainViewer timestamp) and ``archive_gap`` records
collection outages longer than the upstream backfill window. All datetime
fields are ``timestamptz`` (``USE_TZ=True``); ``timestamp`` stays a raw
epoch-seconds ``BIGINT`` exactly as RainViewer reports it.
"""

from __future__ import annotations

from django.db import models
from django.db.models import JSONField
from django.db.models import Value


class RadarFrame(models.Model):
    """One archived radar frame.

    ``status`` semantics:
      * ``ok``      — all matrix tiles attempted, none errored (empties are fine);
      * ``partial`` — ≥1 tile errored after retries (listed in ``missing``);
      * ``failed``  — every tile errored (or the frame index itself failed).

    The PK is a surrogate ``id``: both providers emit epoch timestamps
    that can collide on shared 600 s boundaries, so ``(provider, timestamp)`` — not
    ``timestamp`` alone — is the identity. All frame queries are provider-filtered.
    """

    id = models.BigAutoField(primary_key=True)
    timestamp = models.BigIntegerField()  # UTC epoch seconds as the provider reports it
    provider = models.CharField(max_length=20)  # provider name that archived this frame
    collected_at = models.DateTimeField(auto_now_add=True)  # when we archived it (UTC)
    tile_count = models.PositiveSmallIntegerField(default=0)  # PNGs actually written (200s)
    status = models.CharField(max_length=10)  # 'ok' | 'partial' | 'failed'
    missing = models.JSONField(default=list)  # [{"z":..,"x":..,"y":..}, ...] errored tiles
    # Tiles upstream reported as having nothing to draw (404/None), so nothing was
    # written for them. Recorded because a frame is immutable upstream once
    # published: without this, every retry of a partial frame re-fetches its empty
    # tiles forever, since "no file on disk" cannot distinguish "empty" from
    # "never fetched". Same shape as `missing`.
    # db_default, not just default: migrations run against the live DB *before* old
    # containers are swapped out, and an old container INSERTs without this column —
    # which a plain NOT NULL default (applied by Django, not the DB) would reject.
    empty = models.JSONField(default=list, db_default=Value([], JSONField()))

    class Meta:
        db_table = "radar_frame"
        constraints = [
            # A provider archives each timestamp at most once (was the PK guarantee).
            models.UniqueConstraint(
                fields=["provider", "timestamp"],
                name="radar_frame_provider_ts_uniq",
            ),
        ]
        indexes = [
            # Keep the plain timestamp index for range scans (same name as 0001, no
            # churn); add the provider-scoped composite the frames/range views use.
            models.Index(fields=["timestamp"], name="radar_frame_timesta_48c392_idx"),
            models.Index(fields=["provider", "timestamp"], name="radar_frame_provider_ts_idx"),
        ]

    def __str__(self) -> str:
        return f"RadarFrame(provider={self.provider}, ts={self.timestamp}, status={self.status})"


class ArchiveGap(models.Model):
    """A recorded collection gap.

    ``gap_end IS NULL`` marks an *ongoing* gap (upstream currently unreachable);
    it is closed on the next successful poll. Bounded gaps record an outage that
    exceeded the upstream backfill window and is therefore unrecoverable. Gaps are
    per-provider (``provider`` defaults to ``rainviewer`` for pre-existing rows).
    """

    id = models.BigAutoField(primary_key=True)
    service = models.CharField(max_length=20, default="radar")  # 'radar' | 'lightning'
    provider = models.CharField(max_length=20, default="rainviewer")  # radar provider name
    gap_start = models.DateTimeField()  # first missing instant (UTC)
    gap_end = models.DateTimeField(null=True, blank=True)  # NULL = ongoing
    reason = models.TextField()
    detail = models.JSONField(default=dict)

    class Meta:
        db_table = "archive_gap"
        indexes = [models.Index(fields=["service", "gap_start"])]

    def __str__(self) -> str:
        state = "ongoing" if self.gap_end is None else "closed"
        return f"ArchiveGap(service={self.service}, start={self.gap_start}, {state})"


class LightningStrike(models.Model):
    """One archived lightning strike.

    The DB table is **monthly RANGE-partitioned** by ``struck_at`` and managed by
    raw SQL (migration ``0002`` + :mod:`radar.lightning.partitions`), so this model
    is ``managed = False`` — Django never tries to (re)create or alter it, and the
    "no missing migrations" CI check stays green. The real DB primary key is the
    composite ``(id, struck_at)`` (the partition key must be part of the PK); for
    the ORM, the IDENTITY ``id`` is the pk. Inserts (``abulk_create`` / ``acreate``)
    work normally — the server generates ``id`` and ``struck_at`` routes each row to
    its month partition — and range reads on the parent let Postgres prune
    partitions.

    ``intensity`` is a **proxy**, not a calibrated amperage: Blitzortung exposes no
    current, so the adapter stores the detecting-station count (capped to smallint)
    or NULL. ``lat``/``lon`` are ``real`` (~7 significant digits — ample for
    strike positions, half the storage of double).
    """

    id = models.BigAutoField(primary_key=True)
    struck_at = models.DateTimeField()  # strike instant (UTC, sub-second)
    lat = models.FloatField(null=True)
    lon = models.FloatField(null=True)
    intensity = models.SmallIntegerField(null=True)  # proxy (station count) or NULL

    class Meta:
        managed = False  # table + partitions are raw-SQL managed
        db_table = "lightning_strike"
        indexes = [models.Index(fields=["struck_at"])]

    def __str__(self) -> str:
        return f"LightningStrike(struck_at={self.struck_at}, lat={self.lat}, lon={self.lon})"


class PushSubscription(models.Model):
    """A browser Web Push subscription for background storm alerts.

    The app's first per-user state — but there are still no accounts: the ``endpoint``
    URL (unique) is the identity. ``lat``/``lon`` are the user-chosen alert anchor,
    **coarsened to ~1 km** (2 decimals) before storage, for privacy — a re-upsert
    on every app open / anchor move refreshes ``last_seen_at`` and re-coarsens. Per-tier
    throttle state lives in Redis, not here. A row is deleted on unsubscribe and
    pruned by the daily janitor once ``last_seen_at`` is older than ``PUSH_STALE_DAYS``.
    """

    id = models.BigAutoField(primary_key=True)
    endpoint = models.TextField(unique=True)  # push-service URL = the identity
    p256dh = models.CharField(max_length=200)  # client public key (base64url)
    auth = models.CharField(max_length=200)  # client auth secret (base64url)
    lat = models.FloatField()  # anchor latitude, coarsened to 2 decimals
    lon = models.FloatField()  # anchor longitude, coarsened to 2 decimals
    locale = models.CharField(max_length=5, default="en")  # 'fr' | 'en' (notif copy)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)  # refreshed on every upsert

    class Meta:
        db_table = "push_subscription"
        # Explicit name so it matches migration 0003 (an unnamed Index gets a hashed
        # name, which makemigrations --check would then want to "rename" — CI gate).
        indexes = [models.Index(fields=["last_seen_at"], name="push_subscr_last_se_idx")]

    def __str__(self) -> str:
        return f"PushSubscription(id={self.id}, locale={self.locale}, seen={self.last_seen_at})"
