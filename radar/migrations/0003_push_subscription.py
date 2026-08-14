"""Create the ``push_subscription`` table for background storm alerts.

Expand-only (backward-compatible): adds one standalone, ORM-managed table and
touches nothing else, so it is safe to apply while the previous release is still
running (deploy rules require expand/contract).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("radar", "0002_lightning_strike"),
    ]

    operations = [
        migrations.CreateModel(
            name="PushSubscription",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("endpoint", models.TextField(unique=True)),
                ("p256dh", models.CharField(max_length=200)),
                ("auth", models.CharField(max_length=200)),
                ("lat", models.FloatField()),
                ("lon", models.FloatField()),
                ("locale", models.CharField(default="en", max_length=5)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "push_subscription",
                "indexes": [
                    models.Index(fields=["last_seen_at"], name="push_subscr_last_se_idx"),
                ],
            },
        ),
    ]
