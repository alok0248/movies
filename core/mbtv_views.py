"""MovieBoxTV (aoneroom) relay + media proxy.

All protocol logic (signing, JWT, matching, language resolution) lives in
static/js/mbtv-extract.js and runs in the visitor's browser. These two views
are deliberately dumb:

* mbtv_relay_view -- forwards an already-signed request to tv.aoneroom.com.
  The browser builds every header (signature, guest token, device blob);
  the server only enforces the request shape and reads back the guest JWT
  from the ``x-user`` response header during bootstrap.

* mbtv_media_view -- range-passing stream proxy so signed/HLS/DASH URLs can
  play from the browser without CORS/ORB problems. Mirrors the portable
  player's /media handler: forward Range, stream chunks, rewrite MPD
  segment URLs through itself, and append the sign cookie for DASH.
"""
import base64
import gzip
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

CONNECT_HOST = 'tv.aoneroom.com'

# Browsers cannot send okhttp's UA without extensions, and aoneroom does not
# check UA anyway (the app sends okhttp/4.12.0). We forward the browser's own
# signature headers verbatim and let the relay fill the transport-level gaps.

RELAY_TOKEN_SALT = 'mbtv-relay-v1'
MEDIA_TOKEN_SALT = 'mbtv-media-v1'

MAX_RELAY_BODY = 64 * 1024
MAX_MEDIA_REDIRECTS = 3


def _client_error(msg, status=400):
    return JsonResponse({'ok': False, 'error': msg}, status=status)


@csrf_exempt
@require_POST
def mbtv_relay_view(request):
    """Forward an already-signed browser request to the aoneroom gateway.

    Body (JSON, built entirely by static/js/mbtv-extract.js):
      m: HTTP method (GET/POST)
      p: BFF path, must start with /wefeed-tv-bff/
      q: full query string (includes host=api6.aoneroom.com)
      b: JSON body string ('' for GET)
      h: header dict produced by the browser signer
      x: MD5(shape-token) binding method/path/query/body
    """
    try:
        if len(request.body) > MAX_RELAY_BODY:
            return _client_error('payload too large', 413)
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return _client_error('invalid JSON')

    method = str(payload.get('m') or 'GET').upper()
    if method not in ('GET', 'POST'):
        return _client_error('method not allowed')

    path = str(payload.get('p') or '')
    if not path.startswith('/wefeed-tv-bff/'):
        return _client_error('path not allowed')

    query = str(payload.get('q') or '')
    body = str(payload.get('b') or '')
    headers_in = payload.get('h') or {}
    token = str(payload.get('x') or '')

    if body and method != 'POST':
        return _client_error('body on GET request')

    # Shape token: same formula the browser used (no secret, just an
    # integrity check so the relay isn't an open URL fetcher).
    expected = hashlib.md5(
        (RELAY_TOKEN_SALT + '|' + method + '|' + path + '|' + query + '|' + body).encode('utf-8')
    ).hexdigest()
    if token != expected:
        return _client_error('bad token')

    allowed_headers = (
        'x-tr-signature', 'x-client-token', 'x-client-info',
        'x-client-status', 'authorization', 'content-type',
    )
    headers = {'Accept-Encoding': 'gzip'}
    for name in allowed_headers:
        val = headers_in.get(name) or headers_in.get(name.title())
        if val:
            headers[name] = str(val)
    if body:
        headers.setdefault('Content-Type', 'application/json; charset=UTF-8')

    url = f'https://{CONNECT_HOST}{path}' + (f'?{query}' if query else '')
    req = urllib.request.Request(url, data=body.encode('utf-8') if body else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            resp_headers = {k.lower(): v for k, v in r.headers.items()}
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        resp_headers = {k.lower(): v for k, v in e.headers.items()}
        status = e.code
    except (urllib.error.URLError, OSError) as e:
        return JsonResponse({'ok': False, 'error': f'upstream error: {e}'}, status=502)

    enc = resp_headers.get('content-encoding', '')
    if enc == 'gzip' or raw[:2] == b'\x1f\x8b':
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        data = {'raw': raw[:500].decode('utf-8', 'replace')}

    resp = JsonResponse(data, safe=False, status=status)
    if 'x-user' in resp_headers:
        # Bootstrap response: hand the guest JWT back to the browser so it
        # can cache it and attach it to future requests itself. The x-user
        # value is a JSON object; extract just the token string.
        try:
            xuser = json.loads(resp_headers['x-user'])
            resp['X-MBTV-JWT'] = str(xuser.get('token') or '')
        except (ValueError, TypeError):
            resp['X-MBTV-JWT'] = resp_headers['x-user']
    return resp


def _media_token(url):
    return hashlib.md5((MEDIA_TOKEN_SALT + url).encode('utf-8')).hexdigest()


_MBTV_TMDB_KEY_CACHE = ['']


def _tmdb_api_key():
    """Site's TMDB key from the same source the detail views use."""
    if _MBTV_TMDB_KEY_CACHE[0]:
        return _MBTV_TMDB_KEY_CACHE[0]
    from django.conf import settings as dj_settings
    key = getattr(dj_settings, 'TMDB_API_KEY', '')
    if key:
        _MBTV_TMDB_KEY_CACHE[0] = key
    return key


@csrf_exempt
@require_POST
def mbtv_title_view(request):
    """Resolve a TMDB id to (title, year) using the server's TMDB key.

    The browser cannot call TMDB directly without exposing a key, and the
    TMDB API itself has no CORS-restricted endpoints for this. Shape token
    same pattern as the relay.
    """
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return _client_error('invalid JSON')
    tmdb_id = str(payload.get('id') or '').strip()
    media_type = 'tv' if str(payload.get('type') or '') == 'tv' else 'movie'
    token = str(payload.get('x') or '')
    expected = hashlib.md5(('mbtv-title-v1|' + tmdb_id + '|' + media_type).encode('utf-8')).hexdigest()
    if token != expected:
        return _client_error('bad token')
    if not tmdb_id.isdigit():
        return _client_error('bad id')

    from django.conf import settings as dj_settings
    key = _tmdb_api_key()
    if not key:
        return _client_error('no TMDB key configured', 500)

    # Local TMDB DB first — zero network flakiness; live API only as fallback.
    try:
        from core.models import TMDBMovie, TMDBTV
        if media_type == 'movie':
            m = TMDBMovie.objects.filter(id=tmdb_id).only('title', 'release_date').first()
            if m and m.title:
                return JsonResponse({'title': m.title, 'year': (m.release_date or '')[:4]})
        else:
            t = TMDBTV.objects.filter(id=tmdb_id).only('name', 'first_air_date').first()
            if t and t.name:
                return JsonResponse({'title': t.name, 'year': (t.first_air_date or '')[:4]})
    except Exception:
        pass

    url = (f'https://api.themoviedb.org/3/{media_type}/{tmdb_id}?language=en-US'
           f'&api_key={urllib.parse.quote(key)}')
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    data = None
    last_err = None
    # TMDB intermittently resets connections (~40% of calls from some
    # networks) — retry so a single dropped packet doesn't kill playback.
    import time as _time
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode('utf-8', 'replace'))
            break
        except urllib.error.HTTPError as e:
            return _client_error(f'TMDB HTTP {e.code}', 502)
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            _time.sleep(0.6 * (attempt + 1))
    if data is None:
        return _client_error(f'TMDB error: {last_err}', 502)
    return JsonResponse({
        'title': data.get('title') or data.get('name') or '',
        'year': (data.get('release_date') or data.get('first_air_date') or '')[:4],
    })


