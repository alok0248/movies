/*
 * Browser Proxy Service Worker — Stealth Mode
 *
 * Intercepts /proxy/ requests and fetches from CDN directly in the browser.
 * Designed to look exactly like a normal player iframe making requests.
 * No custom headers, no batch patterns, no fingerprints.
 */

const PROXY_PREFIX = '/proxy/';
const VIDEASY_ORIGIN = 'https://videasy.to';
const VIDEASY_PLAYER_ORIGIN = 'https://player.videasy.to';
/* Real Chrome UA — matches what HLS.js sends natively */
const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';
const PREFETCH_CACHE = 'sw-prefetch-v3';
const MAX_CACHE_ENTRIES = 500;

/* ===== DDoS-Guard Cookie Management ===== */
var _cachedCookies = null;
var _cookieFetchPromise = null;

function fetchCookiesFromOrigin() {
  if (_cookieFetchPromise) return _cookieFetchPromise;
  _cookieFetchPromise = fetch(VIDEASY_PLAYER_ORIGIN + '/', {
    method: 'GET',
    mode: 'cors',
    credentials: 'omit',
    headers: { 'User-Agent': USER_AGENT }
  }).then(function(response) {
    var cookies = {};
    return fetch(VIDEASY_PLAYER_ORIGIN + '/', {
      method: 'GET',
      mode: 'cors',
      credentials: 'include',
      headers: { 'User-Agent': USER_AGENT }
    }).then(function(r2) {
      _cachedCookies = cookies;
      return cookies;
    });
  }).catch(function() {
    _cachedCookies = {};
    return {};
  });
  return _cookieFetchPromise;
}

function getCookieHeader() {
  if (_cachedCookies && Object.keys(_cachedCookies).length > 0) {
    return Object.entries(_cachedCookies).map(function(e) {
      return e[0] + '=' + e[1];
    }).join('; ');
  }
  return '';
}

/* ===== SW Lifecycle ===== */
self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(PREFETCH_CACHE).then(function() { return; })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(names) {
      return Promise.all(
        names.filter(function(n) { return n !== PREFETCH_CACHE; })
             .map(function(n) { return caches.delete(n); })
      );
    }).then(function() { return clients.claim(); })
  );
});

/* ===== Fetch Interception ===== */
self.addEventListener('fetch', function(event) {
  var url = event.request.url;
  var pathStart = url.indexOf(PROXY_PREFIX);
  if (pathStart === -1) return;

  /* Only intercept GET requests — let POST/PUT/etc pass through */
  if (event.request.method !== 'GET') return;

  event.respondWith(stealthFetch(event.request, pathStart));
});

/*
 * Stealth fetch: check cache first, then fetch from CDN
 * Looks like normal browser traffic — no custom headers
 */
function stealthFetch(request, pathStart) {
  var cdnUrl = cdnUrlOf(request.url, pathStart);
  return caches.open(PREFETCH_CACHE).then(function(cache) {
    return cache.match(request).then(function(cached) {
      if (cached) return maybeRewritePlaylist(cdnUrl, cached);
      return fetchDirect(request, pathStart).then(function(response) {
        if (response && response.ok) {
          /* Cache the RAW successful response (segments, manifests) */
          var toCache = response.clone();
          cache.put(request, toCache);
          trimCache(cache);
        }
        return maybeRewritePlaylist(cdnUrl, response);
      });
    });
  });
}

/* Extract the real CDN URL from a /proxy/ request. */
function cdnUrlOf(requestUrl, pathStart) {
  var proxyPath = requestUrl.substring(pathStart + PROXY_PREFIX.length);
  return proxyPath.startsWith('http/') ? 'http://' + proxyPath.substring(5) : 'https://' + proxyPath;
}

/*
 * Rewrite HLS playlists served through the proxy so EVERY child URI becomes an
 * absolute /proxy/<host>/... URL. Without this, root-relative child paths
 * (e.g. "/r6/s/seg.ts") are resolved by hls.js against the PAGE origin and
 * leak to the site server as 404s instead of staying in the browser proxy.
 */
