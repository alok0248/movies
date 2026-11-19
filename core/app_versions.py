"""Build identity of an app install, its version row, and version-scoped switches.

The app sends its build twice. ``X-Android-Build`` is the legacy value the
endpoint allow-list matches against, so it must never be reinterpreted here.
Newer builds also send ``X-App-Version-Code`` (the real versionCode) and
``X-App-Version-Name`` (the real versionName); those are read first, which lets
an operator scope a switch to a precise build without touching the allow-list.

Nothing in this module raises: the config endpoint must keep answering even when
the database or a header is unusable.
"""

from datetime import timedelta

from django.utils import timezone

# One row update per device per hour is enough for "who is on which build";
# a write on every config poll would be pure load with no extra information.
_TOUCH_INTERVAL = timedelta(hours=1)


def version_identity(request, fallback_build=''):
    """``(build_number, version_name)`` reported by this request.

    ``build_number`` is 0 when the request does not say, and 0 is deliberately
    never matched by a policy: an install whose build is unknown must keep
    working rather than be switched off by a rule it cannot be compared to.
    """
    code = (request.headers.get('X-App-Version-Code') or '').strip()
    if not code:
        code = (fallback_build or request.headers.get('X-Android-Build') or '').strip()
    try:
        build = int(str(code).lstrip('#'))
    except (TypeError, ValueError):
        build = 0
    name = (request.headers.get('X-App-Version-Name') or '').strip()[:64]
    return build, name


def features_for_build(android_app, build):
    """The ``features`` object to send this caller, or ``{}`` when no rule matches."""
    try:
        from .models import AppVersionPolicy
        return AppVersionPolicy.features_for(android_app, build)
    except Exception:
        return {}


def record_version(android_app, build, version_name='', device_id='', email=''):
    """Remember which build this device runs. Best effort, never raises."""
    if android_app is None:
        return None
    try:
        from .models import AppVersionDevice
        build = int(build or 0)
    except (TypeError, ValueError):
        build = 0
    except Exception:
        return None
    device = (device_id or '').strip()[:255] or 'unknown'
    version_name = (version_name or '').strip()[:64]
    email = (email or '').strip()[:254]
    now = timezone.now()
    try:
        row, created = AppVersionDevice.objects.get_or_create(
            android_app=android_app,
            build_code=build,
            device_id=device,
            defaults={
                'version_name': version_name,
                'email': email,
                'last_seen': now,
                'requests': 1,
            },
        )
        if created:
            return row
        changed = []
        if version_name and row.version_name != version_name:
            row.version_name = version_name
            changed.append('version_name')
        if email and row.email != email:
            row.email = email
            changed.append('email')
        if changed or (now - row.last_seen) > _TOUCH_INTERVAL:
            row.last_seen = now
            row.requests = (row.requests or 0) + 1
            row.save(update_fields=list(dict.fromkeys(changed + ['last_seen', 'requests'])))
        return row
    except Exception:
        return None


def version_summary(android_app=None, limit=40):
    """Recent builds with how many devices/accounts are on each.

    One aggregate query on the version table, ordered by the newest sighting, so
    the admin page answers "is the old build emptying out yet?" at a glance.
    """
    from django.db.models import Count, Max, Min, Q

    from .models import AppVersionDevice

    rows = AppVersionDevice.objects.all()
    if android_app is not None:
        rows = rows.filter(android_app=android_app)
    try:
        return list(
            rows.values('android_app__name', 'android_app_id', 'build_code', 'version_name')
            .annotate(
                devices=Count('id'),
                accounts=Count('id', filter=~Q(email='')),
                requests=Count('id'),
                first_seen=Min('first_seen'),
                last_seen=Max('last_seen'),
            )
            .order_by('-last_seen')[:limit]
        )
    except Exception:
        return []
