"""Read-only admin for ops visibility into the archive."""

from __future__ import annotations

from django.contrib import admin

from radar.models import ArchiveGap
from radar.models import RadarFrame


@admin.register(RadarFrame)
class RadarFrameAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "provider", "collected_at", "tile_count", "status")
    list_filter = ("provider", "status")
    ordering = ("-timestamp",)
    readonly_fields = ("timestamp", "provider", "collected_at", "tile_count", "status", "missing")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ArchiveGap)
class ArchiveGapAdmin(admin.ModelAdmin):
    list_display = ("id", "service", "gap_start", "gap_end", "reason")
    list_filter = ("service",)
    ordering = ("-gap_start",)
    readonly_fields = ("service", "gap_start", "gap_end", "reason", "detail")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
