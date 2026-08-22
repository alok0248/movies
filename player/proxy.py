"""
Django views for Videasy Player proxy and cookie endpoints.

Add to urls.py:
    from player.proxy import proxy_view, cookies_view, health_view
    urlpatterns = [
        path('proxy/<path:target>', proxy_view, name='videasy-proxy'),
        path('api/cookies', cookies_view, name='videasy-cookies'),
        path('api/health', health_view, name='videasy-health'),
    ]

Or include this file's URL patterns:
    from player.proxy import urlpatterns
    urlpatterns += urlpatterns
"""

import json
import re
import http.cookiejar
import urllib.request
from urllib.parse import urlparse

from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_GET


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
VIDEASY_ORIGIN = "https://player.videasy.to"


@require_GET
def health_view(request):
    return JsonResponse({"status": "ok"})


@require_GET
def cookies_view(request):
    """Fetch DDoS Guard cookies from videasy.to."""
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj)
        )
        opener.addheaders = [("User-Agent", USER_AGENT)]
        resp = opener.open(f"{VIDEASY_ORIGIN}/", timeout=10)
        resp.read()
        cookies = {}
        for c in cj:
            cookies[c.name] = c.value
        for hdr in resp.headers.get_all("Set-Cookie") or []:
            m = re.match(r"([^=]+)=([^;]+)", hdr)
            if m:
                cookies[m.group(1).strip()] = m.group(2).strip()
        return JsonResponse({"cookies": cookies, "count": len(cookies)})
    except Exception as e:
        return JsonResponse({"cookies": {}, "error": str(e)})


def proxy_view(request, target):
    """
    Proxy requests to CDN/streams.
    Reconstructs the full URL from the path, tries with videasy.to Origin,
    falls back to no Origin on 403.
    """
    # Reconstruct full URL — auto-detect http vs https
    if target.startswith("http/"):
        target_url = "http://" + target[5:]
    else:
        target_url = "https://" + target

    # Try with videasy.to Origin first (needed for CDN servers)
    response = _fetch_url(target_url, origin=VIDEASY_ORIGIN, referer=f"{VIDEASY_ORIGIN}/")
    if response is not None:
        return response

    # Retry without Origin (for servers that reject fake Origin)
    response = _fetch_url(target_url, origin=None, referer=None)
    if response is not None:
        return response

    return HttpResponse("Proxy error: all attempts failed", status=502)


def _fetch_url(target_url, origin=None, referer=None):
    """Fetch a URL and return an HttpResponse, or None on failure."""
    try:
        req = urllib.request.Request(target_url)
        req.add_header("User-Agent", USER_AGENT)
        if origin:
            req.add_header("Origin", origin)
        if referer:
            req.add_header("Referer", referer)
        resp = urllib.request.urlopen(req, timeout=30)
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        data = resp.read()
        http_resp = HttpResponse(data, content_type=content_type)
        http_resp["Access-Control-Allow-Origin"] = "*"
        return http_resp
    except urllib.error.HTTPError as e:
        if e.code == 403 and origin:
            return None  # Signal to retry without origin
        return HttpResponse(f"Proxy error: {e.code}", status=e.code)
    except Exception as e:
        return HttpResponse(f"Proxy error: {e}", status=502)
