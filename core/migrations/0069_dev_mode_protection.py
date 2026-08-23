from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0068_add_data_retention_days'),
    ]

    operations = [
        migrations.AddField(
            model_name='sitesettings',
            name='dev_mode_protection',
            field=models.BooleanField(
                default=False,
                help_text='If enabled, users with browser DevTools/Inspector open will see a 404 page with ads.',
            ),
        ),
    ]
