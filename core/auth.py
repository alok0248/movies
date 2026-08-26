"""
JWT authentication and rate limiting for Android API endpoints.
"""
import jwt
import time
import hashlib
from datetime import timedelta
from django.conf import settings
from django.core.cache import cache
from functools import wraps
from django.http import JsonResponse

# Secret key for JWT signing — derived from Django SECRET_KEY
JWT_SECRET = hashlib.sha256(
    (getattr(settings, 'SECRET_KEY', 'default-secret') + ':android-api-jwt').encode()
).hexdigest()
JWT_ALGORITHM = 'HS256'
JWT_EXPIRY_HOURS = 720  # 30 days
JWT_REFRESH_EXPIRY_HOURS = 1800  # 75 days


def create_jwt_token(user_id, email):
    """Create a JWT access token for a user."""
    now = int(time.time())
    payload = {
        'sub': str(user_id),
        'email': email,
        'iat': now,
        'exp': now + (JWT_EXPIRY_HOURS * 3600),
        'type': 'access',
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id, email):
    """Create a JWT refresh token for a user."""
    now = int(time.time())
    payload = {
        'sub': str(user_id),
        'email': email,
        'iat': now,
        'exp': now + (JWT_REFRESH_EXPIRY_HOURS * 3600),
        'type': 'refresh',
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt_token(token):
    """Decode and validate a JWT token. Returns payload dict or None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def create_user_token_pair(user):
    """Create both access + refresh tokens for a user."""
    return {
        'token': create_jwt_token(user.pk, user.email),
        'refreshToken': create_refresh_token(user.pk, user.email),
        'expiresIn': JWT_EXPIRY_HOURS * 3600,
    }


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

RATE_LIMIT_WINDOW = 900  # 15 minutes
RATE_LIMIT_MAX_ATTEMPTS = 10  # max requests per window per IP
RATE_LIMIT_LOCKOUT = 1800  # 30 minutes lockout


def _get_rate_limit_key(identifier, action):
    """Get cache key for rate limiting."""
    return f'api_rl:{action}:{identifier}'


def check_rate_limit(identifier, action='default'):
    """
    Check rate limit for an identifier (IP or email).
    Returns (allowed: bool, remaining: int, retry_after: int or None).
    """
    # Check lockout
    lockout_key = _get_rate_limit_key(identifier, f'{action}:lockout')
    if cache.get(lockout_key):
        ttl = cache.ttl(lockout_key) or RATE_LIMIT_LOCKOUT
        return False, 0, ttl

    # Check attempt count
    key = _get_rate_limit_key(identifier, action)
    attempts = cache.get(key, 0)

    if attempts >= RATE_LIMIT_MAX_ATTEMPTS:
        # Lock out
        cache.set(lockout_key, True, RATE_LIMIT_LOCKOUT)
        return False, 0, RATE_LIMIT_LOCKOUT

    return True, RATE_LIMIT_MAX_ATTEMPTS - attempts, None


def record_rate_limit_attempt(identifier, action='default'):
    """Record a failed attempt. Increments the counter."""
    key = _get_rate_limit_key(identifier, action)
    attempts = cache.get(key, 0)
    cache.set(key, attempts + 1, RATE_LIMIT_WINDOW)


def reset_rate_limit(identifier, action='default'):
    """Reset rate limit on successful authentication."""
    key = _get_rate_limit_key(identifier, action)
    lockout_key = _get_rate_limit_key(identifier, f'{action}:lockout')
    cache.delete(key)
    cache.delete(lockout_key)


def rate_limit_response(retry_after):
    """Build a 429 Too Many Requests response."""
    return JsonResponse({
        'status': 'error',
        'message': f'Too many requests. Please try again in {retry_after // 60} minutes.',
        'retryAfter': retry_after,
    }, status=429)


# ---------------------------------------------------------------------------
# Email Sending Helper — uses saved SMTP settings from admin dashboard
# ---------------------------------------------------------------------------
import logging
logger = logging.getLogger(__name__)


def send_configured_email(subject, message, recipient_list=None, purpose='notification', from_email=None, fail_silently=True, **kwargs):
    """Send email using the admin-configured EmailAddress for the given purpose.
    Falls back to SiteSettings, then to Django console backend."""
    if recipient_list is None:
        recipient_list = []
    from django.core.mail import EmailMessage

    # 1. Try EmailAddress model (purpose-based)
    try:
        from .models import EmailAddress
        addr = EmailAddress.objects.filter(purpose=purpose, is_active=True, is_default=True).first()
        if not addr:
            addr = EmailAddress.objects.filter(purpose=purpose, is_active=True).first()
        if addr:
            backend = addr.get_backend()
            from_email = addr.smtp_username or addr.email
            email = EmailMessage(subject=subject, body=message, from_email=from_email, to=recipient_list, connection=backend)
            email.send(fail_silently=fail_silently)
            logger.info(f'Email sent via {addr.email} ({purpose}) to {recipient_list}: {subject}')
            # Log to EmailSendLog
            try:
                from .models import EmailSendLog
                for recipient in recipient_list:
                    EmailSendLog.objects.create(address=addr, recipient=recipient, subject=subject, purpose=purpose, status='sent', source=purpose)
            except Exception:
                pass
            return True
    except Exception as e:
        logger.error(f'EmailAddress send failed: {e}', exc_info=True)
        # Log failure
        try:
            from .models import EmailSendLog
            for recipient in recipient_list:
                EmailSendLog.objects.create(address=addr if 'addr' in dir() else None, recipient=recipient, subject=subject, purpose=purpose, status='failed', error_message=str(e), source=purpose)
        except Exception:
            pass

    # 2. Fallback to SiteSettings SMTP config
    try:
        from .models import SiteSettings
        site = SiteSettings.get_settings()
        host = getattr(site, 'email_host', None)
        user = getattr(site, 'email_host_user', None)
        password = getattr(site, 'email_host_password', None)
        if host and user and password:
            from django.core.mail.backends.smtp import EmailBackend as SmtpBackend
            backend = SmtpBackend(
                host=host, port=getattr(site, 'email_port', 587),
                username=user, password=password,
                use_tls=getattr(site, 'email_use_tls', True), fail_silently=False,
            )
            email = EmailMessage(subject=subject, body=message, from_email=from_email or user, to=recipient_list, connection=backend)
            email.send(fail_silently=fail_silently)
            logger.info(f'Email sent via SiteSettings ({user}) to {recipient_list}: {subject}')
            return True
    except Exception as e:
        logger.error(f'SiteSettings email failed: {e}', exc_info=True)

    # 3. Final fallback: Django console
    from django.core.mail import send_mail as _send_mail
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@newmovies.linkpc.net')
    try:
        _send_mail(subject, message, from_email, recipient_list, fail_silently=True)
        logger.info(f'Email sent (console fallback) to {recipient_list}: {subject}')
        return True
    except Exception as e:
        logger.error(f'Email send failed: {e}')
        return False


def get_email_template(purpose, **kwargs):
    """Get and render an email template for the given purpose."""
    try:
        from .models import EmailTemplate
        tmpl = EmailTemplate.objects.filter(purpose=purpose, is_active=True).first()
        if tmpl:
            return tmpl.render(**kwargs)
    except Exception:
        pass
    return None, None
