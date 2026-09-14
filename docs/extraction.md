# Stream Extraction Architecture (Videasy / Vidking pipeline)

This document covers how the project turns a TMDB ID into playable stream links,
how the links are decrypted, and how playback reaches the browser.

There are **two parallel implementations of the same extraction logic**:

| Implementation | Where | Runs in | Used by |
|---|---|---|---|
| Server-side (Python) | `core/views.py` + `core/streaming_views.py` | Django server | Detail-page link lists, CinePlayer fallback |
| Client-side (JS) | `static/js/client-extract.js` + `static/js/moviein-extract.js` | Browser | The extractor player (`/api/player/`, detail pages) |

The upstream for both is the **speedracelight API** (`api.speedracelight.com`,
internally called "Vidking") — the backend that videasy-style players talk to —
plus a TMDB-mirror database at `db.speedracelight.com/3`.

---

## 1. Pipeline overview

```
TMDB ID
  │
  ├─ 1. TMDB metadata ──► title, year, imdbId          (db.speedracelight.com/3/…)
  │
  ├─ 2. Seed ──────────► per-title key string          (api.speedracelight.com/seed)
  │
  ├─ 3. Server fetch ──► encrypted blobs × 9 "servers" (…/{endpoint}/sources-with-title)
  │
  ├─ 4. Decrypt ───────► JSON { sources: [...], subtitles: [...] }
  │
  ├─ 5. Aggregate ─────► dedupe, tag language + quality, order by health
  │
  └─ 6. Playback ──────► hls.js ◄── manifests/segments via browser SW proxy (/proxy/)
```

### Step 1 — TMDB metadata

`GET https://db.speedracelight.com/3/{movie|tv}/{tmdbId}?append_to_response=external_ids`

Returns a TMDB-shaped payload. Extracted: `title` (or `name`), `year` (from
`release_date`/`first_air_date`), and `imdbId` (from `external_ids`).
Implementation: `_vk_fetch_tmdb_info()` in `core/views.py`.

For TV, `player_episodes_view` uses the same DB to list seasons and episodes.

### Step 2 — Seed

`GET https://api.speedracelight.com/seed?mediaId={tmdbId}` → `{"seed": "…"}`

