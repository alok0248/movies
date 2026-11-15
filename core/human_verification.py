"""Layer 1 of the bot protection: is this visitor a human?

A visitor proves it once by solving a Cloudflare Turnstile challenge at
``/verify/``. On success the session is marked and the mark lasts
``settings.HUMAN_VERIFICATION_TTL_SECONDS`` (24 hours), after which the next
page load asks again.

Everything here is deliberately small and side-effect free except
:func:`verify_turnstile` (one HTTP call) and the session helpers:

* :func:`is_configured` — the gate is inert until a real key pair is loaded, so
  a deploy that lands before the keys are added cannot lock every visitor out.
* :func:`is_human` / :func:`mark_human` — the session mark, with its own expiry
  so an old session cannot stay "verified" for ever.
* :func:`is_google_crawler` — search indexing is the one crawler exemption
  (every other crawler has to solve the challenge, which it cannot).
* :func:`is_app_request` — the Android app and the API never see the gate.
* :func:`is_probe_path` / :func:`is_scraper_agent` — the shared classifier the
  admin traffic reports use to label a scanner without refusing it.
* :func:`safe_next` — kills open redirects on the ``next`` parameter.
* :func:`verify_turnstile` — the server-side siteverify call, with a hard
  timeout, and a hostname check so a token minted for another site is refused.
"""
import logging
import time
from urllib.parse import urlsplit

import requests
from django.conf import settings

logger = logging.getLogger('core.security')

#: Where the solved-challenge mark lives in the session.
SESSION_HUMAN_KEY = 'is_human'
SESSION_HUMAN_AT_KEY = 'is_human_at'

#: Paths that never need the challenge, matched on the start of the path.
EXEMPT_PREFIXES = (
    '/verify/',
    # The status page explains the gate, so it has to be readable before it is
    # passed — otherwise the only people who could read it never need to.
    '/security-status/',
    # The admin second factor must be reachable before the human mark exists,
    # or a staff member could not get past either gate on a fresh browser.
    '/staff-otp/',
    '/static/',
    '/media/',
    '/admin/',
    '/api/',
    '/apps/download/',
    '/favicon.ico',
    '/robots.txt',
    '/sitemap.xml',
    '/manifest.json',
    '/sw.js',
    '/service-worker.js',
    '/ads.txt',
    '/app-ads.txt',
    '/.well-known/',
)

#: User-Agent fragments that mean Google. Only Google is let through: search
#: indexing is the one crawler the site cannot afford to lose.
GOOGLE_CRAWLER_MARKERS = (
    'googlebot',
    'google-inspectiontool',
    'googleother',
    'google-site-verification',
    'adsbot-google',
    'mediapartners-google',
    'apis-google',
    'feedfetcher-google',
    'storebot-google',
)

#: Paths that real users never browse to: exploit probes and framework files.
#: Used to label traffic in the admin reports, not to refuse it.
PROBE_PATH_MARKERS = (
    '/index.php', '/wp-', '/.env', '/.git', '/vendor/', '/phpunit',
    '/xmlrpc.php', '/admin.php', '/config.php', '/cgi-bin/', '/shell',
    '/actuator', '/docker', '/credentials', '/.aws', '/.ssh', '/boaform',
    '/hudson', '/solr/', '/jenkins', '/telescope', '/containers/json',
    '/sdk/', '/userportal/', '/_ignition', '/.vscode', '/.idea', '/.svn',
    '/.htaccess', '/.htpasswd', '/.ds_store', '/server-status', '/phpinfo',
    '/info.php', '/test.php', '/db.php', '/owa/', '/autodiscover', '/cgi',
    '/gponform', '/.dockerenv', '/wp-content',
)

#: Signatures of scanners, crawlers and command-line clients that hammer the
#: site without ever being a real visitor.
SCRAPER_UA_MARKERS = (
    'bot', 'crawler', 'spider', 'scrapy', 'curl', 'wget', 'python-requests',
    'python-urllib', 'go-http-client', 'java/', 'okhttp', 'libwww', 'axios',
    'headlesschrome', 'phantomjs', 'masscan', 'zgrab', 'nmap', 'nuclei',
    'l9explore', 'expanse', 'censys', 'shodan', 'internetmeasurement',
)

#: Reason codes returned by verify_turnstile, so a caller (and the tests) can
#: branch on the cause instead of the message.
REASON_OK = 'ok'
REASON_NOT_CONFIGURED = 'not_configured'
REASON_MISSING_TOKEN = 'missing_token'
REASON_TIMEOUT = 'timeout'
REASON_NETWORK = 'network_error'
REASON_BAD_JSON = 'invalid_response'
REASON_REFUSED = 'verification_failed'
REASON_HOSTNAME = 'hostname_mismatch'

_PLACEHOLDER_KEYS = {'', 'your_site_key', 'your_secret_key'}


def is_configured():
    """True once a real key pair is loaded. Until then the gate does nothing."""
    site_key = (getattr(settings, 'TURNSTILE_SITE_KEY', '') or '').strip()
    secret = (getattr(settings, 'TURNSTILE_SECRET_KEY', '') or '').strip()
    return site_key.lower() not in _PLACEHOLDER_KEYS and secret.lower() not in _PLACEHOLDER_KEYS


def site_key():
    return (getattr(settings, 'TURNSTILE_SITE_KEY', '') or '').strip()


