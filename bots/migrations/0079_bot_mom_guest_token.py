import secrets

from django.db import migrations, models


def backfill_mom_guest_tokens(apps, schema_editor):
    Bot = apps.get_model("bots", "Bot")
    for bot in Bot.objects.filter(mom_guest_token__isnull=True):
        for _ in range(12):
            tok = secrets.token_urlsafe(32)
            if not Bot.objects.filter(mom_guest_token=tok).exists():
                bot.mom_guest_token = tok
                bot.save(update_fields=["mom_guest_token"])
                break


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0078_bot_meeting_summary"),
    ]

    operations = [
        migrations.AddField(
            model_name="bot",
            name="mom_guest_token",
            field=models.CharField(blank=True, editable=False, help_text="Secret segment for guest MoM link (no login).", max_length=64, null=True, unique=True),
        ),
        migrations.RunPython(backfill_mom_guest_tokens, migrations.RunPython.noop),
    ]
