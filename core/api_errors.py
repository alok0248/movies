"""
In-memory capture of recent Android API exceptions for ops visibility.

The ring buffer lives in this process's memory (one buffer per server worker),
so it resets on restart. That is intentional: it is a debugging aid for
"what just went wrong", not a persistent audit log. Production 500s on the
Android endpoints surface here as full tracebacks without digging through
log files.

Deliberately does NOT store request bodies — they contain passwords.
"""
import traceback
from collections import deque
from datetime import datetime

MAX_RECORDED = 25
_records = deque(maxlen=MAX_RECORDED)
_next_id = [1]


def record_api_error(view_name, request, exc):
    """Store an exception raised inside a guarded Android API endpoint.

    Never raises: capture must not break the request path.
    """
    try:
        tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        ip = ''
        try:
            xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
            ip = xff.split(',')[0].strip() or request.META.get('REMOTE_ADDR', '')
        except Exception:
            pass
        _records.appendleft({
            'id': _next_id[0],
            'time': datetime.now().isoformat(timespec='seconds'),
            'view': view_name,
            'method': request.method,
            'path': request.path,
            'client_ip': ip or '',
            'error_type': type(exc).__name__,
            'message': str(exc)[:500],
            'traceback': tb,
        })
        _next_id[0] += 1
    except Exception:
        pass


def recent_errors(limit=None):
    if limit is None:
        return list(_records)
    return list(_records)[:limit]


def clear_errors():
    _records.clear()