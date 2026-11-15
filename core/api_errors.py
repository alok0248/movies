"""Persistent capture of API and server errors for the admin error page.

Every failed response (4xx/5xx) is written to the ErrorLog table so the admin
``/admin-dashboard/api-errors/`` page can show Android-API exceptions and
ordinary page errors side by side, and so the history survives worker restarts
(an in-memory buffer would reset on every restart and only ever held the
records of whichever gunicorn worker served the page).

Recording is always best-effort: a failure to log an error must never break
the request that hit it.

Request bodies are deliberately never stored — they can contain passwords.
"""

import logging
import traceback

logger = logging.getLogger(__name__)

# Keep the table bounded so bot 404 traffic cannot grow it without limit.
MAX_ROWS = 2000
# Prune only every Nth write — counting rows on every insert is wasteful.
_PRUNE_EVERY = 200
_writes_since_prune = [0]

# Endpoints that are not worth recording (static assets, health probes).
SKIP_PREFIXES = ('/static/', '/media/', '/admin-dashboard/health/')

# A refusal from one of these is the security doing its job, not a fault. The
# app's own user agent is checked first and always keeps its row.
BOT_UA_MARKERS = (
    'bot', 'crawler', 'spider', 'slurp', 'baidu', 'yandex', 'bing', 'duckduck',
    'ahrefs', 'semrush', 'mj12', 'petal', 'headless', 'phantomjs', 'scrapy',
    'python-requests', 'python-urllib', 'aiohttp', 'curl/', 'wget',
    'go-http-client', 'libwww', 'java/', 'node-fetch', 'axios',
)
SCANNER_UA_MARKERS = ('okhttp', 'httpclient', 'apache-httpclient', 'dart:io', 'okgo')
# Ajax endpoints a cached page or a service worker can replay without a token.
CSRF_REPLAY_PREFIXES = ('/ajax/', '/sw-proxy')


def user_agent_family(request):
    """'app', 'scanner', 'bot', 'browser' or 'none'."""
    try:
        ua = (request.META.get('HTTP_USER_AGENT') or '').strip().lower()
    except Exception:
        return 'none'
    if not ua:
        return 'none'
    if 'cinestream' in ua:
        return 'app'
    if any(marker in ua for marker in SCANNER_UA_MARKERS):
        return 'scanner'
    if any(marker in ua for marker in BOT_UA_MARKERS):
        return 'bot'
    if 'mozilla' in ua:
        return 'browser'
    return 'other'


def is_suppressed_refusal(request, status_code):
    """True for 403/405 refusals that should be counted, not logged.

    The app keeps every 4xx it triggers: a phone being refused is worth seeing.
    Everyone else's CSRF and method refusals are noise — thousands a day — and
    are counted in SuppressedRequest instead.
    """
    try:
        status_code = int(status_code or 0)
    except Exception:
        return False
    if status_code not in (403, 405):
        return False
    family = user_agent_family(request)
    if family == 'app':
        return False
    if status_code == 405:
        # The app POSTs its endpoints; a GET is a scanner probing them.
        return True
    if family in ('bot', 'scanner', 'none', 'other'):
        return True
    # Browser + 403 is a stale page or a service worker replaying an Ajax call
    # without a token. A signed-in browser being refused elsewhere (a real
    # 403 from a view) still gets its row.
    try:
        path = request.path or ''
        signed_in = getattr(getattr(request, 'user', None), 'is_authenticated', False)
    except Exception:
        return False
    return (not signed_in) and path.startswith(CSRF_REPLAY_PREFIXES)


def _count_suppressed(request, status_code):
    """Increment today's counter for this refusal. Never raises."""
    try:
        from django.db import IntegrityError
        from django.db.models import F
        from django.utils import timezone
        from .models import SuppressedRequest

        day = timezone.localdate()
        family = user_agent_family(request)
        path = (getattr(request, 'path', '') or '')[:300]
        key = {'day': day, 'status_code': int(status_code),
               'family': family, 'path': path}
        if not SuppressedRequest.objects.filter(**key).update(hits=F('hits') + 1):
            try:
                SuppressedRequest.objects.create(hits=1, **key)
            except IntegrityError:
                SuppressedRequest.objects.filter(**key).update(hits=F('hits') + 1)
    except Exception:
        logger.debug('SuppressedRequest: could not count %s', status_code, exc_info=True)


def prune_suppressed(days=45):
    """Drop counters older than the retention window."""
    try:
        from datetime import timedelta
        from django.utils import timezone
        from .models import SuppressedRequest
        SuppressedRequest.objects.filter(
            day__lt=timezone.localdate() - timedelta(days=days)).delete()
    except Exception:
        logger.debug('SuppressedRequest: prune failed', exc_info=True)


