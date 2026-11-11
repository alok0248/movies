"""The data-health checks, defined once.

The page and the sidebar badge both read this list, so a check can never be
counted in one place and missing from the other.
"""

from django.db.models import Count
from django.utils import timezone


def check_rows():
    """``(slug, label, count)`` for every check, in page order."""
    from .models import (AndroidAppDevice, AndroidAppDeviceVisit, AndroidAppLog,
                         ErrorLog, SyncedUser, UserHeartbeat)

    now = timezone.now()
    today = timezone.localdate()
    day_ago = now - timezone.timedelta(hours=24)
    month_ago = now - timezone.timedelta(days=90)

    shared = (SyncedUser.objects.exclude(device_id='').values('device_id').order_by()
              .annotate(n=Count('id')).filter(n__gt=1).count())

    return [
        ('expired', 'Expired but still subscribed',
         SyncedUser.objects.filter(is_subscribed=True, valid_until__lt=today).count()),
        ('unverified', 'Accounts not marked verified',
         SyncedUser.objects.filter(is_email_verified=False).count()),
        ('nodevice', 'Accounts with no device id',
         SyncedUser.objects.filter(device_id='').count()),
        ('shared', 'Device ids shared by two accounts', shared),
        ('unlinked', 'Install records with no account link',
         AndroidAppDevice.objects.filter(account_key='').count()),
        ('guests', 'Guest heartbeat rows',
         UserHeartbeat.objects.filter(email__startswith='guest_').count()),
        ('visits', 'Device visits older than 90 days',
         AndroidAppDeviceVisit.objects.filter(visited_at__lt=month_ago).count()),
        ('logs', 'App logs older than 90 days',
         AndroidAppLog.objects.filter(timestamp__lt=month_ago).count()),
        ('errors', 'Errors captured in the last 24 hours',
         ErrorLog.objects.filter(created_at__gte=day_ago).count()),
    ]