function maybeRewritePlaylist(cdnUrl, response) {
  if (!response || !response.ok) return response;
  var ct = (response.headers.get('content-type') || '').toLowerCase();
  var isPlaylist = ct.indexOf('mpegurl') > -1 || ct.indexOf('hls') > -1 || /\\.m3u8(\\?|$)/i.test(cdnUrl || '');
  if (!isPlaylist) {
    /* Some CDNs serve tokenized playlists with no .m3u8 in the URL and a generic
       content-type. Peek only the FIRST chunk of a clone (never buffer a big
       media body in memory) to check for the #EXTM3U magic. */
    if (!response.body) return response;
    var clone = response.clone();
    var reader = clone.body.getReader();
    var decoder = new TextDecoder('utf-8');
    return reader.read().then(function(r) {
      try { reader.cancel(); } catch (e) {}
      var head = r && r.value ? decoder.decode(r.value, { stream: true }) : '';
      head = String(head).replace(/^\uFEFF/, '').replace(/^\s+/, '');
      if (head.indexOf('#EXTM3U') !== 0) return response;
      return response.text().then(function(text) {
        return rewritePlaylistResponse(cdnUrl, response, text);
      });
    }).catch(function() { return response; });
  }
  return response.text().then(function(text) {
    return rewritePlaylistResponse(cdnUrl, response, text);
  }).catch(function() { return response; });
}

function rewritePlaylistResponse(cdnUrl, response, text) {
  var rewritten = rewriteManifest(text, cdnUrl);
  var h = new Headers(response.headers);
  h.delete('content-length');
  h.delete('content-encoding');
  h.set('Access-Control-Allow-Origin', '*');
  if (!h.get('content-type')) h.set('content-type', 'application/vnd.apple.mpegurl');
  return new Response(rewritten, {
    status: response.status,
    statusText: response.statusText,
    headers: h
  });
}

function resolveChildUri(uri, cdnUrl) {
  if (!uri) return '';
  if (/^https?:\/\//i.test(uri)) return uri;
  if (uri.indexOf('//') === 0) return 'https:' + uri;
  if (uri.charAt(0) === '/') {
    var origin = cdnUrl.split('/').slice(0, 3).join('/');   /* scheme + host */
    return origin + uri;
  }
  var base = cdnUrl.substring(0, cdnUrl.lastIndexOf('/') + 1);
  return base + uri;
}

function rewriteManifest(text, cdnUrl) {
  var lines = String(text || '').split('\n');
  var out = [];
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (line === '') { out.push(''); continue; }
    if (line.charAt(0) === '#') {
      /* Tags carry URIs: EXT-X-KEY, EXT-X-MEDIA, EXT-X-MAP, EXT-X-I-FRAME-STREAM-INF... */
      out.push(line.replace(/URI="([^"]*)"/gi, function(m, u) {
        var full = resolveChildUri(u, cdnUrl);
        return 'URI="' + makeProxyUri(full) + '"';
      }));
    } else {
      out.push(makeProxyUri(resolveChildUri(line, cdnUrl)));
    }
  }
  return out.join('\n');
}

