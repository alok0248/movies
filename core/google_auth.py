"""Google Sign-In switches.

The website's popup needs two answers on every page render: is Google sign-in
switched on, and which OAuth client ID should the browser use? Both live in
``SiteSettings`` so the operator can flip them from the admin without touching
``settings.py`` or restarting the server. The client ID falls back to the
``GOOGLE_OAUTH_CLIENT_ID`` setting, so a site that never fills the field keeps
working exactly as before.

Put here (rather than inline in ``views.py``) because the context processor,
the sign-in endpoint and the admin form all need the same rules.
"""

from __future__ import annotations

__all__ = [
    "resolve_client_id",
    "google_login_enabled",
    "allowed_domains",
    "email_domain_allowed",
]

# Cache the settings row in the same way the rest of the site does.
_SETTINGS_CACHE_SECONDS = 60


def _settings():
    from .models import SiteSettings
    try:
        return SiteSettings.get_settings()
    except Exception:
        return None


def _default_client_id():
    """The client ID from settings.py / the environment, or ''."""
    from django.conf import settings
    return getattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', '') or ''


def resolve_client_id():
    """The OAuth Web client ID the browser should use.

    A value saved in the admin wins; otherwise the deploy-time default is used
    so `google_login_enabled` alone is enough to switch the button on.
    """
    conf = _settings()
    saved = (getattr(conf, 'google_login_client_id', '') or '').strip()
    return saved or _default_client_id().strip()


def google_login_enabled():
    """True when the operator allows Google sign-in.

    A site with no client ID at all can never enable it — the button would only
    produce failed popups — so that case reports False.
    """
    conf = _settings()
    if conf is None:
        return False
    if not getattr(conf, 'google_login_enabled', False):
        return False
    return bool(resolve_client_id())


def allowed_domains():
    """Email domains accepted for Google sign-in, lower-cased.

    Empty list means "any Google account".
    """
    conf = _settings()
    raw = getattr(conf, 'google_login_allowed_domains', '') or ''
    domains = []
    for chunk in raw.replace(',', ' ').replace('\n', ' ').split():
        chunk = chunk.strip().lower().lstrip('@')
        if chunk and chunk not in domains:
            domains.append(chunk)
    return domains


def email_domain_allowed(email):
    """True when *email*'s domain passes the optional domain restriction."""
    domains = allowed_domains()
    if not domains:
        return True
    email = (email or '').strip().lower()
    if '@' not in email:
        return False
    domain = email.rsplit('@', 1)[1]
    return any(domain == d or domain.endswith('.' + d) for d in domains)