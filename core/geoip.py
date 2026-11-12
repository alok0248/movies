"""IP geolocation for the admin panels.

ipwho.is is the source. Results are cached in ``IPLocation`` so that a table full
of repeat addresses costs nothing, and the API is only touched for an address an
admin actually asked about. A stale cache entry is preferred over an error: if
the lookup fails we show the last saved answer instead of a blank cell.
"""
import json
import logging
from datetime import timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.utils import timezone

logger = logging.getLogger(__name__)

API_ROOT = 'https://ipwho.is/'
CACHE_TTL = timedelta(days=30)
TIMEOUT = 6


def _text(value):
    return value if isinstance(value, str) else ''


def summarize(data):
    """Trim an ipwho.is payload down to the fields the UI shows."""
    connection = data.get('connection') if isinstance(data.get('connection'), dict) else {}
    tz = data.get('timezone') if isinstance(data.get('timezone'), dict) else {}
    flag = data.get('flag') if isinstance(data.get('flag'), dict) else {}
    return {
        'ok': bool(data.get('success')),
        'country': _text(data.get('country')),
        'country_code': _text(data.get('country_code')),
        'flag': _text(flag.get('emoji')),
        'continent': _text(data.get('continent')),
        'region': _text(data.get('region')),
        'city': _text(data.get('city')),
        'postal': _text(data.get('postal')),
        'calling_code': _text(data.get('calling_code')),
        'capital': _text(data.get('capital')),
        'borders': _text(data.get('borders')),
        'isp': _text(connection.get('isp')),
        'org': _text(connection.get('org')),
        'domain': _text(connection.get('domain')),
        'asn': connection.get('asn') or '',
        'timezone': _text(tz.get('id')),
        'utc': _text(tz.get('utc')),
        'latitude': data.get('latitude'),
        'longitude': data.get('longitude'),
        'message': _text(data.get('message')),
    }


def cached_rows(ips):
    """{ip: IPLocation} for the addresses already stored, in one query."""
    from .models import IPLocation

    wanted = [ip for ip in set(ips or []) if ip]
    if not wanted:
        return {}
    return {row.ip: row for row in IPLocation.objects.filter(ip__in=wanted)}


def attach(records, ip_key='ip'):
    """Add geo_country/geo_flag to each record from the cache, no network.

    List pages call this before rendering so the country is in the first HTML
    the browser gets; the browser-side fill only ever handles cache misses.
    Works on dicts (report rows) and model instances alike.
    """
    rows = list(records)

    def record_ip(rec):
        if isinstance(rec, dict):
            return rec.get(ip_key) or ''
        return getattr(rec, ip_key, '') or ''

    known = cached_rows({record_ip(rec) for rec in rows})
    for rec in rows:
        info = row_summary(known.get(record_ip(rec))) or {}
        country = info.get('country') or ''
        flag = info.get('flag') or ''
        if isinstance(rec, dict):
            rec['geo_country'] = country
            rec['geo_flag'] = flag
        else:
            rec.geo_country = country
            rec.geo_flag = flag
    return rows


def row_summary(row):
    """Summarize a stored row without touching the network."""
    if row is None:
        return None
    payload = {}
    if row.payload:
        try:
            payload = json.loads(row.payload)
        except (TypeError, ValueError):
            payload = {}
    summary = summarize(payload) if payload else {
        'ok': row.success, 'country': row.country, 'country_code': row.country_code,
        'flag': '', 'city': row.city, 'region': row.region, 'isp': row.isp,
    }
    summary['ok'] = bool(row.success)
    summary['ip'] = row.ip
    summary['cached'] = True
    summary['fetched_at'] = timezone.localtime(row.fetched_at).isoformat(timespec='seconds')
    if payload:
        summary['payload'] = payload
    return summary


def _store(ip, data):
    from .models import IPLocation

    summary = summarize(data)
    IPLocation.objects.update_or_create(
        ip=ip,
        defaults={
            'success': summary['ok'],
            'country': summary['country'],
            'country_code': summary['country_code'],
            'region': summary['region'],
            'city': summary['city'],
            'isp': summary['isp'],
            'payload': json.dumps(data, ensure_ascii=False)[:20000],
        },
    )
    return summary


def lookup(ip, force=False):
    """Geo details for one address: cache first, ipwho.is on a miss.

    Never raises -- the caller always gets a dict with ``ok`` set.
    """
    from .models import IPLocation

    ip = (ip or '').strip()
    if not ip or len(ip) > 64:
        return {'ok': False, 'ip': ip, 'error': 'No IP address to look up.'}

    row = IPLocation.objects.filter(ip=ip).first()
    fresh = bool(row and (timezone.now() - row.fetched_at) < CACHE_TTL)
    if row and fresh and not force:
        return row_summary(row)

    try:
        request = Request(
            API_ROOT + quote(ip, safe=''),
            headers={'User-Agent': 'CineStream-admin/1.0', 'Accept': 'application/json'},
        )
        with urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8', 'replace'))
    except Exception as exc:  # network, timeout, bad JSON, DNS
        logger.warning('ipwho.is lookup failed for %s: %s', ip, exc)
        if row is not None and row.payload:
            fallback = row_summary(row)
            fallback['stale'] = True
            fallback['error'] = 'Lookup failed -- showing the last saved result.'
            return fallback
        return {'ok': False, 'ip': ip, 'error': 'Lookup failed. Try again in a moment.'}

    summary = _store(ip, data)
    summary['ip'] = ip
    summary['cached'] = False
    summary['payload'] = data
    return summary
