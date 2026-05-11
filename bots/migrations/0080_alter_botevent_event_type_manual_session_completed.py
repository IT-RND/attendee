# Generated manually for BotEventTypes.MANUAL_SESSION_COMPLETED

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0079_bot_mom_guest_token"),
    ]

    operations = [
        migrations.AlterField(
            model_name="botevent",
            name="event_type",
            field=models.IntegerField(
                choices=[
                    (1, "Bot Put in Waiting Room"),
                    (2, "Bot Joined Meeting"),
                    (3, "Bot Recording Permission Granted"),
                    (4, "Meeting Ended"),
                    (5, "Bot Left Meeting"),
                    (6, "Bot requested to join meeting"),
                    (7, "Bot Encountered Fatal error"),
                    (8, "Bot requested to leave meeting"),
                    (9, "Bot could not join meeting"),
                    (10, "Post Processing Completed"),
                    (11, "Data Deleted"),
                    (12, "Bot staged"),
                    (13, "Recording Paused"),
                    (14, "Recording Resumed"),
                    (15, "Bot joined breakout room"),
                    (16, "Bot left breakout room"),
                    (17, "Bot began joining breakout room"),
                    (18, "Bot began leaving breakout room"),
                    (19, "Bot recording permission denied"),
                    (100, "App Session Connection Requested"),
                    (101, "App Session Connected"),
                    (102, "App Session Disconnect Requested"),
                    (103, "App Session Disconnected"),
                    (104, "Manual session completed (user resolved stuck bot)"),
                ]
            ),
        ),
    ]
