from django.db import migrations

#: The first AdMob banner unit the app should serve. It is created here so a
#: fresh install has a working unit the moment the feature ships; every other
#: unit is added from the Ads page and needs no migration.
DEFAULT_UNIT = {
    'ad_unit_id': 'ca-app-pub-3676720856033816/3976174493',
    'name': 'testing',
    'placement': 'home',
    'refresh_seconds': 60,
    'is_active': True,
    'order': 0,
}


def seed_default_unit(apps, schema_editor):
    AdUnit = apps.get_model('core', 'AdUnit')
    AdUnit.objects.get_or_create(
        ad_unit_id=DEFAULT_UNIT['ad_unit_id'],
        defaults={key: value for key, value in DEFAULT_UNIT.items() if key != 'ad_unit_id'},
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0155_adunit'),
    ]

    operations = [
        migrations.RunPython(seed_default_unit, migrations.RunPython.noop),
    ]
