"""IP geolocation: one path from an address to a saved location.

ipwho.is is the source. Every address the site writes is converted here through
``ensure`` (core/signals.py calls it on save), so a visit, a download, an app
attempt, an audit row and a blocked address all end up with the same answer in
``IPLocation`` -- and the admin pages read that saved answer instead of waiting on
the network. An address with no saved answer is linked to iplocation.io instead.
Results are cached in ``IPLocation`` so that a table full of repeat addresses
costs nothing. A stale cache entry is preferred over an error: if the lookup
fails we show the last saved answer instead of a blank cell.
"""
import ipaddress
import json
import logging
import threading
import time
from datetime import timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.utils import timezone

logger = logging.getLogger(__name__)

API_ROOT = 'https://ipwho.is/'
# Where an admin is sent for an address ipwho.is has no saved answer for.
EXTERNAL_ROOT = 'https://iplocation.io/ip/'
CACHE_TTL = timedelta(days=30)
# An address ipwho.is could not answer is retried after this rather than on
# every write.
FAILED_RETRY = timedelta(hours=6)
TIMEOUT = 6
# Addresses this process checks a moment ago, so writing the same address on
# every request costs one check instead of one per row.
RECENT_TTL = 120.0
RECENT_MAX = 4096
# How many addresses may be queued at once, so a burst of new visitors cannot
# start an unbounded number of threads. An address skipped this way is queued by
# its next row, and the admin pages can still look it up on demand.
MAX_IN_FLIGHT = 12

_in_flight = set()
_recent = {}
_lock = threading.Lock()


def _text(value):
    return value if isinstance(value, str) else ''


def address(ip):
    """The parsed address behind a log line, or None when there is not one."""
    raw = (ip or '').strip()
    if not raw or len(raw) > 64:
        return None
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        pass
    # Some logs keep the source port ("1.2.3.4:5678"); the address is still there.
    host, _, port = raw.partition(':')
    if host and port.isdigit():
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            return None
    return None


def is_public(ip):
    """True for the addresses ipwho.is can actually answer.

    LAN, loopback, link-local, reserved and multicast ranges are not on the
    internet, so no API call is spent on them: the admin page links those to
    iplocation.io instead.
    """
    addr = address(ip)
    if addr is None:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def external_url(ip):
    """The iplocation.io page for an address we have no saved answer for."""
    ip = (ip or '').strip()[:64]
    return (EXTERNAL_ROOT + quote(ip, safe='')) if ip else ''


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
        'flag_img': _text(flag.get('img')),
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


def _warm(ip, force=False):
    """Look one address up in the background, store it, stop tracking it."""
    try:
        lookup(ip, force=force)
    except Exception as exc:  # never surface a lookup failure
        logger.debug('background ip lookup failed for %s: %s', ip, exc)
    finally:
        try:
            from django.db import connections
            connections.close_all()
        except Exception:
            pass
        with _lock:
            _in_flight.discard(ip)


def ensure(ip, force=False):
    """Convert an address that was just written into a saved location.

    This is the single path from "an address exists" to "its location is in the
    database": core/signals.py calls it for every row saved with an address, so
    the admin pages find the answer stored instead of looking it up themselves.

    The ipwho.is call runs on a background thread, so nothing waits on the
    network: a stored answer younger than ``CACHE_TTL`` is reused, an address
    that failed is retried at most every ``FAILED_RETRY``, the same address is
    never looked up twice at once, and an address this process checked in the
    last couple of minutes is left alone. Private and loopback addresses are
    skipped -- the API has nothing to say about them, and the admin page links
    those to iplocation.io.

    Returns True when a lookup was queued.
    """
    from .models import IPLocation

    ip = (ip or '').strip()[:64]
    if not ip or not is_public(ip):
        return False

    now = time.monotonic()
    if not force:
        seen = _recent.get(ip)
        if seen is not None and (now - seen) < RECENT_TTL:
            return False
        row = IPLocation.objects.filter(ip=ip).first()
        if row is not None:
            age = timezone.now() - row.fetched_at
            if age < (CACHE_TTL if row.success else FAILED_RETRY):
                _recent[ip] = now
                return False

    with _lock:
        if ip in _in_flight or len(_in_flight) >= MAX_IN_FLIGHT:
            return False
        _in_flight.add(ip)
        if len(_recent) > RECENT_MAX:
            _recent.clear()
        _recent[ip] = now

    threading.Thread(target=_warm, args=(ip, force), daemon=True).start()
    return True