def should_record(request):
    """False for paths we never want in the error log."""
    try:
        path = request.path or ''
    except Exception:
        return False
    if path.startswith(SKIP_PREFIXES):
        return False
    return True


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


def record_error(request, kind, status_code, view_name='', error_type='',
                 message='', tb=''):
    """Write one error row. Never raises.

    Marks the request so the same failure is not recorded twice — a guarded
    API view records the exception, then the error middleware also sees the
    500 response it returned.
    """
    try:
        try:
            request._error_log_recorded = True
        except Exception:
            pass
        if not should_record(request):
            return
        if is_suppressed_refusal(request, status_code):
            _count_suppressed(request, status_code)
            return
        from .models import ErrorLog
        ErrorLog.objects.create(
            kind=kind,
            status_code=status_code or 500,
            view_name=(view_name or _view_name(request) or '')[:120],
            method=(getattr(request, 'method', '') or '')[:10],
            path=(getattr(request, 'path', '') or '')[:500],
            query=(request.META.get('QUERY_STRING', '') or '')[:500],
            client_ip=_client_ip(request),
            username=_username(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:300],
            error_type=(error_type or '')[:160],
            message=(message or '')[:4000],
            traceback=(tb or '')[:20000],
        )
        _maybe_prune()
    except Exception:
        logger.debug('ErrorLog: could not record %s %s', status_code, kind, exc_info=True)


def record_api_error(view_name, request, exc):
    """Store an exception raised inside a guarded Android API endpoint.

    Kept as the entry point used by the API view guard.
    """
    tb = ''
    try:
        tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        tb = ''
    record_error(
        request, kind='api', status_code=500,
        view_name=view_name,
        error_type=type(exc).__name__,
        message=str(exc),
        tb=tb,
    )


def _maybe_prune():
    _writes_since_prune[0] += 1
    if _writes_since_prune[0] < _PRUNE_EVERY:
        return
    _writes_since_prune[0] = 0
    prune()
    prune_suppressed()


def prune():
    """Drop everything older than the newest MAX_ROWS rows. Never raises."""
    try:
        from .models import ErrorLog
        keep = list(
            ErrorLog.objects.order_by('-created_at').values_list('id', flat=True)[:MAX_ROWS]
        )
        if len(keep) < MAX_ROWS:
            return
        ErrorLog.objects.exclude(id__in=keep).delete()
    except Exception:
        logger.debug('ErrorLog: prune failed', exc_info=True)


def error_queryset(kind=None, status=None, q=None):
    """The filtered ErrorLog queryset, newest first.

    Shared by ``recent_errors`` (the JSON export) and the admin page's pager,
    which walks it a page at a time so one response never carries every
    traceback at once.
    """
    from django.db.models import Q
    from .models import ErrorLog

    qs = ErrorLog.objects.all()
    if kind in ('api', 'server'):
        qs = qs.filter(kind=kind)
    if status == '4xx':
        qs = qs.filter(status_code__gte=400, status_code__lt=500)
    elif status == '5xx':
        qs = qs.filter(status_code__gte=500)
    if q:
        qs = qs.filter(
            Q(path__icontains=q) | Q(message__icontains=q)
            | Q(error_type__icontains=q) | Q(view_name__icontains=q)
        )
    # The id breaks ties between rows written in the same second, so a page
    # boundary can never show one twice or skip it.
    return qs.order_by('-created_at', '-id')


def recent_errors(limit=300, kind=None, status=None, q=None):
    """Newest errors first, optionally filtered by kind ('api'/'server'),
    status class ('4xx'/'5xx') and a free-text query."""
    return list(error_queryset(kind=kind, status=status, q=q)[:limit])


def error_total():
    try:
        from .models import ErrorLog
        return ErrorLog.objects.count()
    except Exception:
        return 0


def clear_errors():
    """Delete every recorded error (the admin 'Clear' button)."""
    try:
        from .models import ErrorLog
        ErrorLog.objects.all().delete()
    except Exception:
        logger.debug('ErrorLog: clear failed', exc_info=True)


def error_to_dict(rec):
    """Serialize an ErrorLog row for the JSON view."""
    try:
        when = rec.created_at.isoformat(timespec='seconds')
    except Exception:
        when = ''
    return {
        'id': rec.id,
        'time': when,
        'kind': rec.kind,
        'status_code': rec.status_code,
        'view': rec.view_name,
        'method': rec.method,
        'path': rec.path,
        'query': rec.query,
        'client_ip': rec.client_ip,
        'username': rec.username,
        'user_agent': rec.user_agent,
        'error_type': rec.error_type,
        'message': rec.message,
        'traceback': rec.traceback,
    }
