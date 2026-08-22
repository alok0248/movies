from django.http import StreamingHttpResponse
from django.views.decorators.http import require_GET
import time as _time
import json as _json
import logging

logger = logging.getLogger(__name__)


def _streaming_view_deps():
    """Import deps from core.views lazily"""
    from core import views
    return {
        '_vk_fetch_tmdb_info': views._vk_fetch_tmdb_info,
        '_vk_get_seed': views._vk_get_seed,
        '_vk_fetch_sources_for_server': views._vk_fetch_sources_for_server,
        '_VIDKING_API_BASE': views._VIDKING_API_BASE,
        '_VIDKING_SERVERS': views._VIDKING_SERVERS,
    }


@require_GET
def player_sources_stream_view(request):
    """SSE streaming endpoint - sends sources as each server completes."""
    deps = _streaming_view_deps()
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

    try:
        tmdb_info = deps['_vk_fetch_tmdb_info'](tmdb_id, media_type)
    except Exception:
        tmdb_info = {'title': '', 'year': '', 'imdbId': ''}

    title = tmdb_info.get('title', '')
    year = tmdb_info.get('year', '')
    imdb_id = tmdb_info.get('imdbId', '')

    seed = ''
    for attempt in range(2):
        try:
            seed = deps['_vk_get_seed'](deps['_VIDKING_API_BASE'], tmdb_id)
            break
        except Exception:
            if attempt == 0:
                _time.sleep(0.3)

    def generate():
        sent = set()
        for server_name, endpoint in deps['_VIDKING_SERVERS'].items():
            try:
                result = deps['_vk_fetch_sources_for_server'](
                    server_name, endpoint, tmdb_id, media_type,
                    title=title, year=year, season_id=season,
                    episode_id=episode, imdb_id=imdb_id,
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
        yield 'data: ' + _json.dumps({'type': 'done', 'title': title, 'year': year, 'tmdbId': tmdb_id}) + NL

    return StreamingHttpResponse(generate(), content_type='text/event-stream')
