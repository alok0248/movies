"""Lift the ``okhttp`` User-Agent rule the bot profile used to create.

'okhttp' was part of BOT_BLOCK_PROFILE, but it is the Android app's own HTTP
client — the app announces itself as ``okhttp/4.x``. Switching the profile on
therefore refused every app request outside ``/api/`` (the movie pages it opens,
its APK download from ``/apps/download/<id>/``), which reads to a user as "the
app is blocked".

The token is gone from the profile now, so it is never offered again, and the
app's own paths are exempt from header rules anyway. This migration switches off
the rule that is already sitting in the database, because changing the profile
alone does not lift rows that were created by an earlier click. The row is kept
(inactive) so the history still shows it was on and who switched it on.

Only a User-Agent rule whose value is exactly the okhttp token is touched; a
longer, operator-written rule that happens to mention okhttp is left alone.
"""

from django.db import migrations


def deactivate_okhttp_rules(apps, schema_editor):
    BlockedHeader = apps.get_model('core', 'BlockedHeader')
    rules = BlockedHeader.objects.filter(is_active=True, header_name='User-Agent')
    for rule in rules:
        if (rule.value or '').strip().lower() in ('okhttp', 'okhttp/'):
            rule.is_active = False
            rule.unblocked_by = 'system: okhttp is the app client'
            rule.save(update_fields=['is_active', 'unblocked_by'])


def noop(apps, schema_editor):
    """Reversing would re-block the app, which is never what anyone wants."""


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0115_alertrecipient_checks_alertsettings_quiet_end_and_more'),
    ]

    operations = [
        migrations.RunPython(deactivate_okhttp_rules, noop),
    ]
