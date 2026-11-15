"""The one account used for hands-on testing, the same one every time.

Why it exists: poking at the site by hand (sign-in, the app's API, the admin
dashboard) needs an account, and inventing a new one per session leaves the
database full of throwaways and makes results impossible to compare. So there is
one account, defined once, reused everywhere.

The rules this module enforces:

* **One account, created once.** :func:`ensure_test_user` looks the address up
  first and returns the existing row untouched. Running it a hundred times
  creates nothing after the first and never rewrites the password, so the
  credentials stay the same across runs, restarts and machines.
* **A gmail.com address.** Enforced, not assumed — the registration form only
  accepts Gmail and Yahoo, and a test account has to look like one a real person
  could have made.
* **The same credentials locally and on the server.** They are written to
  ``.test_user.json`` next to ``manage.py`` (gitignored, so never pushed), and
  ``TEST_USER_EMAIL`` / ``TEST_USER_PASSWORD`` override it in any environment
  that prefers environment variables.
* **Approval differs by environment.** Locally the account is switched on by the
  command itself (``is_active`` plus the confirmed-email flag, so the gate from
  ``core/email_verification.py`` lets it in). On the server it is deliberately
  left pending for an administrator to approve by hand: an account that turns
  itself on in production is a backdoor, so the command refuses to do it there
  unless someone passes ``--approve`` on purpose.

**Treat the credentials as secrets.** They are a working sign-in for the live
site. Keep ``.test_user.json`` out of git, and switch the account off (or delete
it) when it is not being used.

Run it with::

    python manage.py ensure_test_user            # create if missing, then report
    python manage.py ensure_test_user --check     # report only, create nothing
    python manage.py ensure_test_user --approve   # also switch the account on
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

#: Where the local credentials live, next to ``manage.py``.
CREDENTIALS_FILENAME = '.test_user.json'

#: The only domain a test account may use.
ALLOWED_DOMAIN = 'gmail.com'


class TestUserConfigError(Exception):
    """The credentials are missing, unreadable, or not acceptable."""


@dataclass
class TestUserStatus:
    """What :func:`ensure_test_user` found and did."""

    email: str
    username: str = ''
    created: bool = False
    existed: bool = False
    approved: bool = False
    already_approved: bool = False
    is_active: bool = False

    def describe(self):
        if self.created:
            what = 'created'
        elif self.existed:
            what = 'already existed (nothing changed)'
        else:
            what = 'not created'
        state = 'approved' if self.is_active else 'pending approval'
        return '%s — %s, %s' % (self.email, what, state)


def credentials_path():
    """The local credentials file for this checkout."""
    return Path(settings.BASE_DIR) / CREDENTIALS_FILENAME


def load_credentials():
    """``(email, password)`` from the environment, else from the local file.

    Raises :class:`TestUserConfigError` with a usable message rather than a bare
    ``KeyError`` — this runs before tests, so a wrong path must say what to do.
    """
    email = (os.environ.get('TEST_USER_EMAIL') or '').strip().lower()
    password = os.environ.get('TEST_USER_PASSWORD') or ''
    if email and password:
        return email, password

    path = credentials_path()
    if not path.exists():
        raise TestUserConfigError(
            'No test-user credentials found. Either set TEST_USER_EMAIL and '
            'TEST_USER_PASSWORD, or create %s containing '
            '{"email": "name@gmail.com", "password": "..."}.' % path)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise TestUserConfigError('Could not read %s: %s' % (path, exc))
    if not isinstance(data, dict):
        raise TestUserConfigError('%s must contain a JSON object.' % path)
    return (str(data.get('email') or '').strip().lower(),
            str(data.get('password') or ''))


def validate_email(email):
    """Only a gmail.com address is accepted; anything else is refused."""
    email = (email or '').strip().lower()
    if '@' not in email:
        raise TestUserConfigError('The test user needs an email address, not %r.' % email)
    domain = email.rsplit('@', 1)[1]
    if domain != ALLOWED_DOMAIN:
        raise TestUserConfigError(
            'The test user must be a %s address; %r uses %r.' % (email, ALLOWED_DOMAIN, domain))
    if not email.split('@', 1)[0]:
        raise TestUserConfigError('The test user needs something before the @: %r.' % email)
    return email


def should_approve_locally():
    """True in a development checkout, False anywhere that looks like production.

    Auto-approval is tied to ``DEBUG`` on purpose: it is off on the server, so
    the live account can only ever be switched on by someone passing
    ``--approve``. ``TEST_USER_AUTO_APPROVE=1`` is the explicit escape hatch.
    """
    if os.environ.get('TEST_USER_AUTO_APPROVE', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return True
    return bool(settings.DEBUG)


def _build_username(email):
    """A stable username for the address, nudged if an unrelated account holds it."""
    from django.contrib.auth.models import User

    base = email.split('@', 1)[0][:140] or 'testuser'
    if not User.objects.filter(username=base).exists():
        return base
    suffix = 2
    while User.objects.filter(username='%s%d' % (base, suffix)).exists():
        suffix += 1
    return '%s%d' % (base, suffix)


def ensure_test_user(approve=None, email=None, password=None):
    """Return the test account, creating it only when it is genuinely missing.

    ``approve`` forces the decision (``True``/``False``); ``None`` means "decide
    from this environment" — see :func:`should_approve_locally`. An existing
    account is never recreated and its password is never rewritten here.
    """
    from django.contrib.auth.models import User

    if email is None:
        email, file_password = load_credentials()
        password = password or file_password
    email = validate_email(email)

    status = TestUserStatus(email=email)

    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        # A password is only needed to create the account; once it exists the
        # credentials are whatever they were, and nothing here may change them.
        if not password:
            raise TestUserConfigError('A password is needed to create the test account.')
        user = User.objects.create_user(
            username=_build_username(email), email=email, password=password)
        # Pending, exactly as the registration form leaves a new account.
        user.is_active = False
        user.save(update_fields=['is_active'])
        status.created = True
    else:
        status.existed = True

    status.username = user.username

    approve_now = should_approve_locally() if approve is None else bool(approve)
    if approve_now:
        status.already_approved = bool(user.is_active)
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=['is_active'])
        # The confirmed-email flag is what the sign-in gate reads, so approval
        # has to set it or the account still cannot get in.
        from .email_verification import is_email_verified, mark_email_verified
        if not is_email_verified(user):
            mark_email_verified(user)
        status.approved = True

    user.refresh_from_db(fields=['is_active'])
    status.is_active = user.is_active
    return status