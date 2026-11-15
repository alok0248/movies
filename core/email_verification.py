"""Whether an account is allowed to sign in: its email has to be confirmed.

The answer lives on ``UserProfile.email_verified``, server-side only — nothing
the browser can send will set it. Three rules keep turning this on safe:

* **Staff and superusers are always verified.** The admin sign-in must never
  depend on a mailbox that may have gone away.
* **Accounts that predate the rule are grandfathered.** An *active* account
  with no profile row was created by Google sign-in, by the app's sync or by
  an administrator — all of which trust the address — so it stays able to sign
  in. Only an account created *inactive*, which is what both registration
  flows do, starts unverified.
* **A failed lookup never grants access.** An unreadable profile is treated as
  unverified rather than verified.

``mark_email_verified`` is called from every place that confirms the code: the
web OTP box, the emailed link, and the app's verify endpoint.
"""
from django.utils import timezone


def is_email_verified(user):
    """True when this account may sign in."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return True

    from .models import UserProfile

    try:
        profile = UserProfile.objects.filter(user_id=user.pk).only('email_verified').first()
    except Exception:
        # Fail closed: a database problem must not read as "verified".
        return False
    if profile is None:
        # No row yet — trusted only if the account was never left pending.
        return bool(getattr(user, 'is_active', False))
    return bool(profile.email_verified)


def needs_email_verification(user):
    """Signed in, but the address was never confirmed."""
    if not getattr(user, 'is_authenticated', False):
        return False
    return not is_email_verified(user)


def mark_email_verified(user):
    """Record that this address was confirmed. Safe to call twice."""
    if user is None:
        return
    from .models import UserProfile

    profile, _created = UserProfile.objects.get_or_create(user=user)
    if profile.email_verified:
        return
    profile.email_verified = True
    profile.email_verified_at = timezone.now()
    profile.save(update_fields=['email_verified', 'email_verified_at'])