The seed is the key material for step 4 and is **per-title**. The server caches
it for 5 minutes (`stream_seed_{tmdb_id}` in Django's cache) because fetching
costs ~0.7 s. If the API answers `401` to a source fetch, the seed was rejected
(stale/expired) and the fetch must be retried with a fresh one.
Implementation: `_vk_get_seed()`.

### Step 3 — Fetch the "servers"

The nine "servers" are just different endpoint prefixes on the same API:

| Server name | Endpoint prefix |
|---|---|
| Yoru | `cdn` |
| Cypher | `downloader2` |
| Breach | `m4uhd` |
| Neon | `vsrc` |
| Vyse | `hdmovie` |
| Killjoy | `meine` |
| Fade | `hdmovie` (same as Vyse) |
| Omen | `lamovie` |
| Raze | `superflix` |

Request (constants in `_VIDKING_SERVERS` / `_VIDKING_API_BASE`, `core/views.py:5784`):

```
GET https://api.speedracelight.com/{endpoint}/sources-with-title
    ?title={title}&mediaType={movie|tv}&year={year}
    &seasonId={n}&episodeId={n}&tmdbId={id}&imdbId={tt…}
    &enc=2&seed={seed}&_t={ms-timestamp}
```

The response body is a **base64url-encoded encrypted blob** (not JSON).
Status `401` means "seed rejected".

> Note: links minted by these CDNs are frequently **bound to the requesting
> client** — e.g. Vyse URLs embed an expiry timestamp and the client IP
> (`…:1789311219:185.101.253.137:…`). They go stale within hours and can 403
> when fetched from a different IP than the one that extracted them. This is
> why extraction is kept in the browser wherever possible.

### Step 4 — Decrypt (the "Vidking cipher")

Implementations: `_vk_decrypt()` + helpers (`core/views.py`, lines ~5800-5950)
and `decrypt()` in `static/js/client-extract.js`.

1. **Base64url-decode** the payload (`-`→`+`, `_`→`/`, re-pad).
2. **Build a keystream** from a custom PRNG seeded by `(seed, tmdbId)`
   (`_vk_init_state` / `_vk_step`):
   - State = a 61-word table (`_JS_SIZE`) plus an accumulator. The counter
     starts at `_vk_ci(_vk_vf(seed) ^ _vk_ci(tmdbId ^ 0x9E3779B9))` and runs 8
     rounds (`_JS_ROUNDS`) of: rotate-left (`_vk_ps`) mixing with
     `0x9E3779B9`, table writes `(i ^ _vk_ci(i))`, and a murmur-style
     finalizer (`_vk_ci`). The accumulator ends as `_vk_ci(i ^ 0xA5A5A5A5)`
     (`_MAGIC_XOR`).
   - Vestigial branches: the code contains parity predicates (`_vk_bf`/`_vk_if`,
     `n*(n+1) & 1`) that gate an RC4 key-scheduling path and SHA-256-constant
     injection — but since `n*(n+1)` is always even, `bf` is always true and
     `if` always false in this port, so the table path always runs and every
     round always mixes. They are kept for fidelity with the obfuscated
     original.
   - Each `_vk_step(counter)` produces one 32-bit word → 4 keystream bytes,
     little-endian.
3. **XOR** the payload bytes with the keystream.
4. **Verify the magic bytes** `"mvm1"` (`6D 76 6D 31`). A mismatch means wrong
   seed or a tampered payload → fail.
5. The remaining bytes are **UTF-8 JSON**:

```json
{
  "sources": [
    { "url": "https://cdn…/index.m3u8", "quality": "1080p",
      "language": "Hindi", "audioLanguage": "…", "server": "…" }
  ],
  "subtitles": [ { "url": "…", "lang": "en", "lang_name": "English" } ]
}
```

### Step 5 — Aggregate

- Deduplicate by URL (many servers return the same master manifest).
- Tag each source: `_lang` (from extractor metadata first — see §4),
  `_quality`, `_server`.
- Probe each HLS manifest **direct and via the proxy** in parallel
  (`_buildSmartQueue`) and order attempts so healthy sources play first.
- Remember the CDN host that delivered (`_favHost`) to speed up the next play.

### Step 6 — Playback

- **hls.js** attaches to the manifest (MSE). Config in `_tryHlsDirect`
  (`static/js/player-core.js`): huge buffers (`maxBufferLength: 1800`),
  `startFragPrefetch`, aggressive retry tuning.
- CDNs reject browser `Origin` headers (403/CORS), so HLS traffic goes through
  the **service-worker proxy** (`static/sw-proxy.js`, served at `/sw-proxy.js`,
  scope `/proxy/`). The SW intercepts `/proxy/<host>/<path>` requests and
  fetches them from the page context — the bytes still go browser↔CDN, not
  through Django.
- Fallback chain per source: direct → SW proxy → next source. If a stream
  fails, the queue advances (see §5 for language-aware ordering).

---

## 2. Client-side extractors (the browser path)

The extractor player runs **two extractors in parallel** and plays whatever
arrives first (`onSource` → `_playHls`); the rest join the fallback queue.

### `client-extract.js` — "Speedracelight" (Vidking cipher in JS)

- Same pipeline as the server (steps 1-4), ported to JS with `Math.imul` for
  the 32-bit ops.
- Calls `https://api.speedracelight.com` **directly** — that API sends
  `Access-Control-Allow-Origin: *`.
- Falls back to `/proxy/api.speedracelight.com/...` **only when the service
  worker is active** (`swProxyActive()`), so requests always originate from the
  browser and never route through Django.

### `moviein-extract.js` — "MovieIn" (AES-CBC + MD5)

A second provider on the same API with a mobile-app-style session:

- **Session headers**: `app_id: moviein`, `package_name: com.fvvcl.flickverse`,
  `device_id` (persisted in `localStorage`), `cur_time` (ms), `token`,
  and `sign` = `MD5(SALT + device_id + cur_time).toUpperCase()` with
  `SALT = '47Q8tBqO4YqrMHf4'`.
- **Session init** (`/api/…/init` endpoints) mints the token, then
  search/source endpoints are called as form posts.
- **Decrypt**: responses are base64 → **AES-CBC** (Web Crypto,
  `AES_KEY = '0123456789123456'`, `AES_IV = '2015030120123456'`) → strip PKCS7
  → **gzip inflate** (`pako`) → JSON.
- Yields sources with quality + language (often multi-language audio, e.g.
  "Español / English").

---

## 3. Endpoints

### Site endpoints (Django, `core/urls.py`)

| Endpoint | View | Purpose |
|---|---|---|
| `GET /ajax/fetch-sources/` | `fetch_embed_sources` | Original one-shot server extraction; returns decrypted `sources[]` |
| `GET /ajax/videasy-sources/` | `videasy_sources_view` | Extraction grouped per server; stops after 3 productive servers (detail-page link lists) |
| `GET /ajax/player-sources/` | `player_sources_view` | Server extraction, **all 9 servers in parallel**, flat deduped `results[]` for the player |
| `GET /ajax/player-sources-stream/` | `player_sources_stream_view` | Same but **SSE**: emits `type: source` events as each server responds (playback starts ASAP), then `type: done` |
| `GET /ajax/player-episodes/` | `player_episodes_view` | Seasons + episodes for TV (from the speedracelight DB) |
| `GET /api/links/` | `api_links_view` | **Direct-links API for native apps** — returns extracted links with raw + `/proxy/` routes per source (see below) |
| `GET /api/player/` | `videasy_player_view` | The standalone extractor player page |
| `GET /api/player/frame/` | `videasy_player_frame_view` | Embeddable frame variant |
| `GET /proxy/<host>/<path>` | `proxy_view` | Server-side fallback proxy (see below) |
| `GET /api/cookies` | `cookies_view` | DDoS-Guard cookies for videasy.to |
| `GET /api/health` | `health_view` | Health probe |
| `GET /sw-proxy.js` | `serve_sw_proxy_js` | Service-worker script (browser proxy) |

Shared params for the extraction endpoints: `tmdb_id` (required), `type` /
`media_type` (`movie`|`tv`), `season`, `episode`.

### Direct-links API for native apps (`/api/links/`)

For apps that want to play the stream themselves (ExoPlayer, VLC, MX Player,
WebView players) instead of loading the webplayer embed:

```
GET /api/links/?tmdb_id=550&type=movie[&season=1&episode=1]
              [&lang=hindi][&server=yoru]
```

```json
{
  "success": true, "title": "Fight Club", "year": "1999",
  "tmdbId": 550, "type": "movie", "count": 7,
  "links": [
    { "server": "Yoru", "quality": "1080p", "language": "Original",
      "type": "hls",
      "url": "https://i-arch-400.gufin435siv.com/…/index.m3u8",
      "proxy_url": "http://host:8000/proxy/i-arch-400.gufin435siv.com/…/index.m3u8",
      "headers": { "Referer": "https://player.videasy.to/",
                   "Origin": "https://player.videasy.to" } }
  ]
}
```

Field semantics:
- `url` — the raw CDN link. Try this first with the supplied `headers`.
- `proxy_url` — the same stream through our server proxy, with the manifest
  rewritten so segments also stream through it. Use when the raw link 403s or
  the CDN blocks unknown clients. Requires the proxy host to be reachable.
- `type` — `hls` (feed to ExoPlayer's `HlsMediaSource` / hls.js) or `mp4`.
- `language` / `quality` — normalized; language-stuffed qualities ("Hindi")
  become `quality: "Auto"` + `language: "Hindi"` so apps can filter cleanly.
- Filters: `lang` substring-matches language or quality; `server` filters by
  server name.
- CORS: `Access-Control-Allow-Origin: *` — WebView clients can call it directly.

### Proxying (two implementations)

**Server proxy** `proxy_view` (`core/views.py:6880`):
`/proxy/https://cdn.example/path` → fetched with videasy `User-Agent`/`Origin`/
`Referer` headers (retry without Origin on 403), **streams** the response, and
rewrites `.m3u8` manifests so every segment/variant URL (and `URI="…"` tags)
points back through `/proxy/`. Used as a fallback when the browser SW proxy is
unavailable (e.g. insecure HTTP origins can't register service workers).

**Browser SW proxy** `static/sw-proxy.js`: a fetch handler scoped to `/proxy/`
that does the same manifest rewriting client-side. This is the default for the
extractor player; video bytes never touch the Django server.

### Upstream endpoints (external)

| URL | Purpose |
|---|---|
| `https://db.speedracelight.com/3/{type}/{id}` | TMDB mirror: title/year/imdbId, seasons/episodes |
| `https://api.speedracelight.com/seed?mediaId={id}` | Per-title decryption seed |
| `https://api.speedracelight.com/{endpoint}/sources-with-title` | Encrypted source blobs per server |
| MovieIn API paths on the same base (`…/init`, `…/search/result`, …) | Session + sources for the MovieIn extractor |

---

## 4. Source metadata: language & quality tagging

- `s._lang` starts from extractor metadata (`language`, `audioLanguage`,
  `audio` fields → `_detectLangFromSource`); `"Original"` if none.
- `_detectLangFromManifest` fetches the master playlist and parses
  `#EXT-X-MEDIA:…LANGUAGE="…"` to *fill in* languages **only when the source
  had no explicit language metadata**. (It must not overwrite server labels —
  that used to silently revert the user's Audio selection while the Tracks
  sheet was open.)
- `quality` comes from the server, but some servers stuff the *language* into
  the quality field (`"Hindi"`); the Tracks UI canonicalizes those to `"Auto"`
  via `_pcCanonRes` so they never appear as fake resolution rows.
- The user's picks are sticky: `_pcPrefLang` / `_pcPrefRes` survive stream
  failures, fallback ordering keeps same-language sources before other
  languages (`_orderedSources`), and any forced change shows a toast
  (`_showPickToast`) instead of silently switching.

---

## 5. Failure handling & known constraints

- **Probe-first playback**: every HLS route (direct + proxy) is probed with a
  real fetch before hls.js attaches; dead routes are skipped. Probes are cached
  20 s (`_probeTtlMs`) and invalidated when the user explicitly picks a source.
- **Explicit picks always go first** (`keepFirst`) — a false-negative probe can
  veto an automatic ordering but never a user choice.
- **Host death**: a CDN host that fails 3 routes is skipped for the rest of the
  session (`_hostFails`).
- **Seed expiry**: `401` from a source fetch means the seed was rejected; the
  seed cache TTL is 5 min but upstream can invalidate sooner. Retry with a
  fresh seed.
- **Link freshness**: CDN links embed expiry + client IP; long sessions should
  re-extract rather than cache URLs.
- **HTTPS requirement**: the browser SW proxy needs a secure context
  (`localhost`/`127.0.0.1` count). On plain HTTP LAN origins the player falls
  back to the Django server proxy.

---

## 6. Where the code lives

| File | Role |
|---|---|
| `core/views.py` (lines ~5784-6060) | Vidking cipher, seed, per-server fetch (Python) |
| `core/views.py` (lines ~7123-7360) | `videasy_sources_view`, `player_sources_view`, episodes |
| `core/views.py` (line ~6880) | Server-side `proxy_view` + manifest rewriting |
| `core/streaming_views.py` | SSE extraction endpoint + seed prefetch |
| `static/js/client-extract.js` | Vidking cipher + extraction (browser) |
| `static/js/moviein-extract.js` | MovieIn AES/MD5 extractor (browser) |
| `static/js/player-core.js` | Source aggregation, probing, hls.js playback, fallback queue |
| `static/js/tracks-ui.js` | Tracks sheet: audio/resolution/dual-stream/subtitles |
| `static/sw-proxy.js` | Browser service-worker proxy |
