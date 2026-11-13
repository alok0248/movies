"""Access control for the server-side media proxy endpoints.

``/proxy/<target>``, ``/ajax/download-proxy/`` and ``/ajax/proxy-embed/`` all
make *our* server fetch a remote URL on behalf of a visitor. Without a gate
they are an open relay: anybody can point them at any public host, and the
response used to carry ``Access-Control-Allow-Origin: *`` so a third-party page
could read the bytes too.

This module keeps every legitimate playback path working while refusing
requests that were not started by one of our own pages or by a player we embed.
Two proofs are accepted:

1. **Browser proof** — ``Sec-Fetch-Site: same-origin`` or a ``Referer`` /
   ``Origin`` whose host is this site (or a player origin we embed). Browsers
   send these automatically; the site is served with Django's default
   ``Referrer-Policy: same-origin``, so our own pages always send a full
   same-origin referrer.
2. **Capability proof** — a signed, expiring token that
   :func:`signed_proxy_query` appends to the ``proxy_url`` handed out by
   ``/api/links/``. Native players (and the documented "play ``proxy_url``"
   flow) have no referrer, so the token is what keeps them working.

Every caller is rate limited per IP address as well, before either proof is
looked at, so brute-forcing tokens is not cheaper than using the API.

Residual, reported trade-off: a *non-browser* client can forge ``Referer`` and
``Sec-Fetch-Site``. That is why the SSRF guard (``core.url_guard``) and the
per-IP limit stay in front of every upstream fetch. A hard upstream host
allowlist was deliberately not added — the extractors rotate CDN hosts
constantly and an allowlist would silently break playback.
"""
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.cache import patch_vary_headers

# Query parameter carrying the capability token on a proxy URL.
PROXY_TOKEN_PARAM = '_pxt'

# How long a handed-out proxy_url stays usable. Long enough for one viewing
# session (players re-request the manifest many times) but not permanent.
PROXY_TOKEN_TTL = 6 * 3600

PROXY_TOKEN_SALT = 'core.proxy-url.v1'

# Player origins we embed that are allowed to call the proxy cross-origin.
TRUSTED_PLAYER_DOMAINS = ('videasy.to',)

# Requests per minute per client IP across all proxy endpoints. Sized well
# above real playback traffic (a viewer pulls a few segments per second at
# most) so it only ever bites abusive clients. nginx applies a second,
# harder limit on /proxy/ at the edge.
PROXY_RATE_LIMIT_PER_MINUTE = 3000
PROXY_RATE_LIMIT_WINDOW = 60


def make_proxy_token(target):
    """Sign the ``host/path`` part of a proxy URL."""
    return signing.dumps(target, salt=PROXY_TOKEN_SALT, compress=True)


def proxy_token_valid(target, token):
    """True when ``token`` is a live signature for exactly this ``target``."""
    if not token:
        return False
    try:
        return signing.loads(token, salt=PROXY_TOKEN_SALT,
                             max_age=PROXY_TOKEN_TTL) == target
    except signing.BadSignature:
        return False


def signed_proxy_query(target):
    """``_pxt=...`` query fragment for a URL we hand to a native client."""
    return f'{PROXY_TOKEN_PARAM}={make_proxy_token(target)}'


def proxy_path_and_query(url):
    """Split a raw ``host/path`` (+ optional query) into (target, query)."""
    raw = url.split('://', 1)[-1]
    target, _, query = raw.partition('?')
    return target, query


def _host_of(value):
    if not value:
        return ''
    try:
        return (urlsplit(value).hostname or '').lower()
    except ValueError:
        return ''


def _own_hosts(request):
    hosts = set()
    try:
        hosts.add(request.get_host().split(':')[0].lower())
    except Exception:
        pass
    for name in getattr(settings, 'ALLOWED_HOSTS', None) or ():
        name = (name or '').strip().lower()
        if name and name != '*' and not name.startswith('.'):
            hosts.add(name)
    return hosts


def _is_site_host(host, request):
    if not host:
        return False
    if host in _own_hosts(request):
        return True
    for name in getattr(settings, 'ALLOWED_HOSTS', None) or ():
        name = (name or '').strip().lower()
        if name.startswith('.') and (host == name[1:] or host.endswith(name)):
            return True
    return False


def _is_trusted_player_host(host):
    if not host:
        return False
    return any(host == d or host.endswith('.' + d) for d in TRUSTED_PLAYER_DOMAINS)


def browser_proof(request):
    """True when the request demonstrably came from a browser we serve."""
    if request.META.get('HTTP_SEC_FETCH_SITE', '').lower() == 'same-origin':
        return True
    for header in ('HTTP_ORIGIN', 'HTTP_REFERER'):
        host = _host_of(request.META.get(header, ''))
        if host and (_is_site_host(host, request) or _is_trusted_player_host(host)):
            return True
    return False


def _client_ip(request):
    # Same resolution the rest of the site uses (core.views._get_client_ip),
    # imported lazily to avoid a circular import.
    try:
        from .views import _get_client_ip
        return _get_client_ip(request) or 'unknown'
    except Exception:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        return (forwarded.split(',')[0].strip()
                or request.META.get('REMOTE_ADDR', '') or 'unknown')


def rate_limit_exceeded(request):
    """Count this request and report whether the caller is over the limit."""
    limit = getattr(settings, 'PROXY_RATE_LIMIT_PER_MINUTE',
                    PROXY_RATE_LIMIT_PER_MINUTE)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = PROXY_RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return False
    key = f'proxy-rl:{_client_ip(request)}'
    try:
        count = cache.get(key, 0) + 1
        cache.set(key, count, PROXY_RATE_LIMIT_WINDOW)
    except Exception:
        return False
    return count > limit


def check_proxy_access(request, target, token=None):
    """Return ``None`` when the request may use the proxy, else a response."""
    if rate_limit_exceeded(request):
        resp = HttpResponse('Too many proxy requests, slow down.', status=429,
                            content_type='text/plain')
        resp['Retry-After'] = str(PROXY_RATE_LIMIT_WINDOW)
        resp['Cache-Control'] = 'no-store'
        return resp

    if proxy_token_valid(target, token):
        return None
    if browser_proof(request):
        return None

    resp = HttpResponse(
        'This proxy only serves playback from newmovies.linkpc.net.',
        status=403, content_type='text/plain')
    resp['Cache-Control'] = 'no-store'
    return resp


def apply_proxy_cors(request, response):
    """Reflect a trusted Origin instead of the old ``Access-Control-Allow-Origin: *``."""
    origin = request.META.get('HTTP_ORIGIN', '')
    host = _host_of(origin)
    if host and (_is_site_host(host, request) or _is_trusted_player_host(host)):
        response['Access-Control-Allow-Origin'] = origin
        # The allowed origin now depends on the request, so shared caches
        # (and the browser's own segment cache) must key on it.
        patch_vary_headers(response, ('Origin',))
    return response
