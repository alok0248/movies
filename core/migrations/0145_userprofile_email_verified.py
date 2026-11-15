import django.utils.timezone
from django.db import migrations, models


def grandparent_active_profiles(apps, schema_editor):
    """Treat existing profiles of already-active accounts as verified.

    The new rule refuses sign-in until the address is confirmed, but every
    account that could sign in before this migration was trusted already.
    Without this pass, any user who happened to open their profile page
    (which is what creates the row) would be locked out the moment the rule
    went live. Only accounts created inactive afterwards start unverified.
    """
    UserProfile = apps.get_model('core', 'UserProfile')
    User = apps.get_model('auth', 'User')
    alias = schema_editor.connection.alias
    active_ids = list(
        User.objects.using('default').filter(is_active=True).values_list('id', flat=True)
    )
    if not active_ids:
        return
    UserProfile.objects.using(alias).filter(
        user_id__in=active_ids, email_verified=False,
    ).update(email_verified=True, email_verified_at=django.utils.timezone.now())


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0144_sitesettings_require_login_to_browse"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="email_verified",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="email_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(grandparent_active_profiles, migrations.RunPython.noop),
    ]
