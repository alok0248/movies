"""Email-OTP as the second factor for admin (staff / superuser) sign-in.

The password is the first factor. Before any ``/admin-dashboard/`` page (or
Django's own ``/admin/``) opens, the account must also type back a 6-digit code
mailed to the address on its own user row, so a stolen password on its own can
no longer reach the dashboard.

Design choices, all deliberate:

* **Off unless switched on.** ``settings.STAFF_OTP_REQUIRED`` defaults to True,
  but the test runner forces it off (see ``settings.py``) so the existing suite
  does not need a mailbox. ``override_settings`` turns it on for the MFA tests.
* **Fail open on a mail outage.** If the code cannot be sent and
  ``STAFF_OTP_FAIL_OPEN`` is on (the default), the visitor is let through and a
  warning is logged. Locking every administrator out because SMTP is down is a
  worse outcome than a temporarily weaker second factor.
* **No address, no challenge.** A staff row with no email cannot receive a code,
  so it is let through rather than stranded — the same reasoning that makes
  ``is_email_verified`` trust staff.
* **Only the admin surface is gated.** Public pages and the ``/api/`` endpoints
  are untouched; the challenge is about the dashboard.
"""
import logging
import time

from django.conf import settings

logger = logging.getLogger('core.security')

SESSION_PASSED_KEY = 'staff_otp_passed_at'
#: Reaching the dashboard once lasts this long before a fresh code is asked for.
DEFAULT_TTL_SECONDS = 12 * 60 * 60

#: Paths that make up the admin surface (and therefore need the second factor).
ADMIN_PATH_PREFIXES = ('/admin-dashboard', '/admin/')
#: Always reachable, even before the code is entered.
OPEN_PREFIXES = (
    '/staff-otp/',
    '/logout/',
    '/ajax/logout/',
    '/static/',
    '/media/',
    '/favicon.ico',
)


def is_staff_account(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    return bool(getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))


def is_admin_path(path):
    value = path or ''
    if value.startswith(OPEN_PREFIXES):
        return False
    return value.startswith(ADMIN_PATH_PREFIXES)


def _ttl():
    return int(getattr(settings, 'STAFF_OTP_TTL_SECONDS', DEFAULT_TTL_SECONDS))


def passed(request):
    """True when the second factor was already satisfied recently."""
    session = getattr(request, 'session', None)
    if session is None:
        return False
    try:
        stamp = float(session.get(SESSION_PASSED_KEY) or 0)
    except (TypeError, ValueError):
        return False
    if not stamp:
        return False
    if time.time() - stamp > _ttl():
        session.pop(SESSION_PASSED_KEY, None)
        return False
    return True


def mark_passed(request):
    request.session[SESSION_PASSED_KEY] = time.time()


def clear(request):
    request.session.pop(SESSION_PASSED_KEY, None)


def challenge_needed(request):
    """Should this request be sent to the code screen?"""
    if not getattr(settings, 'STAFF_OTP_REQUIRED', False):
        return False
    user = getattr(request, 'user', None)
    if not is_staff_account(user):
        return False
    if not (getattr(user, 'email', '') or '').strip():
        return False
    if passed(request):
        return False
    return is_admin_path(getattr(request, 'path_info', '') or getattr(request, 'path', ''))


def _send_code(user, code):
    """Mail the code. Returns (ok, error)."""
    body = (
        "Your administrator sign-in code is: {code}\n\n"
        "It expires in {minutes} minutes. Enter it on the verification screen to "
        "open the dashboard.\n\n"
        "If you did not try to sign in, change your password now.\n"
    ).format(code=code, minutes=10)
    try:
        from django.core.mail import send_mail
        send_mail(
            'Your administrator sign-in code',
            body,
            getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@localhost'),
            [user.email],
            fail_silently=False,
        )
        return True, ''
    except Exception as exc:      # an SMTP outage must never crash a request
        logger.warning('Staff OTP: could not send the code to %s', user.email, exc_info=True)
        return False, str(exc)[:300]


def issue(user):
    """Invalidate any earlier unused code, issue a new one and mail it."""
    from .models import StaffLoginOTP

    StaffLoginOTP.objects.filter(user=user, used=False).update(used=True)
    code = StaffLoginOTP.generate()
    StaffLoginOTP.objects.create(user=user, otp=StaffLoginOTP.hash_code(code))
    return _send_code(user, code)


def has_live_code(user):
    """True when a still-valid code already exists (so a GET need not re-mail)."""
    from .models import StaffLoginOTP

    entry = (StaffLoginOTP.objects.filter(user=user, used=False)
             .order_by('-created_at').first())
    return bool(entry and entry.is_valid)


def verify(user, code):
    """Check ``code`` against the newest live code. Returns True on success."""
    from .models import StaffLoginOTP

    entry = (StaffLoginOTP.objects.filter(user=user, used=False)
             .order_by('-created_at').first())
    if entry is None or entry.too_many_attempts:
        return False
    if entry.is_expired:
        return False
    if not entry.verify_code(code):
        StaffLoginOTP.objects.filter(pk=entry.pk).update(attempts=entry.attempts + 1)
        return False
    StaffLoginOTP.objects.filter(pk=entry.pk).update(used=True)
    return True


def fail_open_enabled():
    return bool(getattr(settings, 'STAFF_OTP_FAIL_OPEN', True))