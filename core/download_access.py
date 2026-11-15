"""Who may open the Android app download page (``/get-app/``).

Two switches on ``SiteSettings`` decide it:

* ``app_download_public_access`` — anyone at all, signed in or not;
* ``app_download_login_required`` — only signed-in accounts.

They combine into one of three states:

* ``public`` — public access is on. It wins over the login switch, so turning
  the login requirement on never takes the page away from people who already
  had it.
* ``login``  — public access is off and the login requirement is on.
* ``closed`` — both are off, so nobody may view the page. That is the state an
  operator picks during an incident when the download page should disappear.

The answer is cached for a few seconds per worker, the same way the site-wide
browse gate is: the settings row itself lives in cache for a minute, which is
far too long for a switch an admin expects to bite immediately. Every
``SiteSettings.save()`` clears this key, so a save takes effect on the very
next request in every worker.
"""
from django.core.cache import cache

PUBLIC = 'public'
LOGIN = 'login'
CLOSED = 'closed'

# How long a worker may reuse the answer before it looks again.
CACHE_SECONDS = 5

# What a posted toggle may contain. An HTML checkbox sends "on" when ticked and
# nothing when not; the rest are accepted so a script or a different form shape
# still works, and anything else is refused rather than guessed at.
VALID_TOGGLE_VALUES = {
    'on': True, '1': True, 'true': True, 'yes': True,
    'off': False, '0': False, 'false': False, 'no': False, '': False,
}

LABELS = {
    PUBLIC: 'Public — open to everyone',
    LOGIN: 'Sign-in required',
    CLOSED: 'Closed — nobody may view it',
}

EXPLANATIONS = {
    PUBLIC: ('Anyone can open the Android app download page. The sign-in switch '
             'is ignored while public access is on.'),
    LOGIN: ('Only signed-in visitors can open the Android app download page. '
            'Everyone else is sent to the sign-in screen.'),
    CLOSED: ('The Android app download page is closed to everyone, signed in or '
             'not. Turn one of the switches on to reopen it.'),
}


class InvalidToggle(ValueError):
    """A posted value that is not a valid enable/disable state."""


def parse_toggle(raw):
    """Turn a posted checkbox into True/False, refusing anything unexpected.

    ``None`` (field absent from the POST) means unchecked. A value outside the
    accepted set raises :class:`InvalidToggle` so the caller can refuse the save
    instead of silently storing a guess.
    """
    if raw is None:
        return False
    key = str(raw).strip().lower()
    try:
        return VALID_TOGGLE_VALUES[key]
    except KeyError:
        raise InvalidToggle(raw)


def download_access_state():
    """Return ``public``, ``login`` or ``closed`` for the download page."""
    from .models import SiteSettings

    state = cache.get(SiteSettings._DOWNLOAD_ACCESS_CACHE_KEY)
    if state in (PUBLIC, LOGIN, CLOSED):
        return state

    site = SiteSettings.get_settings()
    if site.app_download_public_access:
        state = PUBLIC
    elif site.app_download_login_required:
        state = LOGIN
    else:
        state = CLOSED
    cache.set(SiteSettings._DOWNLOAD_ACCESS_CACHE_KEY, state, CACHE_SECONDS)
    return state


def may_view(user):
    """``(allowed, state)`` for this visitor."""
    state = download_access_state()
    if state == PUBLIC:
        return True, state
    signed_in = bool(user is not None and getattr(user, 'is_authenticated', False))
    if state == LOGIN:
        return signed_in, state
    return False, state


def label(state):
    return LABELS.get(state, state)


def explanation(state):
    return EXPLANATIONS.get(state, '')