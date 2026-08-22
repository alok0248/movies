from django.http import StreamingHttpResponse, JsonResponse
from django.views.decorators.http import require_GET
from django.core.cache import cache
import time as _time
import json as _json
import logging

logger = logging.getLogger(__name__)


def _deps():
    from core import views
    return views


@require_GET
def player_sources_stream_view(request):
    """SSE streaming - plays first source ASAP, extracts rest in background."""
    V = _deps()
    NL = chr(10) + chr(10)

    tmdb_id = request.GET.get('tmdb_id', '')
    media_type = request.GET.get('type', 'movie')
    season = request.GET.get('season', '')
    episode = request.GET.get('episode', '')

    if not tmdb_id:
        def gen():
            yield 'data: ' + _json.dumps({'success': False, 'error': 'tmdb_id required'}) + NL
        return StreamingHttpResponse(gen(), content_type='text/event-stream')

    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        def gen():
            yield 'data: ' + _json.dumps({'success': False, 'error': 'Invalid tmdb_id'}) + NL
        return StreamingHttpResponse(gen(), content_type='text/event-stream')

    timestamp = str(int(_time.time() * 1000))

    def generate():
        # Yield immediately to flush headers and start connection
        yield 'data: ' + _json.dumps({'type': 'connecting'}) + NL
        sent = set()
        # Use cached seed if available (saves ~0.7s)
        seed_cache_key = f'stream_seed_{tmdb_id}'
        seed = cache.get(seed_cache_key, '')
        if not seed:
            try:
                seed = V._vk_get_seed(V._VIDKING_API_BASE, tmdb_id)
                if seed:
                    cache.set(seed_cache_key, seed, 300)  # Cache for 5 min
            except Exception:
                seed = ''

        if not seed:
            yield 'data: ' + _json.dumps({'type': 'error', 'message': 'Could not fetch seed'}) + NL
            return

        for server_name, endpoint in V._VIDKING_SERVERS.items():
            try:
                result = V._vk_fetch_sources_for_server(
                    server_name, endpoint, tmdb_id, media_type,
                    title='', year='', season_id=season,
                    episode_id=episode, imdb_id='',
                    seed=seed, timestamp=timestamp,
                )
                sources = result.get('sources', [])
                if isinstance(sources, list) and sources:
                    for src in sources:
                        if isinstance(src, dict) and src.get('url') and src['url'] not in sent:
                            sent.add(src['url'])
                            lang = src.get('language', '') or src.get('audioLanguage', '') or src.get('audio', '') or ''
                            quality = src.get('quality', '?')
                            yield 'data: ' + _json.dumps({
                                'type': 'source',
                                'url': src['url'],
                                'quality': quality,
                                'language': lang,
                                'server': server_name,
                            }) + NL
            except Exception as e:
                logger.debug('stream: %s failed: %s', server_name, e)
                continue

        yield 'data: ' + _json.dumps({'type': 'done', 'tmdbId': tmdb_id}) + NL

    return StreamingHttpResponse(generate(), content_type='text/event-stream')


@require_GET
def prefetch_sources_view(request):
    """Warm the seed cache so the streaming endpoint is faster."""
    V = _deps()
    tmdb_id = request.GET.get('tmdb_id', '')
    if not tmdb_id:
        return JsonResponse({'ok': True})
    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        return JsonResponse({'ok': True})
    
    seed_cache_key = f'stream_seed_{tmdb_id}'
    seed = cache.get(seed_cache_key, '')
    if not seed:
        try:
            seed = V._vk_get_seed(V._VIDKING_API_BASE, tmdb_id)
            if seed:
                cache.set(seed_cache_key, seed, 300)
        except Exception:
            pass
    return JsonResponse({'ok': True, 'cached': bool(seed)})
