"""`{% ip_geo_chip ip %}` -- the country chip for an address.

The chip needs the country in the first HTML, but a page can show hundreds of
rows. Looking the address up in IPLocation per row would be a query per row, so
answers are memoised per thread for a short window: one query per distinct
address per half minute, regardless of how many rows repeat it.
"""

import json
import threading
import time

from django import template

register = template.Library()

_MEMO_TTL = 30.0
_local = threading.local()


def _memo():
    now = time.time()
    store = getattr(_local, 'store', None)
    if store is None or (now - getattr(_local, 'stamp', 0.0)) > _MEMO_TTL:
        store = {}
        _local.store = store
        _local.stamp = now
    return store


def _flag_from(payload):
    if not payload:
        return ''
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return ''
    flag = data.get('flag')
    return (flag.get('emoji') or '') if isinstance(flag, dict) else ''


@register.inclusion_tag('core/_ip_geo.html')
def ip_geo_chip(ip):
    ip = (ip or '').strip()
    if not ip:
        return {'ip': '', 'country': '', 'flag': ''}

    store = _memo()
    entry = store.get(ip)
    if entry is None:
        from ..models import IPLocation

        row = IPLocation.objects.filter(ip=ip).values('success', 'country', 'payload').first()
        entry = {
            'country': (row['country'] or '') if row and row['success'] else '',
            'flag': _flag_from(row['payload']) if row else '',
        }
        store[ip] = entry

    return {'ip': ip, 'country': entry['country'], 'flag': entry['flag']}
