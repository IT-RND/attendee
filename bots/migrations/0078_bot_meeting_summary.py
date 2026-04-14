import bots.storage
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0077_alter_participantevent_event_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='bot',
            name='meeting_summary',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='bot',
            name='meeting_summary_pdf',
            field=models.FileField(blank=True, null=True, storage=bots.storage.StorageAlias('recordings'), upload_to=''),
        ),
    ]
