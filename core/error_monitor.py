"""Error monitoring middleware — emails admins when a 500 error occurs.

Catches unhandled exceptions during request processing and sends an email
with the full traceback, request context, and server info so crashes are
visible to the admin before users report them.

Rate-limited to at most one email per unique URL path per 5 minutes to
avoid flooding inboxes during sustained errors.
"""

import http
import logging
import traceback
import threading
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('error_monitor')

# Rate-limit: one email per URL path per 5 minutes
_RATE_LIMIT = timedelta(minutes=5)
_last_alerts = {}  # path -> datetime of last alert sent
_last_lock = threading.Lock()


def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def _rate_limited(path):
    """Return True if we already sent an alert for this path recently."""
    now = timezone.now()
    with _last_lock:
        last = _last_alerts.get(path)
        if last and (now - last) < _RATE_LIMIT:
            return True
        _last_alerts[path] = now
        # Prune old entries
        cutoff = now - _RATE_LIMIT * 2
        stale = [k for k, v in _last_alerts.items() if v < cutoff]
        for k in stale:
            _last_alerts.pop(k, None)
        return False


def _send_alert_email(request, exc_type, exc_value, tb_text):
    """Send an email about a server error using the project's SMTP config."""
    try:
        from core.models import EmailAddress, EmailSendLog
        from django.core.mail import EmailMessage as DjangoEmailMessage
        from django.utils import timezone as tz

        # Find the configured "from" email address
        smtp_addr = EmailAddress.objects.filter(is_active=True).first()
        if not smtp_addr:
            logger.error('ErrorMonitor: No active EmailAddress configured — cannot send alert')
            return False

        # Get admin recipients
        from django.contrib.auth.models import User
        admins = list(
            User.objects.filter(is_staff=True, is_active=True, email__contains='@')
            .values_list('email', flat=True)[:5]
        )
        if not admins:
            admins = [smtp_addr.email]  # Fallback: send to the SMTP sender

        method = request.method
        path = request.path
        ip = _get_client_ip(request)
        user = getattr(request, 'user', None)
        user_str = f'{user.username} (id={user.id})' if user and user.is_authenticated else 'Anonymous'
        try:
            host = request.get_host()
        except Exception:
            host = request.META.get('SERVER_NAME', 'unknown')
        ua = (request.META.get('HTTP_USER_AGENT', '') or '')[:120]

        subject = f'[500] {method} {path} — {exc_type.__name__}'
        body = (
            f'SERVER ERROR REPORT\n'
            f'{"=" * 60}\n\n'
            f'Time:       {tz.now().strftime("%Y-%m-%d %H:%M:%S %Z")}\n'
            f'URL:        {method} https://{host}{path}\n'
            f'IP:         {ip}\n'
            f'User:       {user_str}\n'
            f'User-Agent: {ua}\n'
            f'Query:      {request.META.get("QUERY_STRING", "")}\n\n'
            f'Exception:  {exc_type.__name__}: {exc_value}\n\n'
            f'Traceback:\n{tb_text}\n'
        )

        # Send via SMTP using the project's email backend
        msg = DjangoEmailMessage(
            subject=subject,
            body=body,
            from_email=smtp_addr.email,
            to=admins,
        )
        msg.connection = smtp_addr.get_backend()
        msg.send(fail_silently=True)

        # Log to EmailSendLog
        try:
            EmailSendLog.objects.create(
                recipient=', '.join(admins),
                subject=subject,
                status='sent',
                purpose='system',
                source='web',
                address=smtp_addr,
            )
        except Exception:
            pass

        return True

    except Exception as mail_err:
        logger.error(f'ErrorMonitor: Failed to send alert email: {mail_err}')
        return False


def _record(request, status_code, error_type='', message='', tb='', kind='server'):
    """Persist an error row for the admin error page. Never raises."""
    try:
        from core.api_errors import record_error
        record_error(request, kind, status_code, error_type=error_type,
                     message=message, tb=tb)
    except Exception:
        logger.debug('ErrorMonitor: could not record error', exc_info=True)


def _status_error_type(code):
    """'NotFound' for 404, 'InternalServerError' for 500, etc."""
    try:
        return ''.join(p.title() for p in http.HTTPStatus(code).name.split('_'))
    except Exception:
        return f'HTTP{code}'


class ErrorMonitoringMiddleware:
    """Catch unhandled exceptions and email the admin with full traceback.

    Also records every failed response (4xx/5xx) — API and ordinary pages —
    in the ErrorLog table so the admin error page shows them all in one place.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        code = getattr(response, 'status_code', None)

        if code is not None and code >= 400:
            # A guarded API view (or process_exception below) already recorded
            # this failure with its traceback — don't log it twice.
            if not getattr(request, '_error_log_recorded', False):
                try:
                    reason = http.HTTPStatus(code).phrase
                except Exception:
                    reason = f'HTTP {code}'
                _record(request, code, error_type=_status_error_type(code),
                        message=f'{code} {reason}')
            # An error page nobody can see is worthless — email on 500s.
            if code == 500:
                self._alert(request, None, None, None)

        return response

    def process_exception(self, request, exception):
        """Called by Django when a view raises an unhandled exception."""
        from django.core.exceptions import PermissionDenied
        from django.http import Http404

        # Client disconnects during body upload are not server errors — they
        # are the app's network dropping mid-request. Never email about them.
        from django.http.request import UnreadablePostError
        if isinstance(exception, UnreadablePostError) or isinstance(exception.__cause__, ConnectionResetError):
            logger.info('ErrorMonitor: skipping client-disconnect error for %s', request.path)
            return None

        # Http404 / PermissionDenied are normal outcomes, not server faults.
        # Let them fall through so __call__ records the real 4xx status (and
        # no "[500]" alert email is sent for a missing page).
        if isinstance(exception, (Http404, PermissionDenied)):
            return None

        tb_text = traceback.format_exc()
        _record(request, 500, error_type=type(exception).__name__,
                message=str(exception), tb=tb_text)
        self._alert(request, type(exception), exception, tb_text)
        return None  # Let Django's default 500 handler take over

    def _alert(self, request, exc_type, exc_value, tb_text):
        """Send alert email if not rate-limited."""
        if exc_type is None:
            # 500 response returned without an unhandled exception: a view or
            # decorator caught the error itself and answered 500 deliberately
            # (e.g. _guard_android_api_errors). The real traceback is logged by
            # that handler — point the admin at it instead of a dead end.
            exc_type = type(Exception)
            exc_value = Exception('HTTP 500 error (returned, not raised)')
            tb_text = (
                '(No traceback — a 500 response was returned by the view itself.)\n'
                'The full traceback was logged by the view error handler; check the\n'
                'gunicorn error log for the matching timestamp, e.g.:\n'
                '  grep -A 30 "Unhandled error" /tmp/gunicorn_service.err')

        path = request.path

        # Skip static/media/admin-health endpoints
        skip_prefixes = ('/static/', '/media/', '/admin-dashboard/health/')
        if any(path.startswith(p) for p in skip_prefixes):
            return

        # Rate limit
        if _rate_limited(path):
            return

        # Don't crash the error reporter itself
        try:
            _send_alert_email(request, exc_type, exc_value, tb_text)
        except Exception:
            logger.error(f'ErrorMonitor: Could not send alert for {path}', exc_info=True)
