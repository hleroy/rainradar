from django.apps import AppConfig


class RadarConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "radar"

    def ready(self) -> None:
        # Fail fast at startup on a RADAR_PROVIDER that is unknown or not enabled,
        # rather than on the first request that needs it.
        from radar.providers import get_active_provider  # noqa: PLC0415

        get_active_provider()
