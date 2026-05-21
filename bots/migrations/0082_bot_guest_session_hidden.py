from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bots", "0081_project_meeting_platform_flags"),
    ]

    operations = [
        migrations.AddField(
            model_name="bot",
            name="guest_session_hidden",
            field=models.BooleanField(
                default=False,
                help_text="When set, this session is hidden from the public guest sessions page.",
            ),
        ),
    ]
