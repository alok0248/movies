"""Device <-> IP history for the admin.

Every request that reaches the site with an address is recorded here as a
(device, IP) pair: who was signed in, what the address was (and later, from
ipwho.is, where it is), and when it was first and last seen. A device that
changes network keeps both rows, so the admin can see every address a phone or
browser has used, and every device and address a single account has used.

The country is fetched as the traffic arrives -- a lookup is queued for any
address that is not cached yet -- so the admin pages can render the country from
the database without waiting on the network.

Recording is best-effort, exactly like the request log: nothing here may raise
into the request that triggered it.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

# The lookup itself lives in core/geoip.py (``geoip.ensure``): one path converts
# an address to a stored location, whether it arrives on this table, on a request
# log row or on any other row that carries an address.

PLATFORM_BY_PREFIX = (
    ('/api/', 'android'),
    ('/ajax/', 'web'),
)


def client_ip(request):
    """Canonical address for a request: first X-Forwarded-For hop, else peer."""
    try:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        raw = forwarded.split(',')[0] if forwarded else request.META.get('REMOTE_ADDR', '')
    except Exception:
        return ''
    return (raw or '').strip()[:64]


def device_key(request):
    """Stable identity for the device behind a request.

    Apps send X-Device-Id (or a device_id parameter); the website is identified
    by its visitor cookie, and anything else falls back to the session key so an
    anonymous browser is still one device across its requests.
    """
    try:
        for header in ('HTTP_X_DEVICE_ID', 'HTTP_X_DEVICE_ID_HASH', 'HTTP_X_INSTALL_ID'):
            value = request.META.get(header)
            if value:
                return ('dev-' + str(value))[:100]
        for source in (getattr(request, 'POST', None), getattr(request, 'GET', None)):
            if source:
                for name in ('device_id', 'deviceId', 'install_id'):
                    value = source.get(name)
                    if value:
                        return ('dev-' + str(value))[:100]
        visitor = request.COOKIES.get('visitor_id')
        if visitor:
            return ('web-' + str(visitor))[:100]
        session = getattr(request, 'session', None)
        if session is not None and session.session_key:
            return ('ses-' + session.session_key)[:100]
    except Exception:
        pass
    return ''


def _platform(request):
    path = getattr(request, 'path', '') or ''
    for prefix, name in PLATFORM_BY_PREFIX:
        if path.startswith(prefix):
            return name
    return 'web'


def ensure_lookup(ip, force=False):
    """Queue the ipwho.is lookup for an address that is not saved yet."""
    try:
        from . import geoip

        geoip.ensure(ip, force=force)
    except Exception:
        pass


def record(request, user=None):
    """Upsert the (device, IP) row for this request and warm its country."""
    from .models import DeviceIPHistory

    ip = client_ip(request)
    if not ip:
        return None
    device = device_key(request)
    account = user if user is not None and getattr(user, 'is_authenticated', False) else None
    username = getattr(account, 'username', '') or ''
    user_agent = (request.META.get('HTTP_USER_AGENT') or '')[:400]
    now = timezone.now()

    row = DeviceIPHistory.objects.filter(device_id=device, ip=ip).first()
    if row is None:
        row = DeviceIPHistory.objects.create(
            device_id=device, ip=ip, user=account, username=username,
            user_agent=user_agent, platform=_platform(request), hits=1,
            first_seen=now, last_seen=now,
        )
    else:
        # Cheap update: no save() so concurrent requests cannot clobber hits.
        DeviceIPHistory.objects.filter(pk=row.pk).update(
            last_seen=now, hits=row.hits + 1,
            username=username or row.username,
            user=account or row.user,
            user_agent=user_agent or row.user_agent,
        )

    ensure_lookup(ip)
    return row