def is_human(request):
    """True when this session solved the challenge and the mark has not expired."""
    session = getattr(request, 'session', None)
    if session is None:
        return False
    if session.get(SESSION_HUMAN_KEY) is not True:
        return False
    try:
        solved_at = float(session.get(SESSION_HUMAN_AT_KEY) or 0)
    except (TypeError, ValueError):
        return False
    if not solved_at:
        return False
    ttl = int(getattr(settings, 'HUMAN_VERIFICATION_TTL_SECONDS', 24 * 60 * 60))
    if time.time() - solved_at > ttl:
        # Expired: clear it so the next request asks again.
        session.pop(SESSION_HUMAN_KEY, None)
        session.pop(SESSION_HUMAN_AT_KEY, None)
        return False
    return True


def mark_human(request):
    """Record a solved challenge on the current session."""
    request.session[SESSION_HUMAN_KEY] = True
    request.session[SESSION_HUMAN_AT_KEY] = time.time()


def is_google_crawler(user_agent):
    agent = (user_agent or '').lower()
    return bool(agent) and any(marker in agent for marker in GOOGLE_CRAWLER_MARKERS)


def is_probe_path(path):
    """True when a path is one only a scanner would ever ask for."""
    candidate = (path or '').lower()
    if not candidate:
        return False
    return any(marker in candidate for marker in PROBE_PATH_MARKERS)


def is_scraper_agent(user_agent):
    """True when the client announces itself as a crawler or a script."""
    agent = (user_agent or '').strip().lower()
    if not agent:
        return False
    return any(marker in agent for marker in SCRAPER_UA_MARKERS)


def is_app_request(request):
    """True for the Android app's own traffic, which must never be gated.

    Two signals, both of which the app already sends: the API path prefixes, and
    its OkHttp User-Agent (or the ``X-Android-App`` header the build identifies
    itself with).
    """
    path = (getattr(request, 'path_info', '') or getattr(request, 'path', '') or '')
    if path.startswith(('/api/', '/apps/download/')):
        return True
    agent = (request.META.get('HTTP_USER_AGENT', '') or '').lower()
    if 'okhttp' in agent:
        return True
    return bool(request.META.get('HTTP_X_ANDROID_APP'))


def skip_path(request):
    """True when the path is on the list that never needs the challenge."""
    path = (getattr(request, 'path_info', '') or getattr(request, 'path', '') or '')
    return path.startswith(EXEMPT_PREFIXES)


def safe_next(raw, host):
    """A ``next`` value that cannot leave this site.

    Only a same-origin path is honoured: a value starting with a single ``/``
    (but not ``//``, which is protocol-relative and therefore another host), or
    an absolute URL whose host equals ``host``. Everything else becomes ``/``,
    which is what removes the open-redirect hole.
    """
    value = (raw or '').strip()
    # Control characters and backslashes are refused outright: browsers treat
    # "\" as "/" in a URL, so "/\evil.example" would otherwise leave the site.
    if not value or any(ch in value for ch in '\r\n\t\\'):
        return '/'
    host = (host or '').split(':')[0].lower()
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        if parts.scheme not in ('http', 'https'):
            return '/'
        if parts.netloc.split(':')[0].lower() != host:
            return '/'
        target = parts.path or '/'
        return target + (('?' + parts.query) if parts.query else '')
    if not value.startswith('/') or value.startswith('//'):
        return '/'
    return value


def verify_turnstile(token, remote_ip='', expected_host=''):
    """Verify a Turnstile token server-side.

    Returns ``(ok, reason, detail)``. Never raises: a network failure, a timeout
    or a malformed reply is reported as a reason code so the caller can show a
    retryable message instead of a 500.
    """
    if not is_configured():
        # With no keys there is nothing to check, so the caller treats this as a
        # pass-through rather than locking the site.
        return True, REASON_NOT_CONFIGURED, 'Turnstile is not configured.'

    token = (token or '').strip()
    if not token:
        return False, REASON_MISSING_TOKEN, 'The verification token is missing.'

    payload = {
        'secret': settings.TURNSTILE_SECRET_KEY,
        'response': token,
    }
    if remote_ip:
        payload['remoteip'] = remote_ip

    try:
        response = requests.post(
            getattr(settings, 'TURNSTILE_VERIFY_URL', ''),
            data=payload,
            timeout=float(getattr(settings, 'TURNSTILE_TIMEOUT_SECONDS', 5)),
        )
    except requests.Timeout:
        logger.warning('Turnstile: siteverify timed out')
        return False, REASON_TIMEOUT, 'The verification service did not answer in time.'
    except requests.RequestException as exc:
        logger.warning('Turnstile: siteverify network error: %s', exc)
        return False, REASON_NETWORK, 'The verification service could not be reached.'

    try:
        data = response.json()
    except ValueError:
        logger.warning('Turnstile: siteverify returned non-JSON (HTTP %s)', response.status_code)
        return False, REASON_BAD_JSON, 'The verification service sent an unreadable answer.'
    if not isinstance(data, dict):
        return False, REASON_BAD_JSON, 'The verification service sent an unreadable answer.'

    if data.get('success') is not True:
        codes = ', '.join(data.get('error-codes') or []) or 'no error code given'
        logger.info('Turnstile: challenge refused (%s)', codes)
        return False, REASON_REFUSED, 'That challenge was not solved. Please try again.'

    # A token minted for another hostname must not unlock this one.
    returned_host = (data.get('hostname') or '').strip().lower()
    if expected_host and returned_host:
        if returned_host != expected_host.strip().split(':')[0].lower():
            logger.warning('Turnstile: hostname mismatch (token for %s, site %s)',
                           returned_host, expected_host)
            return False, REASON_HOSTNAME, 'That challenge was issued for a different site.'

    return True, REASON_OK, ''