"""Give every already-linked install a registered_at date.

Before this, "registered" was worked out at read time from whichever email maps
to the device right now. That is fine for a live list and wrong for history: a
guest who signs in later would turn their own past days into registered ones.
This fills the column in once so the trend lines stop moving, using the device's
first_seen_at as the earliest date we can defend (the account link predates any
later sync we can see).
"""

from django.db import migrations


def backfill(apps, schema_editor):
    AndroidAppDevice = apps.get_model('core', 'AndroidAppDevice')
    SyncedUser = apps.get_model('core', 'SyncedUser')
    UserHeartbeat = apps.get_model('core', 'UserHeartbeat')

    linked = set()
    for model in (SyncedUser, UserHeartbeat):
        for device_id in model.objects.exclude(device_id='').values_list('device_id', flat=True):
            if device_id:
                linked.add(device_id)
    for key in (AndroidAppDevice.objects.exclude(account_key='')
                .values_list('account_key', flat=True)):
        if key:
            linked.add(key)

    if not linked:
        return

    # Chunked so a large table does not build one enormous IN clause.
    linked = list(linked)
    for start in range(0, len(linked), 500):
        chunk = linked[start:start + 500]
        for device in AndroidAppDevice.objects.filter(user_id__in=chunk,
                                                      registered_at__isnull=True):
            device.registered_at = device.first_seen_at
            device.save(update_fields=['registered_at'])


def unbackfill(apps, schema_editor):
    AndroidAppDevice = apps.get_model('core', 'AndroidAppDevice')
    AndroidAppDevice.objects.update(registered_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0137_androidappdevice_block_mode_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