def _proxy_query(u, token):
    return MEDIA_PROXY_URL_TEMPLATE + '?' + urllib.parse.urlencode({'u': u, 'x': token})


# Template filled by urls.py include; kept as a plain string so this module
# has no import-time dependency on the URLconf.
MEDIA_PROXY_URL_TEMPLATE = '/mbtv-media/'


def _mpd_rewrite(body, base_url):
    """Rewrite every http(s) URL inside a DASH manifest to route through us."""
    def _sub(m):
        return _proxy_query(m.group(0), _media_token(m.group(0)))

    return re.sub(r'https?://[^\s<>"\']+', _sub, body)


def mbtv_media_view(request):
    """Range-passing proxy for MovieBoxTV media bytes.

    GET /mbtv-media/?u=<url>&x=<md5 token>
    """
    murl = request.GET.get('u', '')
    token = request.GET.get('x', '')
    if not murl.startswith('https://') or token != _media_token(murl):
        return HttpResponse('forbidden', status=403)

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'}
    rng = request.META.get('HTTP_RANGE')
    if rng:
        headers['Range'] = rng

    redirects = 0
    try:
        while True:
            try:
                up = urllib.request.urlopen(urllib.request.Request(murl, headers=headers), timeout=30)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 307, 308) and redirects < MAX_MEDIA_REDIRECTS:
                    redirects += 1
                    murl = e.headers.get('Location', murl)
                    continue
                up = e
            break
    except (urllib.error.URLError, OSError) as e:
        return HttpResponse(f'upstream error: {e}', status=502)

    code = up.getcode() or 200
    ctype = up.headers.get('Content-Type') or 'application/octet-stream'
    content_length = up.headers.get('Content-Length')
    content_range = up.headers.get('Content-Range')

    # Small bodies: buffer fully so MPD manifests can be rewritten.
    # Large bodies: stream through.
    is_manifest = ('dash+xml' in ctype or murl.split('?')[0].endswith('.mpd'))
    if is_manifest:
        try:
            body = up.read()
        finally:
            up.close()
        try:
            text = body.decode('utf-8', 'replace')
            text = _mpd_rewrite(text, murl)
            body = text.encode('utf-8')
        except Exception:
            pass
        resp = HttpResponse(body, content_type=ctype)
        resp['Cache-Control'] = 'no-store'
        return resp

    def chunk_iter():
        try:
            while True:
                chunk = up.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                up.close()
            except Exception:
                pass

    resp = StreamingHttpResponse(chunk_iter(), content_type=ctype, status=code)
    resp['Accept-Ranges'] = 'bytes'
    resp['Cache-Control'] = 'no-store'
    if content_length:
        resp['Content-Length'] = content_length
    if content_range:
        resp['Content-Range'] = content_range
    return resp
