"""Outbound URL guard.

Several endpoints (``/proxy/<host>/<path>``, ``/ajax/proxy-embed/``,
``/ajax/download-proxy/``) fetch a URL supplied by the browser, which makes
them a textbook SSRF surface. The player needs them to talk to arbitrary CDN
hosts, so we cannot use a fixed host allow-list — instead every target is
resolved first and rejected when it points anywhere that is not a normal
public address (loopback, private LAN, link-local / cloud metadata, CGNAT,
multicast, reserved, and so on).

Usage::

    from core.url_guard import UnsafeURLError, ensure_public_url

    try:
        target = ensure_public_url(target_url)
    except UnsafeURLError as exc:
        return HttpResponse(str(exc), status=400)
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "UnsafeURLError",
    "ensure_public_url",
    "guarded_get",
    "is_public_url",
    "looks_like_address",
]

# Hostnames that never need a DNS lookup and must always be refused.
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}

_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
    ".in-addr.arpa",
    ".ip6.arpa",
)

# Ports we are willing to reach. Anything else (SSH, SMTP, Redis, the Django
# dev server, k8s API, ...) is refused.
_ALLOWED_PORTS = frozenset({80, 443, 8080, 8443, 8000, 3000, 1935, 8008})

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLError(ValueError):
    """Raised when a URL must not be fetched."""


def looks_like_address(host: str) -> bool:
    """True when *host* is an IP literal rather than a hostname."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _address_is_public(ip) -> bool:
    """Return True only for addresses that are safe to dial out to."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    # Carrier-grade NAT (100.64.0.0/10) is "shared address space", not private.
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return False
    # Documentation / benchmarking ranges are never real CDNs.
    for net in ("192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24"):
        if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network(net):
            return False
    return True


def _resolve(host: str, port: int):
    """Resolve *host* to a list of ipaddress objects (empty on failure)."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    addresses = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw = sockaddr[0]
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return addresses


def ensure_public_url(url: str, *, resolve: bool = True) -> str:
    """Validate *url* and return its normalized form.

    Raises :class:`UnsafeURLError` when the URL is malformed, uses a scheme we
    do not proxy, targets a forbidden port, or resolves to a non-public
    address. The returned value is safe to hand to ``requests``.

    ``resolve=False`` skips the DNS check (handy for unit tests); callers on
    the request path should never pass it.
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("No URL provided")

    candidate = url.strip()
    if len(candidate) > 4096:
        raise UnsafeURLError("URL is too long")

    # Reject control characters / whitespace tricks such as "http://ok\r\nHost: x".
    if any(ch.isspace() or ord(ch) < 0x20 for ch in candidate):
        raise UnsafeURLError("URL contains whitespace or control characters")

    parts = urlsplit(candidate)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError("Only http and https URLs can be proxied")

    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    try:
        port = parts.port
    except ValueError:
        raise UnsafeURLError("URL has an invalid port")
    if port is None:
        port = 443 if scheme == "https" else 80
    if port not in _ALLOWED_PORTS:
        raise UnsafeURLError(f"Port {port} is not allowed")

    host = host.rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    if host in _BLOCKED_HOSTNAMES:
        raise UnsafeURLError("That host is not allowed")
    if any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        raise UnsafeURLError("That host is not allowed")

    if looks_like_address(host):
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise UnsafeURLError("Invalid IP address")
        if not _address_is_public(ip):
            raise UnsafeURLError("That address is not allowed")
    else:
        if not resolve:
            pass
        else:
            addresses = _resolve(host, port)
            if not addresses:
                raise UnsafeURLError("Could not resolve that host")
            if not all(_address_is_public(ip) for ip in addresses):
                raise UnsafeURLError("That address is not allowed")

    # Strip credentials so they never reach the upstream server or our logs.
    netloc = parts.hostname or ""
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def is_public_url(url: str) -> bool:
    """Boolean convenience wrapper around :func:`ensure_public_url`."""
    try:
        ensure_public_url(url)
    except UnsafeURLError:
        return False
    except Exception:
        return False
    return True


def guarded_get(url, *, session=None, max_redirects=5, resolve=True, **kwargs):
    """``requests.get`` that validates the URL and every redirect hop.

    Returns the upstream response. Raises :class:`UnsafeURLError` as soon as a
    hop fails validation, so callers can answer with 400 instead of leaking an
    internal response.
    """
    import requests
    from urllib.parse import urljoin

    target = ensure_public_url(url, resolve=resolve)
    getter = session.get if session is not None else requests.get
    kwargs['allow_redirects'] = False

    response = None
    for _ in range(max_redirects + 1):
        response = getter(target, **kwargs)
        if not response.is_redirect:
            return response
        location = response.headers.get('Location', '')
        response.close()
        if not location:
            raise UnsafeURLError("Redirect without a target")
        # Resolve relative Location headers against the current URL.
        target = ensure_public_url(urljoin(target, location), resolve=resolve)
    raise UnsafeURLError("Too many redirects")