"""Local YouTube trailer proxy - streams videos through Django server."""
import json
import re
import subprocess
import time
import hashlib
import urllib.request
import urllib.error
from django.http import HttpResponse, StreamingHttpResponse, JsonResponse
from django.views.decorators.http import require_GET
from django.views.decorators.csrf import csrf_exempt

# In-memory cache
_stream_cache = {}
_cache_ttl = 3600


def _get_video_streams(video_id):
    """Fetch YouTube streams using yt-dlp, cached in memory."""
    cache_key = f"streams_{video_id}"
    if cache_key in _stream_cache:
        cached = _stream_cache[cache_key]
        if time.time() - cached['ts'] < _cache_ttl:
            return cached['data'], cached.get('title', ''), cached.get('thumbnail', '')

    streams = []
    title = ''
    thumbnail = f'https://img.youtube.com/vi/{video_id}/hqdefault.jpg'
    try:
        import sys
        result = subprocess.run(
            [sys.executable, '-m', 'yt_dlp', '-j', '--no-warnings', '--no-check-certificates',
             f'https://www.youtube.com/watch?v={video_id}'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            title = info.get('title', '')
            formats = info.get('formats', [])
            # Filter to good video formats
            for f in formats:
                url = f.get('url', '')
                if not url:
                    continue
                vcodec = f.get('vcodec', 'none')
                acodec = f.get('acodec', 'none')
                height = f.get('height', 0)
                if vcodec != 'none' and height >= 360:
                    streams.append({
                        'url': url,
                        'height': height,
                        'quality': f.get('format_note', f'{height}p'),
                        'ext': f.get('ext', 'mp4'),
                        'has_audio': acodec != 'none',
                        'filesize': f.get('filesize', 0),
                    })
            # Deduplicate by height, keep best codec
            seen = {}
            for s in streams:
                h = s['height']
                if h not in seen or (s['has_audio'] and not seen[h]['has_audio']):
                    seen[h] = s
            streams = sorted(seen.values(), key=lambda x: x['height'], reverse=True)
    except FileNotFoundError:
        # yt-dlp CLI not found, try Python API
        try:
            import yt_dlp
            ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'format': 'bestvideo[height>=360]+bestaudio/best[height>=360]/best'}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
                if info:
                    title = info.get('title', '')
                    formats = info.get('formats', [])
                    for f in formats:
                        url = f.get('url', '')
                        if not url:
                            continue
                        vcodec = f.get('vcodec', 'none')
                        acodec = f.get('acodec', 'none')
                        height = f.get('height', 0)
                        if vcodec != 'none' and height and height >= 360:
                            streams.append({
                                'url': url, 'height': height,
                                'quality': f.get('format_note', f'{height}p'),
                                'ext': f.get('ext', 'mp4'),
                                'has_audio': acodec != 'none',
                                'filesize': f.get('filesize', 0),
                            })
                    seen = {}
                    for s in streams:
                        h = s['height']
                        if h not in seen or (s['has_audio'] and not seen[h]['has_audio']):
                            seen[h] = s
                    streams = sorted(seen.values(), key=lambda x: x['height'], reverse=True)
        except Exception:
            pass
    except Exception:
        pass

    _stream_cache[cache_key] = {'data': streams, 'title': title, 'thumbnail': thumbnail, 'ts': time.time()}
    return streams, title, thumbnail


@require_GET
def trailer_player(request, video_id):
    """Local trailer player page - serves a player that streams through local server."""
    if not re.match(r'^[\w-]{11}$', video_id):
        return HttpResponse('Invalid video ID', status=400)

    streams, title, thumbnail = _get_video_streams(video_id)

    # Pick best stream with audio
    best_url = ''
    best_quality = ''
    if streams:
        for s in streams:
            if s.get('has_audio'):
                best_url = s['url']
                best_quality = s.get('quality', '')
                break
        if not best_url and streams:
            best_url = streams[0].get('url', '')
            best_quality = streams[0].get('quality', '')

    quality_options = json.dumps([
        {'url': s['url'], 'quality': s.get('quality', ''), 'height': s['height'], 'has_audio': s.get('has_audio', False)}
        for s in streams[:8]
    ])

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title or "Trailer"} - Local Player</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: 100%; height: 100%; background: #000; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
.player-container {{ width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; position: relative; }}
video {{ width: 100%; height: 100%; object-fit: contain; }}
.controls {{ position: absolute; bottom: 0; left: 0; right: 0; padding: 10px 20px; background: linear-gradient(transparent, rgba(0,0,0,.8)); display: flex; align-items: center; gap: 12px; opacity: 0; transition: opacity .3s; }}
.player-container:hover .controls {{ opacity: 1; }}
.back-btn {{ position: absolute; top: 15px; left: 15px; padding: 8px 16px; border-radius: 8px; background: rgba(0,0,0,.6); border: 1px solid rgba(255,255,255,.15); color: #fff; font-size: .82rem; cursor: pointer; backdrop-filter: blur(8px); z-index: 10; text-decoration: none; transition: all .2s; }}
.back-btn:hover {{ background: rgba(255,255,255,.2); }}
.quality-selector {{ position: absolute; top: 15px; right: 15px; z-index: 10; }}
.quality-btn {{ padding: 6px 14px; border-radius: 6px; background: rgba(0,0,0,.6); border: 1px solid rgba(255,255,255,.15); color: #fff; font-size: .75rem; cursor: pointer; backdrop-filter: blur(8px); transition: all .2s; }}
.quality-btn:hover, .quality-btn.active {{ background: #e50914; border-color: #e50914; }}
.quality-dropdown {{ position: absolute; top: 100%; right: 0; margin-top: 4px; background: rgba(20,20,30,.95); border: 1px solid rgba(255,255,255,.1); border-radius: 8px; padding: 4px; display: none; backdrop-filter: blur(12px); min-width: 100px; }}
.quality-dropdown.show {{ display: block; }}
.quality-option {{ padding: 6px 12px; border-radius: 4px; color: #ccc; font-size: .75rem; cursor: pointer; transition: all .15s; }}
.quality-option:hover {{ background: rgba(255,255,255,.1); color: #fff; }}
.quality-option.active {{ color: #e50914; font-weight: 600; }}
.title-bar {{ position: absolute; bottom: 50px; left: 20px; color: #fff; font-size: .9rem; font-weight: 500; opacity: 0; transition: opacity .3s; pointer-events: none; text-shadow: 0 1px 4px rgba(0,0,0,.8); }}
.player-container:hover .title-bar {{ opacity: 1; }}
.loading {{ position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; background: #000; z-index: 20; }}
.loading.hidden {{ display: none; }}
.spinner {{ width: 40px; height: 40px; border: 3px solid rgba(255,255,255,.1); border-top-color: #e50914; border-radius: 50%; animation: spin .8s linear infinite; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
<div class="player-container" id="playerContainer">
    <div class="loading" id="loading"><div class="spinner"></div></div>
    <a class="back-btn" onclick="history.back()">← Back</a>
    <div class="quality-selector" id="qualitySelector">
        <button class="quality-btn" id="qualityBtn" onclick="toggleQualityMenu()">{best_quality or 'Auto'}</button>
        <div class="quality-dropdown" id="qualityDropdown"></div>
    </div>
    <div class="title-bar">{title}</div>
    <video id="videoPlayer" controls autoplay playsinline>
        <source src="/trailer/stream/{video_id}/" type="video/mp4">
    </video>
</div>
<script>
var streams = {quality_options};
var video = document.getElementById('videoPlayer');
var loading = document.getElementById('loading');

video.addEventListener('canplay', function() {{ loading.classList.add('hidden'); }});
video.addEventListener('waiting', function() {{ loading.classList.remove('hidden'); }});
video.addEventListener('playing', function() {{ loading.classList.add('hidden'); }});

// Build quality menu
var dropdown = document.getElementById('qualityDropdown');
streams.forEach(function(s, i) {{
    var div = document.createElement('div');
    div.className = 'quality-option' + (i === 0 ? ' active' : '');
    div.textContent = s.quality + (s.has_audio ? '' : ' (no audio)');
    div.onclick = function() {{ switchQuality(i); }};
    dropdown.appendChild(div);
}});

function toggleQualityMenu() {{
    dropdown.classList.toggle('show');
}}
function switchQuality(idx) {{
    if (!streams[idx]) return;
    var ct = video.currentTime;
    var playing = !video.paused;
    video.src = streams[idx].url;
    video.currentTime = ct;
    if (playing) video.play();
    document.getElementById('qualityBtn').textContent = streams[idx].quality;
    dropdown.querySelectorAll('.quality-option').forEach(function(d, i) {{
        d.classList.toggle('active', i === idx);
    }});
    dropdown.classList.remove('show');
}}
document.addEventListener('click', function(e) {{
    if (!e.target.closest('.quality-selector')) dropdown.classList.remove('show');
}});
</script>
</body>
</html>'''
    return HttpResponse(html, content_type='text/html')


@csrf_exempt
def trailer_stream(request, video_id):
    """Proxy the YouTube video stream through Django."""
    if not re.match(r'^[\w-]{11}$', video_id):
        return HttpResponse('Invalid video ID', status=400)

    streams, _, _ = _get_video_streams(video_id)
    if not streams:
        return HttpResponse('No streams available', status=404)

    # Pick best stream with audio
    stream_url = ''
    for s in streams:
        if s.get('has_audio') and s.get('url'):
            stream_url = s['url']
            break
    if not stream_url:
        stream_url = streams[0].get('url', '')

    if not stream_url:
        return HttpResponse('No stream URL', status=404)

    # Proxy the request
    try:
        req = urllib.request.Request(stream_url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        req.add_header('Referer', 'https://www.youtube.com/')
        req.add_header('Origin', 'https://www.youtube.com')

        response = urllib.request.urlopen(req, timeout=15)
        content_type = response.headers.get('Content-Type', 'video/mp4')
        content_length = response.headers.get('Content-Length')
        content_range = request.headers.get('Range')

        # If client requested a range
        if content_range:
            # Parse range header
            range_match = re.search(r'bytes=(\d+)-(\d*)', content_range)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2)) if range_match.group(2) else None

                # Forward range to YouTube
                yt_range = f'bytes={start}-'
                if end:
                    yt_range += str(end)
                req.add_header('Range', yt_range)
                response = urllib.request.urlopen(req, timeout=15)

                resp = StreamingHttpResponse(
                    response.stream(8192),
                    status=206,
                    content_type=content_type
                )
                resp['Accept-Ranges'] = 'bytes'
                resp['Content-Range'] = response.headers.get('Content-Range', '')
                resp['Content-Length'] = response.headers.get('Content-Length', '')
                resp['Access-Control-Allow-Origin'] = '*'
                return resp

        resp = StreamingHttpResponse(
            response.stream(8192),
            status=200,
            content_type=content_type
        )
        if content_length:
            resp['Content-Length'] = content_length
        resp['Accept-Ranges'] = 'bytes'
        resp['Access-Control-Allow-Origin'] = '*'
        return resp

    except Exception as e:
        return HttpResponse(f'Stream error: {e}', status=502)
