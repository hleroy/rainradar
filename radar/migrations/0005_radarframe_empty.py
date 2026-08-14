"""Record the tiles a frame had nothing to draw for.

Expand-phase only: an additive column with a **database-level** default, so old
containers still INSERTing without it during the deploy window succeed.
"""

from django.db import migrations, models
from django.db.models import JSONField, Value


class Migration(migrations.Migration):
    dependencies = [
        ("radar", "0004_provider_scoped_frames"),
    ]

    operations = [
        migrations.AddField(
            model_name="radarframe",
            name="empty",
            field=models.JSONField(db_default=Value([], JSONField()), default=list),
        ),
    ]
