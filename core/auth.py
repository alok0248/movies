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


def send_configured_email(subject, message, recipient_list):
    """Send email using saved SMTP settings from SiteSettings.
    Falls back to Django's default send_mail if no SMTP config is saved."""
    from django.core.mail import EmailMessage
    from django.core.mail.backends.smtp import EmailBackend as SmtpBackend
    try:
        from .models import SiteSettings
        site = SiteSettings.get_settings()
    except Exception:
        site = None

    # Build SMTP config from saved settings
    host = getattr(site, 'email_host', None) if site else None
    port = getattr(site, 'email_port', None) if site else None
    user = getattr(site, 'email_host_user', None) if site else None
    password = getattr(site, 'email_host_password', None) if site else None
    use_tls = getattr(site, 'email_use_tls', True) if site else True
    from_email = user or getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@newmovies.linkpc.net')

    # If SMTP credentials are configured, use them directly
    if host and user and password:
        try:
            backend = SmtpBackend(
                host=host,
                port=port or 587,
                username=user,
                password=password,
                use_tls=use_tls,
                fail_silently=False,
            )
            email = EmailMessage(
                subject=subject,
                body=message,
                from_email=from_email,
                to=recipient_list,
                connection=backend,
            )
            email.send(fail_silently=False)
            logger.info(f'Email sent to {recipient_list}: {subject}')
            return True
        except Exception as e:
            logger.error(f'SMTP email failed: {e}')
            # Fall through to Django default

    # Fallback: use Django's default send_mail (console backend)
    from django.core.mail import send_mail as _send_mail
    try:
        _send_mail(subject, message, from_email, recipient_list, fail_silently=True)
        logger.info(f'Email sent (fallback) to {recipient_list}: {subject}')
        return True
    except Exception as e:
        logger.error(f'Email send failed: {e}')
        return False
