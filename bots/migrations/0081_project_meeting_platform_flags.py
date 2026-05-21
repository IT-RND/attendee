from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0080_alter_botevent_event_type_manual_session_completed"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="is_zoom_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="project",
            name="is_google_meet_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="project",
            name="is_teams_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
