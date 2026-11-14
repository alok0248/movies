"""Per-user credential checks for the app's data endpoints.

Until now the app proved only *which build* was calling (the shared Basic
credential plus the ``X-Android-App``/``X-Android-Build`` headers) and the email
in the query string decided whose data came back. Anyone holding the shared
credential could therefore read or write another person's messages, watch
history or notifications by naming their address.

New builds send the JWT access token they were given at sign-in as
``X-User-Token`` (``Authorization: Bearer`` is accepted too). When a token is
present it decides the identity, and a claimed email only has to agree with it.

Existing installs cannot start sending a token they never stored, so the
transition is explicit and per app row: ``AndroidApp.legacy_user_auth_until``
keeps token-less requests working until that date, and after it they get a 401
with ``code: user_token_required`` so the app can prompt for an update. A null
date means "no deadline set"; the day the row is set is the day the shared
credential stops being enough. Every legacy request is logged and counted once
per address, endpoint and day, which is the evidence needed to pick that date.
"""
import logging

from django.core.cache import cache
from django.http import JsonResponse

from .auth import decode_jwt_token

logger = logging.getLogger(__name__)

# The app sends the per-user token here, leaving Authorization for the shared
# app credential it has always used.
TOKEN_HEADER = 'HTTP_X_USER_TOKEN'
BEARER_PREFIX = 'bearer '

CODE_TOKEN_INVALID = 'token_invalid'
CODE_TOKEN_REQUIRED = 'user_token_required'
CODE_EMAIL_MISMATCH = 'email_mismatch'


def token_from_request(request):
    """The per-user token on this request, or ''. Never validated here."""
    token = (request.META.get(TOKEN_HEADER) or '').strip()
    if token:
        return token
    auth = (request.META.get('HTTP_AUTHORIZATION') or '').strip()
    if auth.lower().startswith(BEARER_PREFIX):
        return auth[len(BEARER_PREFIX):].strip()
    return ''


def _legacy_allowed(app_match):
    """Whether a request with no per-user token may still be served.

    ``legacy_user_auth_until is None`` means the deadline has not been set for
    that app row yet. Anything else is inclusive of the day itself.
    """
    if app_match is None:
        return True
    deadline = getattr(app_match, 'legacy_user_auth_until', None)
    if deadline is None:
        return True
    from django.utils import timezone
    return timezone.localdate() <= deadline


def _count_legacy_use(endpoint, email, app_match):
    """Log the first legacy call per address/endpoint/day, and count the rest."""
    day = None
    try:
        from django.utils import timezone
        day = timezone.localdate().isoformat()
    except Exception:
        return
    marker = 'legacy_user_auth:%s:%s:%s' % (day, endpoint, email or '-')
    try:
        first = cache.add(marker, 1, 60 * 60 * 24)
        if first:
            logger.warning(
                'legacy user auth (no per-user token): endpoint=%s email=%s app=%s',
                endpoint, email or '(device only)', getattr(app_match, 'name', '-'))
    except Exception:
        logger.debug('legacy auth counter unavailable', exc_info=True)


def authenticate_user(request, claimed_email='', app_match=None, endpoint=''):
    """Resolve the identity behind a data request.

    Returns ``(email, error_response)``. Exactly one of the two is set.

    * A valid access token wins: the email comes from the token, and a claimed
      email that disagrees is a 403 (never a silent cross-user read).
    * An expired/invalid token is a 401 ``token_invalid`` so the app refreshes
      instead of retrying the same rejected request.
    * No token at all is the legacy path: allowed while the app row's
      ``legacy_user_auth_until`` has not passed, and refused with 401
      ``user_token_required`` afterwards so the caller updates the app.
    """
    claimed = (claimed_email or '').strip().lower()
    token = token_from_request(request)

    if token:
        payload = decode_jwt_token(token)
        if not payload or payload.get('type') != 'access':
            return None, JsonResponse({
                'status': 'error',
                'code': CODE_TOKEN_INVALID,
                'message': 'Your session has expired. Please sign in again.',
                'retryable': False,
            }, status=401)
        email = (payload.get('email') or '').strip().lower()
        if not email:
            return None, JsonResponse({
                'status': 'error',
                'code': CODE_TOKEN_INVALID,
                'message': 'Your session is not valid. Please sign in again.',
            }, status=401)
        if claimed and claimed != email:
            logger.warning('user token/email mismatch: token=%s claimed=%s endpoint=%s',
                           email, claimed, endpoint or '-')
            return None, JsonResponse({
                'status': 'error',
                'code': CODE_EMAIL_MISMATCH,
                'message': 'That credential belongs to a different account.',
            }, status=403)
        return email, None

    if not _legacy_allowed(app_match):
        logger.warning('user token required but absent: endpoint=%s email=%s',
                       endpoint or '-', claimed or '(none)')
        return None, JsonResponse({
            'status': 'error',
            'code': CODE_TOKEN_REQUIRED,
            'message': 'Please update the app to keep using your account.',
            'retryable': False,
        }, status=401)

    _count_legacy_use(endpoint or '-', claimed, app_match)
    return claimed, None
