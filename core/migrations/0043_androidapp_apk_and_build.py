from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0042_androidapp_androidappaccesslog'),
    ]

    operations = [
        migrations.AddField(
            model_name='androidapp',
            name='allowed_build_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='androidapp',
            name='apk_file',
            field=models.FileField(blank=True, null=True, upload_to='android_apks/'),
        ),
    ]
