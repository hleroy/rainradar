from django.urls import path

from radar import views

urlpatterns = [
    path("api/radar/frames", views.frames, name="frames"),
    path("api/radar/latest", views.latest, name="latest"),
    path("api/radar/range", views.range_, name="range"),
    # Lightning: SSE live stream + history replay.
    path("api/lightning/stream", views.lightning_stream, name="lightning_stream"),
    path("api/lightning/history", views.lightning_history, name="lightning_history"),
    # Storm alerts: Web Push subscribe/unsubscribe. Flag-gated (404 off).
    path("api/alerts/subscribe", views.alerts_subscribe, name="alerts_subscribe"),
    path("api/alerts/unsubscribe", views.alerts_unsubscribe, name="alerts_unsubscribe"),
    # Canonical provider-scoped tile path. Prod serves this statically from
    # Nginx with a try_files fallback to this same view; dev serves it here directly.
    path(
        "tiles/<str:provider>/<str:date>/<int:ts>/<int:z>/<int:x>/<int:y>.png",
        views.tile,
        name="tile",
    ),
    # About dialog statistics: read-only, Redis-cached JSON.
    path("api/stats", views.stats, name="stats"),
    path("metrics", views.metrics, name="metrics"),
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
]
