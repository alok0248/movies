"""Persistent capture of every incoming request for the admin request-log page.

ErrorLog already stores failed responses (4xx/5xx). This module stores *all*
traffic — successful page views, Ajax calls and Android API hits included — so
the admin ``/admin-dashboard/requests/`` page can answer "who asked for what,
with which method, from which IP, and how fast".

Recording is best-effort: a failure to log a request must never break the
request itself. For API/Ajax calls the request parameters are stored too, but
values under sensitive names (password, token, OTP, secret, api_key, …) are
masked before they ever reach the database; page and admin traffic is kept
metadata-only.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Field names whose values are masked before a payload is stored. Matching is
# by substring on the lower-cased key, so `password`, `new_password`,
# `accessToken`, `api_key`, `otp`, `authorization`, etc. are all covered.
SENSITIVE_MARKERS = (
    'password', 'passwd', 'pwd', 'pass', 'token', 'secret', 'otp',
    'api_key', 'apikey', 'authorization', 'credential', 'auth_code',
    'private_key', 'session', 'signature',
)

# Cap the stored payload so a huge body cannot bloat the table.
MAX_PAYLOAD_CHARS = 4000
MAX_QUERY_CHARS = 2000

# Keep the table bounded so sustained traffic (and bots) cannot grow it forever.
MAX_ROWS = 5000
# Prune only every Nth write — counting rows on every insert is wasteful.
_PRUNE_EVERY = 200
_writes_since_prune = [0]

# Paths that are not worth recording (static assets, health probes). The
# request-log page itself is skipped too so viewing the log does not keep
# appending its own rows.
SKIP_PREFIXES = (
    '/static/',
    '/media/',
    '/admin-dashboard/health/',
    '/admin-dashboard/requests',
)


def should_record(request):
    """False for paths we never want in the request log."""
    try:
        path = request.path or ''
    except Exception:
        return False
    if path.startswith(SKIP_PREFIXES):
        return False
    # Browser extensions / prefetchers asking for chrome-extension:// URLs.
    return not path.startswith('/chrome-extension')


def categorize(path):
    """Classify a path as 'api', 'admin' or 'page'."""
    p = path or ''
    if p.startswith('/admin-dashboard/') or p.startswith('/admin/'):
        return 'admin'
    if p.startswith('/api/') or p.startswith('/ajax/') or p.startswith('/cineplayer/api/'):
        return 'api'
    return 'page'


def _client_ip(request):
    try:
        xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
        ip = xff.split(',')[0].strip() or request.META.get('REMOTE_ADDR', '')
        return (ip or '')[:64]
    except Exception:
        return ''


def _username(request):
    try:
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            return (user.username or '')[:150]
    except Exception:
        pass
    return ''


def _view_name(request):
    try:
        match = getattr(request, 'resolver_match', None)
        return (getattr(match, 'view_name', '') or '')[:120]
    except Exception:
        return ''


def _is_sensitive(key):
    """True when a parameter name looks like a secret worth masking."""
    try:
        k = str(key).lower()
    except Exception:
        return False
    return any(marker in k for marker in SENSITIVE_MARKERS)


def _redact(obj, depth=0):
    """Recursively mask sensitive values in parsed JSON/form data."""
    if depth > 8:
        return obj
    if isinstance(obj, dict):
        return {k: ('***' if _is_sensitive(k) else _redact(v, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v, depth + 1) for v in obj]
    return obj


def _redact_query(raw):
    """Mask sensitive query-string parameters (e.g. an OTP in the URL)."""
    raw = raw or ''
    if not raw:
        return ''
    try:
        from urllib.parse import parse_qsl, urlencode
        pairs = parse_qsl(raw[:MAX_QUERY_CHARS], keep_blank_values=True)
        if not pairs:
            return raw[:500]
        masked = [(k, '***' if _is_sensitive(k) else v) for k, v in pairs]
        return urlencode(masked)[:500]
    except Exception:
        return raw[:500]


def _extract_payload(request):
    """Return the request parameters as masked JSON text, or '' if not an
    API/Ajax call we should capture. Never raises."""
    try:
        method = (getattr(request, 'method', '') or '').upper()
        if method in ('GET', 'HEAD', 'OPTIONS'):
            return ''
        ctype = (request.META.get('CONTENT_TYPE', '') or '').lower()
        if 'application/json' in ctype:
            raw = request.body  # cached by the view when it parsed the JSON
            if not raw:
                return ''
            data = json.loads(raw.decode('utf-8', 'replace'))
            return json.dumps(_redact(data), ensure_ascii=False)[:MAX_PAYLOAD_CHARS]
        if ('application/x-www-form-urlencoded' in ctype
                or 'multipart/form-data' in ctype):
            data = request.POST.dict()
            if not data:
                return ''
            return json.dumps(_redact(data), ensure_ascii=False)[:MAX_PAYLOAD_CHARS]
    except Exception:
        # Body already consumed, multipart stream, malformed JSON, etc.
        return ''
    return ''


def _response_size(response):
    """Response body size in bytes, from Content-Length when present."""
    if response is None:
        return 0
    try:
        cl = response.get('Content-Length')
        if cl is not None:
            return max(0, int(cl))
    except Exception:
        pass
    try:
        return max(0, len(response.content))
    except Exception:
        return 0


def record_request(request, status_code, duration_ms=0, response=None):
    """Write one request row. Never raises."""
    try:
        if not should_record(request):
            return
        from .models import RequestLog
        path = getattr(request, 'path', '') or ''
        category = categorize(path)
        # Only capture parameters for API/Ajax calls; page and admin traffic
        # keeps the metadata-only record.
        payload = _extract_payload(request) if category == 'api' else ''
        RequestLog.objects.create(
            category=category,
            method=(getattr(request, 'method', '') or '')[:10],
            path=path[:500],
            query=_redact_query(request.META.get('QUERY_STRING', '')),
            status_code=status_code or 0,
            client_ip=_client_ip(request),
            username=_username(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:300],
            referer=(request.META.get('HTTP_REFERER', '') or '')[:500],
            view_name=_view_name(request),
            duration_ms=max(0, int(duration_ms or 0)),
            request_body=payload,
            response_size=_response_size(response),
        )
        _maybe_prune()
    except Exception:
        # The table may not exist yet (before migrations) — stay silent.
        logger.debug('RequestLog: could not record %s', getattr(request, 'path', '?'),
                     exc_info=True)


def _maybe_prune():
    _writes_since_prune[0] += 1
    if _writes_since_prune[0] < _PRUNE_EVERY:
        return
    _writes_since_prune[0] = 0
    prune()


def prune():
    """Drop everything older than the newest MAX_ROWS rows. Never raises."""
    try:
        from .models import RequestLog
        keep = list(
            RequestLog.objects.order_by('-created_at').values_list('id', flat=True)[:MAX_ROWS]
        )
        if len(keep) < MAX_ROWS:
            return
        RequestLog.objects.exclude(id__in=keep).delete()
    except Exception:
        logger.debug('RequestLog: prune failed', exc_info=True)


# Text fields that support a per-column 'contains' filter (the spreadsheet
# view exposes an input for every one of these).
FIELD_FILTERS = (
    'path', 'query', 'client_ip', 'username', 'user_agent', 'referer',
    'view_name', 'request_body',
)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def filter_requests(category=None, method=None, status=None, status_code=None,
                    q=None, min_duration=None, min_size=None, on_date=None,
                    **field_filters):
    """Build the RequestLog queryset for the given filter conditions.

    Every stored field has a condition: ``category``/``method``/``status``
    (class) selects, exact ``status_code``, a ``min_duration`` and ``min_size``
    threshold, an ``on_date`` day, a per-column 'contains' filter for each of
    FIELD_FILTERS, and a free-text ``q`` across all of them.
    """
    from django.db.models import Q
    from .models import RequestLog

    qs = RequestLog.objects.all()
    if category in ('page', 'api', 'admin'):
        qs = qs.filter(category=category)
    if method:
        qs = qs.filter(method=method.upper())
    if status == '2xx':
        qs = qs.filter(status_code__gte=200, status_code__lt=300)
    elif status == '3xx':
        qs = qs.filter(status_code__gte=300, status_code__lt=400)
    elif status == '4xx':
        qs = qs.filter(status_code__gte=400, status_code__lt=500)
    elif status == '5xx':
        qs = qs.filter(status_code__gte=500)

    code = _as_int(status_code)
    if code is not None:
        qs = qs.filter(status_code=code)
    dur = _as_int(min_duration)
    if dur is not None:
        qs = qs.filter(duration_ms__gte=dur)
    size = _as_int(min_size)
    if size is not None:
        qs = qs.filter(response_size__gte=size)
    if on_date:
        try:
            from datetime import datetime
            datetime.strptime(on_date, '%Y-%m-%d')
            qs = qs.filter(created_at__date=on_date)
        except (ValueError, TypeError):
            pass

    for field in FIELD_FILTERS:
        value = field_filters.get(field)
        if value:
            qs = qs.filter(**{f'{field}__icontains': value})

    if q:
        qs = qs.filter(
            Q(path__icontains=q) | Q(query__icontains=q) | Q(client_ip__icontains=q)
            | Q(username__icontains=q) | Q(user_agent__icontains=q)
            | Q(view_name__icontains=q) | Q(referer__icontains=q)
            | Q(request_body__icontains=q)
        )
    return qs


def recent_requests(limit=300, **filters):
    """Newest first, applying any of the filter_requests conditions."""
    return list(filter_requests(**filters)[:limit])


def request_total():
    try:
        from .models import RequestLog
        return RequestLog.objects.count()
    except Exception:
        return 0


def clear_requests():
    """Delete every recorded request (the admin 'Clear' button)."""
    try:
        from .models import RequestLog
        RequestLog.objects.all().delete()
    except Exception:
        logger.debug('RequestLog: clear failed', exc_info=True)


def request_to_dict(rec):
    """Serialize a RequestLog row for the JSON view."""
    try:
        when = rec.created_at.isoformat(timespec='seconds')
    except Exception:
        when = ''
    return {
        'id': rec.id,
        'time': when,
        'category': rec.category,
        'method': rec.method,
        'path': rec.path,
        'query': rec.query,
        'status_code': rec.status_code,
        'client_ip': rec.client_ip,
        'username': rec.username,
        'user_agent': rec.user_agent,
        'referer': rec.referer,
        'view': rec.view_name,
        'duration_ms': rec.duration_ms,
        'request_body': rec.request_body,
        'response_size': rec.response_size,
    }
