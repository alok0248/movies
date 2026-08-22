# Videasy Player — Django Integration

## Files

```
player/
├── player.html          # Main player UI (URL input, selectors, controls)
├── player_frame.html    # Iframe player (HLS.js, dual-stream sync)
├── proxy.py             # Django views (proxy, cookies, health)
└── README.md            # This file
```

## Features

- **URL extraction**: Paste any `player.videasy.net` or `player.videasy.to` URL → extracts sources from 8 servers
- **Language selector**: Original, Hindi, English, Tamil, Telugu, etc.
- **Quality selector**: 2160p, 1080p, 720p, 480p, 360p
- **Dual Stream mode**: Play VIDEO from one server + AUDIO from another, synced in real-time
- **Sync controls**: Adjust audio offset ±0.1s, ±1s, ±10s
- **Volume sliders**: Independent video/audio volume in dual mode
- **CSV export**: All sources with URLs, headers, cookies
- **Fullscreen**: Compact player with fullscreen button

## Django Setup

### 1. Copy files

Copy `player.html` and `player_frame.html` into your Django static directory:

```bash
cp player/player.html yourapp/static/player/
cp player/player_frame.html yourapp/static/player/
```

### 2. Copy proxy views

Copy `proxy.py` into your Django app:

```bash
cp player/proxy.py yourapp/
```

### 3. Add URL patterns

In your project's `urls.py`:

```python
from django.urls import path
from yourapp.proxy import proxy_view, cookies_view, health_view

urlpatterns = [
    # ... your existing patterns ...

    # Videasy Player endpoints
    path('player/', lambda r: render(r, 'player/player.html'), name='player'),
    path('proxy/<path:target>', proxy_view, name='videasy-proxy'),
    path('api/cookies', cookies_view, name='videasy-cookies'),
    path('api/health', health_view, name='videasy-health'),
]
```

**Important**: The proxy path MUST be `/proxy/<path:target>` — the player_frame.html constructs proxy URLs using `location.origin + "/proxy/" + host + path`.

### 4. Create the template

If using Django templates instead of static files, create `yourapp/templates/player/player.html` and update the iframe src:

```html
<!-- In player.html, change the iframe src to your static URL -->
<iframe id="playerFrame" src="{% static 'player/player_frame.html' %}" ...></iframe>
```

Or serve both files from the same static directory (default: iframe src is `player_frame.html` relative to player.html).

### 5. Verify

```
GET /api/health          → {"status": "ok"}
GET /api/cookies         → {"cookies": {"__ddg1_": "...", ...}}
GET /proxy/moon.peakstorm.top/vd/.../index.m3u8  → HLS manifest
GET /player/             → Player UI
```

## How the Proxy Works

The CDN blocks requests from non-videasy.to origins. The proxy:

1. Receives requests from the player iframe (localhost/your-domain)
2. Forwards them to the CDN with `Origin: https://player.videasy.to`
3. Returns the data with `Access-Control-Allow-Origin: *`

If the CDN returns 403 (some servers reject the fake Origin), the proxy retries without it.

## Architecture

```
player.html (parent page)
  ├── Extracts sources via cipher from videasy API
  ├── Language / Quality / Dual-stream selectors
  └── postMessage → player_frame.html (iframe)
                      ├── Single mode: plays one HLS stream
                      ├── Dual mode: two HLS.js instances
                      │   ├── video element (muted) → video source
                      │   └── audioVid element → audio source
                      ├── Sync loop (200ms) corrects drift
                      └── /proxy/ → Django proxy → CDN
```

## Dual Stream Mode

1. Click **⚙ Dual Stream** button
2. **VID Source** row: pick which server/quality for VIDEO
3. **AUD Source** row: pick which server/language for AUDIO
4. **Sync bar**: adjust offset if audio is ahead/behind
5. **Volume sliders**: control video and audio volume independently

The video element plays muted (aggressively enforced). The audio element produces all sound. A sync loop corrects drift every 200ms.

## Notes

- HLS.js is loaded from CDN (`cdn.jsdelivr.net/npm/hls.js@1`)
- The cipher for decrypting sources must match the current Videasy player JS
- Seeds expire after 30 seconds — extraction must happen quickly
- Some servers (Meine, Breach) may return 403 or 500 — this is normal
