"""Error monitoring middleware — emails admins when a 500 error occurs.

Catches unhandled exceptions during request processing and sends an email
with the full traceback, request context, and server info so crashes are
visible to the admin before users report them.

Rate-limited to at most one email per unique URL path per 5 minutes to
avoid flooding inboxes during sustained errors.
"""

import logging
import traceback
import threading
from collections import defaultdict
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


class ErrorMonitoringMiddleware:
    """Catch unhandled exceptions and email the admin with full traceback."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Check for 500 status set by Django's error handling
        if hasattr(response, 'status_code') and response.status_code == 500:
            self._alert(request, None, None, None)

        return response

    def process_exception(self, request, exception):
        """Called by Django when a view raises an unhandled exception."""
        self._alert(request, type(exception), exception, traceback.format_exc())
        return None  # Let Django's default 500 handler take over

    def _alert(self, request, exc_type, exc_value, tb_text):
        """Send alert email if not rate-limited."""
        if exc_type is None:
            # 500 set without exception (e.g., Http500 raised)
            exc_type = type(Exception)
            exc_value = Exception('HTTP 500 error')
            tb_text = '(No traceback — status set directly)'

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
