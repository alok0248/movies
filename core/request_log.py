"""Persistent capture of every incoming request for the admin request-log page.

ErrorLog already stores failed responses (4xx/5xx). This module stores *all*
traffic — successful page views, Ajax calls and Android API hits included — so
the admin ``/admin-dashboard/requests/`` page can answer "who asked for what,
with which method, from which IP, and how fast".

Recording is best-effort: a failure to log a request must never break the
request itself. Request bodies are deliberately never stored — they can
contain passwords.
"""

import logging

logger = logging.getLogger(__name__)

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


def record_request(request, status_code, duration_ms=0):
    """Write one request row. Never raises."""
    try:
        if not should_record(request):
            return
        from .models import RequestLog
        path = getattr(request, 'path', '') or ''
        RequestLog.objects.create(
            category=categorize(path),
            method=(getattr(request, 'method', '') or '')[:10],
            path=path[:500],
            query=(request.META.get('QUERY_STRING', '') or '')[:500],
            status_code=status_code or 0,
            client_ip=_client_ip(request),
            username=_username(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:300],
            referer=(request.META.get('HTTP_REFERER', '') or '')[:500],
            view_name=_view_name(request),
            duration_ms=max(0, int(duration_ms or 0)),
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


def recent_requests(limit=300, category=None, method=None, status=None, q=None):
    """Newest requests first, optionally filtered by category ('page'/'api'/
    'admin'), HTTP method, status class ('2xx'/'3xx'/'4xx'/'5xx') and a
    free-text query."""
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
    if q:
        qs = qs.filter(
            Q(path__icontains=q) | Q(query__icontains=q) | Q(client_ip__icontains=q)
            | Q(username__icontains=q) | Q(user_agent__icontains=q)
            | Q(view_name__icontains=q) | Q(referer__icontains=q)
        )
    return list(qs[:limit])


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
    }
