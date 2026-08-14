"""Archive models — upsert/status transitions and gap lifecycle."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest

from radar.models import ArchiveGap
from radar.models import RadarFrame

pytestmark = pytest.mark.django_db


def test_radar_frame_create_and_str():
    frame = RadarFrame.objects.create(
        timestamp=1683790200,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    assert frame.collected_at is not None  # auto_now_add
    assert "1683790200" in str(frame)
    # The PK is now a surrogate id; identity is (provider, timestamp).
    assert RadarFrame.objects.get(provider="rainviewer", timestamp=1683790200).status == "ok"


def test_radar_frame_upsert_by_provider_timestamp_updates_status():
    # A partial frame is later retried and now complete -> updated toward ok.
    RadarFrame.objects.create(
        timestamp=1683790200,
        provider="rainviewer",
        tile_count=60,
        status="partial",
        missing=[{"z": 7, "x": 1, "y": 2}],
    )
    obj, created = RadarFrame.objects.update_or_create(
        provider="rainviewer",
        timestamp=1683790200,
        defaults={"tile_count": 62, "status": "ok", "missing": []},
    )
    assert created is False
    assert RadarFrame.objects.count() == 1
    obj.refresh_from_db()
    assert obj.status == "ok"
    assert obj.tile_count == 62
    assert obj.missing == []


def test_radar_frame_same_timestamp_two_providers_coexist():
    # Both providers can land on a shared 600 s boundary; (provider, timestamp) is
    # the identity, so the same epoch under two providers is two distinct rows.
    for name in ("rainviewer", "meteofrance"):
        RadarFrame.objects.create(
            timestamp=1683790200,
            provider=name,
            tile_count=62,
            status="ok",
            missing=[],
        )
    assert RadarFrame.objects.filter(timestamp=1683790200).count() == 2


def test_radar_frame_missing_jsonfield_roundtrips():
    missing = [{"z": 7, "x": 1, "y": 2}, {"z": 6, "x": 3, "y": 4}]
    RadarFrame.objects.create(
        timestamp=1,
        provider="rainviewer",
        tile_count=60,
        status="partial",
        missing=missing,
    )
    assert RadarFrame.objects.get(provider="rainviewer", timestamp=1).missing == missing


def test_archive_gap_create_open_then_close():
    start = datetime(2024, 6, 20, 10, 0, tzinfo=UTC)
    gap = ArchiveGap.objects.create(
        service="radar",
        gap_start=start,
        gap_end=None,
        reason="upstream unreachable",
        detail={},
    )
    assert gap.gap_end is None
    assert "ongoing" in str(gap)

    end = datetime(2024, 6, 20, 11, 0, tzinfo=UTC)
    gap.gap_end = end
    gap.save(update_fields=["gap_end"])
    gap.refresh_from_db()
    assert gap.gap_end == end
    assert "closed" in str(gap)


def test_archive_gap_upsert_by_service_and_start_is_idempotent():
    start = datetime(2024, 6, 20, 10, 0, tzinfo=UTC)
    for _ in range(2):
        ArchiveGap.objects.update_or_create(
            service="radar",
            gap_start=start,
            defaults={
                "gap_end": datetime(2024, 6, 20, 11, 0, tzinfo=UTC),
                "reason": "archiver outage exceeded RainViewer backfill window",
                "detail": {"prev_max": 1, "oldest_up": 2},
            },
        )
    assert ArchiveGap.objects.filter(service="radar", gap_start=start).count() == 1
