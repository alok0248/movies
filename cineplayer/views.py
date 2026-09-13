"""
CinePlayer views — standalone browser player with gateway relay fallback.

Architecture:
  - Primary: browser calls the cinevault gateway (port 8787) directly (CORS: *)
  - Fallback: Django relay endpoints handle the same API when gateway is unreachable

All API calls happen in the browser. The Django views only serve as a relay
when the external gateway is not available.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

GATEWAY = os.environ.get('CINEPLAYER_GATEWAY', 'http://127.0.0.1:8787')

# MovieIn API constants (from oracle_play.py)
SALT = '47Q8tBqO4YqrMHf4'
BASE = 'https://moviein.ajfysu.com/'
TMDB_KEY = os.environ.get('TMDB_API_KEY', '')

# AES decryption for encrypted API responses
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    _AES_KEY = b'0123456789123456'
    _AES_IV = b'2015030120123456'
    _HAS_AES = True
except ImportError:
    _HAS_AES = False
import base64 as _b64
import gzip as _gzip
import re as _re


def _decrypt(body):
    """Decrypt AES-CBC encrypted API response body."""
    if not _HAS_AES:
        return body
    try:
        clean = _re.sub(r'[^A-Za-z0-9+/=]', '', body)
        raw = _b64.b64decode(clean)
        cipher = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV), backend=default_backend())
        decryptor = cipher.decryptor()
        out = decryptor.update(raw) + decryptor.finalize()
        pad_len = out[-1]
        out = out[:-pad_len]
        if out[:2] == b'\x1f\x8b':
            out = _gzip.decompress(out)
        return out.decode('utf-8', 'replace')
    except Exception:
        return body

# Per-device identity cache: alias -> device_id
_did_map = {}
_did_lock = __import__('threading').Lock()


def _gen_device_id():
    import random
    return ''.join(random.choice('0123456789abcdef') for _ in range(16))


def _alias_to_did(alias):
    """Map a browser alias to a stable 16-hex device_id."""
    alias = (alias or '').strip()
    if not alias:
        return _gen_device_id()
    with _did_lock:
        did = _did_map.get(alias)
        if did is None:
            did = _gen_device_id()
            _did_map[alias] = did
    return did


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest().upper()


def _session_headers(device_id, token=''):
    t = str(int(time.time() * 1000))
    return {
        'User-Agent': 'okhttp/4.12.0', 'Accept': '*/*',
        'Accept-Encoding': 'gzip, deflate', 'app_id': 'moviein',
        'package_name': 'com.fvvcl.flickverse', 'version': '40000',
        'sys_platform': '2', 'mob_mfr': 'google', 'mobmodel': 'SM-S908E',
        'sysrelease': '9', 'device_id': device_id, 'gaid': '',
        'channel_code': 'moviein_3001', 'androidid': device_id,
        'cur_time': t, 'token': token,
        'sign': _md5(SALT + device_id + t),
        'is_vvv': '1', 'is_language': '0', 'is_display': '0',
        'app_language': 'en', 'en_al': '0',
        'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8',
    }


def _proxy_api(path, fields, device_id):
    """Proxy a POST to the cinevault gateway (port 8787), which talks to speedracelight."""
    # Try the running cinevault gateway first
    try:
        body = urllib.parse.urlencode(fields).encode('utf-8')
        url = f'{GATEWAY}/api/relay/{path}'
        req = urllib.request.Request(url, data=body, headers={
            'X-Device-Id': device_id,
            'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8',
        }, method='POST')
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception:
        pass
    # Fallback: direct to moviein API (AES-encrypted responses)
    try:
        body = urllib.parse.urlencode(fields).encode('utf-8')
        headers = _session_headers(device_id)
        url = BASE + path
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode('utf-8', 'replace')
            decrypted = _decrypt(raw)
            try:
                return json.loads(decrypted)
            except Exception:
                return {'result': []}
    except Exception as e:
        return {'error': str(e)}


# ═══════════════════════════════════════════════════════════════════
# Main player page
# ═══════════════════════════════════════════════════════════════════

@require_GET
def cineplayer_index(request):
    """Render the CinePlayer page."""
    try:
        from core.models import CinePlayerConfig
        config = CinePlayerConfig.get_config()
    except Exception:
        config = None

    return render(request, 'cineplayer/player.html', {
        'gateway': config.gateway_url if config else GATEWAY,
        'user': request.user,
        'config': config,
    })


# ═══════════════════════════════════════════════════════════════════
# Django relay endpoints (fallback when gateway is unreachable)
# These are called by the browser via /cineplayer/api/...
# ═══════════════════════════════════════════════════════════════════

@require_GET
def api_whoami(request):
    """Register/return this client's device identity.
    
    If the user is logged in, link the machine ID to their email so
    their play history is linked to their account.
    Also activates the invite referral for this device.
    """
    alias = request.GET.get('did', '').strip()
    device_id = _alias_to_did(alias) if alias else _gen_device_id()
    
    # If user is logged in, link this machine ID to their email
    email = request.GET.get('email', '').strip()
    if not email and hasattr(request, 'user') and request.user.is_authenticated:
        email = request.user.email
    
    if email and alias:
        try:
            from core.models import DeviceIdentity
            DeviceIdentity.link(email=email, machine_id=alias, device_id=device_id)
        except Exception:
            pass  # Don't break playback if linking fails
    
    # Activate invite for this device (one-time per device_id)
    try:
        from core.models import CinePlayerConfig
        config = CinePlayerConfig.get_config()
        invite_code = config.invite_code or '209173008'
        share_url = config.share_url or ''
    except Exception:
        invite_code = '209173008'
        share_url = ''
    
    # Try gateway's whoami first (handles invite activation)
    invite_activated = False
    try:
        gw_url = f'{GATEWAY}/api/whoami?did={urllib.parse.quote(alias)}&email={urllib.parse.quote(email)}' if email else f'{GATEWAY}/api/whoami?did={urllib.parse.quote(alias)}'
        req = urllib.request.Request(gw_url, headers={'X-Device-Id': alias})
        with urllib.request.urlopen(req, timeout=5) as r:
            gw_data = json.loads(r.read().decode('utf-8', 'replace'))
            invite_activated = True
    except Exception:
        pass
    
    # If gateway unreachable, activate invite via speedracelight API directly
    if not invite_activated and device_id:
        try:
            # Visit the share link to register the referral click
            if share_url:
                req = urllib.request.Request(share_url, headers={
                    'User-Agent': 'Mozilla/5.0 (Linux; Android 9; SM-S908E) AppleWebKit/537.36',
                    'Referer': 'https://moviein.ajfysu.com/'
                })
                urllib.request.urlopen(req, timeout=10).read(4096)
            
            # Call api/public/init with invited_by to complete attribution
            body = urllib.parse.urlencode({
                'invited_by': invite_code,
                'is_install': '1',
                'fb_attribution': ''
            }).encode('utf-8')
            headers = _session_headers(device_id)
            url = 'https://moviein.ajfysu.com/api/public/init'
            req = urllib.request.Request(url, data=body, headers=headers, method='POST')
            urllib.request.urlopen(req, timeout=10).read(4096)
            invite_activated = True
        except Exception:
            pass
    
    return JsonResponse({
        'alias': alias,
        'device_id': device_id,
        'identity': 'registered',
        'email': email or '',
        'invite_activated': invite_activated,
    })


@require_GET
def api_resolve_tmdb(request, tmdb_id):
    """Resolve a TMDB ID to a vod_id via the cinevault gateway."""
    alias = request.headers.get('X-Device-Id', '')
    device_id = _alias_to_did(alias) if alias else _gen_device_id()

    # Try the running cinevault gateway first (has mapping + search)
    try:
        url = f'{GATEWAY}/api/resolve/tmdb/{tmdb_id}'
        req = urllib.request.Request(url, headers={'X-Device-Id': alias})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
            if data.get('vod_id'):
                return JsonResponse(data)
    except Exception:
        pass

    # Fallback: fetch title from TMDB API then search via gateway relay
    title, year, type_pid = _fetch_tmdb_info(tmdb_id)
    if not title:
        return JsonResponse({'error': f'TMDB ID {tmdb_id} not found'}, status=404)

    # Try multiple search strategies
    items = []

    # Strategy 1: api/search/result with keyword (AES-decrypted)
    search_result = _proxy_api('api/search/result', {
        'kw': title, 'pn': '1', 'page_size': '30'
    }, device_id)
    items = (search_result or {}).get('result') or []

    # Strategy 2: api/search/screen (browse feed — returns flat items)
    if not items:
        screen_result = _proxy_api('api/search/screen', {
            'type_id': type_pid or '1', 'type': '', 'area': '',
            'year': '', 'sort': '', 'pn': '1'
        }, device_id)
        raw = (screen_result or {}).get('result') or []
        # search/screen returns flat items OR nested modules
        for item in raw:
            if 'vod_name' in item:
                # Flat item (direct vod object)
                items.append(item)
            else:
                # Nested module with block_list
                for block in (item.get('block_list') or []):
                    for vod in (block.get('vod_list') or []):
                        items.append(vod)

    if not items:
        return JsonResponse({'error': f'No match for "{title}" ({year})'}, status=404)

    best = _find_best_match(title, year, type_pid, items)
    if not best:
        return JsonResponse({'error': f'No match for "{title}"'}, status=404)

    # API uses 'id' field, not 'vod_id'
    vod_id = str(best.get('vod_id') or best.get('id') or '')
    name = best.get('vod_name') or best.get('name') or title
    return JsonResponse({
        'source': 'tmdb',
        'source_id': str(tmdb_id),
        'vod_id': vod_id,
        'name': name,
        'method': 'search',
        'play_url': f'/play/{vod_id}?ep=0',
        'api_url': f'/api/title/{vod_id}',
    })


@require_GET
def api_title(request, vod_id):
    """Get title info (episode list, metadata) for a vod_id via gateway."""
    alias = request.headers.get('X-Device-Id', '')

    # Try the running cinevault gateway first (needs oracle for info_new)
    try:
        url = f'{GATEWAY}/api/title/{vod_id}'
        req = urllib.request.Request(url, headers={'X-Device-Id': alias})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
            if not data.get('error'):
                return JsonResponse(data)
    except Exception:
        pass

    # Gateway oracle is down — return minimal info so the cineplayer can still play
    # The browser-side cineplayer will use /stream/<vod_id> which also needs oracle
    # but the embed players on detail pages bypass this entirely
    return JsonResponse({
        'result': {
            'vod_id': vod_id,
            'vod_name': '',
            'vod_collection': [],
            'vod_serial': '1',
        },
        'error': 'oracle unreachable — title info unavailable (embed players still work)',
    })


@require_GET
def api_browse(request):
    """Search by keyword via the cinevault gateway."""
    kw = request.GET.get('kw', '')
    alias = request.headers.get('X-Device-Id', '')

    # Try the running cinevault gateway first
    try:
        url = f'{GATEWAY}/browse?kw={urllib.parse.quote(kw)}'
        req = urllib.request.Request(url, headers={'X-Device-Id': alias})
        with urllib.request.urlopen(req, timeout=15) as r:
            return JsonResponse(json.loads(r.read().decode('utf-8', 'replace')), safe=False)
    except Exception:
        pass

    return JsonResponse([], safe=False)


@require_GET
def api_audio(request, vod_id):
    """Get audio language list for an episode."""
    ep = int(request.GET.get('ep', 0))
    # Delegate to gateway if available
    try:
        url = f'{GATEWAY}/api/audio/{vod_id}?ep={ep}'
        req = urllib.request.Request(url, headers={'X-Device-Id': request.headers.get('X-Device-Id', '')})
        with urllib.request.urlopen(req, timeout=10) as r:
            return JsonResponse(json.loads(r.read().decode('utf-8', 'replace')))
    except Exception:
        return JsonResponse({'langs': [], 'default': None})


@require_GET
def api_tracks(request, vod_id):
    """Get audio/video/subtitle track info."""
    ep = int(request.GET.get('ep', 0))
    try:
        url = f'{GATEWAY}/api/tracks/{vod_id}?ep={ep}'
        req = urllib.request.Request(url, headers={'X-Device-Id': request.headers.get('X-Device-Id', '')})
        with urllib.request.urlopen(req, timeout=10) as r:
            return JsonResponse(json.loads(r.read().decode('utf-8', 'replace')))
    except Exception:
        return JsonResponse({'audio': {'langs': []}, 'video': {}, 'subs': []})


@require_GET
def api_meta(request, vod_id):
    """Get title metadata."""
    try:
        url = f'{GATEWAY}/api/meta/{vod_id}'
        req = urllib.request.Request(url, headers={'X-Device-Id': request.headers.get('X-Device-Id', '')})
        with urllib.request.urlopen(req, timeout=10) as r:
            return JsonResponse(json.loads(r.read().decode('utf-8', 'replace')))
    except Exception:
        return JsonResponse({'error': 'metadata unavailable'})


@require_GET
def api_subs_vtt(request, vod_id):
    """Get subtitles as WebVTT."""
    ep = int(request.GET.get('ep', 0))
    try:
        url = f'{GATEWAY}/subs/{vod_id}.vtt?ep={ep}'
        req = urllib.request.Request(url, headers={'X-Device-Id': request.headers.get('X-Device-Id', '')})
        with urllib.request.urlopen(req, timeout=10) as r:
            from django.http import HttpResponse
            return HttpResponse(r.read(), content_type='text/vtt; charset=utf-8')
    except Exception:
        from django.http import HttpResponseNotFound
        return HttpResponseNotFound('no subtitles')


@require_GET
def api_stream(request, vod_id):
    """Get stream URL for an episode. Tries gateway first, then speedracelight."""
    ep = int(request.GET.get('ep', 0))
    alias = request.headers.get('X-Device-Id', '')
    device_id = _alias_to_did(alias) if alias else _gen_device_id()

    # Try gateway stream first
    try:
        url = f'{GATEWAY}/stream/{vod_id}?ep={ep}'
        req = urllib.request.Request(url, headers={'X-Device-Id': alias})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
            if data.get('url'):
                return JsonResponse(data)
    except Exception:
        pass

    # Fallback: get stream URL via info_new (needs oracle sign)
    try:
        ts = str(int(time.time() * 1000))
        # Try oracle sign first
        sign = None
        try:
            oracle_url = f'http://127.0.0.1:17000/control?msg=verify&device_id={device_id}{vod_id}&ts={ts}'
            with urllib.request.urlopen(oracle_url, timeout=5) as r:
                sign = r.read().decode('utf-8', 'replace').strip()
        except Exception:
            pass

        if sign:
            result = _proxy_api('api/vod/info_new', {
                'vod_id': vod_id, 'cur_time': ts, 'sign': sign, 'audio_type': ''
            }, device_id)
            coll = (result or {}).get('result', {}).get('vod_collection') or []
            if coll and ep < len(coll):
                ep_url = coll[ep].get('vod_url', '')
                if ep_url:
                    return JsonResponse({'url': ep_url})
    except Exception:
        pass

    return JsonResponse({'error': 'stream unavailable', 'url': ''}, status=404)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _get_tmdb_key():
    """Get TMDB API key from environment or database."""
    if TMDB_KEY:
        return TMDB_KEY
    try:
        from core.models import TMDBApiKey
        key_obj = TMDBApiKey.objects.filter(is_active=True).order_by('last_used_at').first()
        if key_obj:
            return key_obj.key
    except Exception:
        pass
    return ''


def _fetch_tmdb_info(tmdb_id):
    """Fetch title info from TMDB API. Returns (title, year, type_pid)."""
    tmdb_key = _get_tmdb_key()
    if not tmdb_key:
        return None, None, None

    results = []
    for media_type in ('movie', 'tv'):
        url = f'https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={tmdb_key}&language=en-US'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'CinePlayer/1.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode('utf-8', 'replace'))
                title = data.get('title') or data.get('name') or ''
                if media_type == 'movie':
                    year = (data.get('release_date') or '')[:4]
                else:
                    year = (data.get('first_air_date') or '')[:4]
                type_pid = 1 if media_type == 'movie' else 2
                if title:
                    results.append((title, year, type_pid))
        except Exception:
            continue

    if not results:
        return None, None, None
    if len(results) == 1:
        return results[0]
    # Prefer more recent
    best = max(results, key=lambda x: int(x[1] or '0') if x[1] else 0)
    return best


def _normalize_title(name):
    """Lowercase, strip parentheticals, normalize for matching."""
    s = (name or '').lower().strip()
    s = re.sub(r'\s*\(.*?\)\s*', ' ', s)
    s = re.sub(r'\s*-\s*season\s+\d+', '', s)
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _find_best_match(title, year, type_pid, items):
    """Find the best matching item from search results."""
    q_norm = _normalize_title(title)
    best = None
    best_score = -1

    for it in items:
        it_name = it.get('vod_name') or it.get('name') or ''
        it_norm = _normalize_title(it_name)

        if it_norm == q_norm:
            score = 100
        elif it_norm.startswith(q_norm):
            score = 80
        elif q_norm in it_norm:
            score = 60
        else:
            q_tokens = set(q_norm.split())
            it_tokens = set(it_norm.split())
            overlap = len(q_tokens & it_tokens)
            if overlap == 0:
                continue
            score = 40 * overlap // max(len(q_tokens), 1)

        if year:
            it_year = it.get('year') or ''
            if str(year) in str(it_year):
                score += 20

        if score > best_score:
            best_score = score
            best = it

    return best
