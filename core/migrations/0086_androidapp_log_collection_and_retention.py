from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0085_add_androidapplog'),
    ]

    operations = [
        migrations.AddField(
            model_name='androidapp',
            name='log_collection_enabled',
            field=models.BooleanField(
                default=True,
                help_text="Enable or disable log collection from this app. When disabled, the app's /log/ endpoint will reject incoming logs.",
            ),
        ),
        migrations.AddField(
            model_name='androidapp',
            name='log_retention_days',
            field=models.PositiveIntegerField(
                default=30,
                help_text='Number of days to retain AndroidAppLog entries. Older logs are automatically deleted by the clean_analytics command.',
            ),
        ),
    ]