function makeProxyUri(fullUrl) {
  if (!fullUrl || !/^https?:\/\//i.test(fullUrl)) return fullUrl;
  try {
    var u = new URL(fullUrl);
    if (u.origin === self.location.origin && u.pathname.indexOf('/proxy/') === 0) return fullUrl;  /* already proxied */
    return self.location.origin + '/proxy/' + u.hostname + u.pathname + u.search;
  } catch (e) { return fullUrl; }
}

function trimCache(cache) {
  cache.keys().then(function(keys) {
    if (keys.length > MAX_CACHE_ENTRIES) {
      /* Remove oldest 20% when over limit */
      var removeCount = Math.ceil(MAX_CACHE_ENTRIES * 0.2);
      for (var i = 0; i < removeCount && i < keys.length; i++) {
        cache.delete(keys[i]);
      }
    }
  });
}

/*
 * Core: Fetch directly from CDN
 * Headers match exactly what a real browser/HLS.js iframe sends
 */
function fetchDirect(request, pathStart) {
  var url = request.url;
  var proxyPath = url.substring(pathStart + PROXY_PREFIX.length);

  var cdnUrl;
  if (proxyPath.startsWith('http/')) {
    cdnUrl = 'http://' + proxyPath.substring(5);
  } else {
    cdnUrl = 'https://' + proxyPath;
  }

  /* Build headers that look exactly like a real browser request */
  var headers = new Headers();

  /* Forward original request headers the browser would send */
  request.headers.forEach(function(value, key) {
    var lk = key.toLowerCase();
    /* Skip headers the browser controls or that would fingerprint us */
    if (lk === 'host' || lk === 'origin' || lk === 'referer' ||
        lk === 'cookie' || lk === 'user-agent' || lk === 'connection' ||
        lk === 'sec-fetch-dest' || lk === 'sec-fetch-mode' ||
        lk === 'sec-fetch-site' || lk === 'sec-ch-ua' ||
        lk === 'sec-ch-ua-mobile' || lk === 'sec-ch-ua-platform') return;
    headers.set(key, value);
  });

  /* Real browser headers — nothing custom */
  headers.set('User-Agent', USER_AGENT);
  headers.set('Accept', '*/*');
  /* CRITICAL: Real browsers always send gzip,br — NOT identity */
  headers.set('Accept-Encoding', 'gzip, deflate, br');
  headers.set('Accept-Language', 'en-US,en;q=0.9');
  headers.set('Connection', 'keep-alive');
  headers.set('Sec-Fetch-Dest', 'empty');
  headers.set('Sec-Fetch-Mode', 'cors');
  headers.set('Sec-Fetch-Site', 'cross-site');
  headers.set('Sec-Ch-Ua', '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"');
  headers.set('Sec-Ch-Ua-Mobile', '?0');
  headers.set('Sec-Ch-Ua-Platform', '"Windows"');

  /* Set Origin/Referer to look like we're the videasy player iframe */
  headers.set('Origin', VIDEASY_ORIGIN);
  headers.set('Referer', VIDEASY_ORIGIN + '/');

  /* Add DDoS-Guard cookies if we have them */
  var cookieStr = getCookieHeader();
  if (cookieStr) {
    headers.set('Cookie', cookieStr);
  }

  /* Forward Range header if present (HLS.js sends these for segments) */
  var origRange = request.headers.get('Range');
  if (origRange) headers.set('Range', origRange);

  var fetchOptions = {
    method: 'GET',
    headers: headers,
    mode: 'cors',
    credentials: 'omit',
    cache: 'default',
  };

  return fetch(cdnUrl, fetchOptions).then(function(response) {
    if (!response.ok) {
      /* Try without Origin/Referer (some CDN servers reject fake Origin) */
      var fb = new Headers();
      fb.set('User-Agent', USER_AGENT);
      fb.set('Accept-Encoding', 'gzip, deflate, br');
      fb.set('Accept-Language', 'en-US,en;q=0.9');
      if (cookieStr) fb.set('Cookie', cookieStr);
      if (origRange) fb.set('Range', origRange);

      return fetch(cdnUrl, {
        method: 'GET',
        headers: fb,
        mode: 'cors',
        credentials: 'omit',
      }).then(function(r2) {
        if (!r2.ok) {
          return new Response('Proxy error: CDN returned ' + r2.status + ' and ' + response.status, {
            status: r2.status || response.status || 502,
            headers: { 'Content-Type': 'text/plain' }
          });
        }
        return addCorsHeaders(r2);
      }).catch(function(err) {
        return new Response('Proxy error: ' + err.message, {
          status: 502,
          headers: { 'Content-Type': 'text/plain' }
        });
      });
    }
    return addCorsHeaders(response);
  }).catch(function(err) {
    return new Response('Proxy error: ' + err.message, {
      status: 502,
      headers: { 'Content-Type': 'text/plain' }
    });
  });
}

function addCorsHeaders(response) {
  var h = new Headers(response.headers);
  h.set('Access-Control-Allow-Origin', '*');
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: h
  });
}

/* ===== Message Handler ===== */
self.addEventListener('message', function(event) {
  var data = event.data;
  if (!data) return;

  /* Frontend requests cookie info */
  if (data.type === 'get-cookies') {
    fetchCookiesFromOrigin().then(function(cookies) {
      self.clients.matchAll().then(function(clients) {
        clients.forEach(function(client) {
          client.postMessage({
            type: 'cookies',
            cookies: cookies
          });
        });
      });
    });
    return;
  }

  /* Removed: prefetch-segments handler — HLS.js handles its own buffering */
});
