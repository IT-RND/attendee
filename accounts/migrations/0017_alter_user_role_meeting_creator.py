# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0016_backfill_is_managed_zoom_oauth_enabled"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[
                    ("admin", "Admin"),
                    ("regular_user", "Regular User"),
                    ("meeting_creator", "Meeting Creator"),
                ],
                default="admin",
                max_length=255,
            ),
        ),
    ]
