/*
 * Browser Proxy Service Worker — Dual Session Architecture
 *
 * Session 1 (Playing):  Normal HLS.js playback via /proxy/ URLs
 * Session 2 (Lookahead): Prefetch engine fetches segments 4 min ahead via this SW
 *
 * The SW intercepts /proxy/ requests, fetches directly from CDN,
 * and stores prefetched segments in Cache API for instant retrieval.
 */

const PROXY_PREFIX = '/proxy/';
const VIDEASY_ORIGIN = 'https://videasy.to';
const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';
const PREFETCH_CACHE = 'sw-prefetch-v2';
const MANIFEST_CACHE = 'sw-manifest-v2';
const MANIFEST_CACHE_TTL = 8000;
const MAX_CACHE_ENTRIES = 5000;

self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(PREFETCH_CACHE).then(function() { return; })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', function(event) {
  var url = event.request.url;
  var pathStart = url.indexOf(PROXY_PREFIX);
  if (pathStart === -1) return;

  // Parse the request to detect prefetch mode
  var prefetchHeader = event.request.headers.get('X-SW-Prefetch');
  var isPrefetch = prefetchHeader === '1';

  // For prefetch requests, check cache first
  if (isPrefetch) {
    event.respondWith(withPrefetchCache(event.request, url, pathStart));
    return;
  }

  // Normal playback requests: check prefetch cache first (instant!)
  event.respondWith(normalFetch(event.request, url, pathStart));
});

/*
 * Normal playback: check prefetch cache first, then fetch from CDN
 * This means prefetched segments play instantly from cache.
 */
function normalFetch(request, url, pathStart) {
  // Check prefetch cache first
  return caches.open(PREFETCH_CACHE).then(function(cache) {
    return cache.match(request).then(function(cached) {
      if (cached) return cached;

      // Not prefetched — fetch directly from CDN
      return fetchDirect(request, pathStart);
    });
  });
}

/*
 * Prefetch request: fetch from CDN, store in prefetch cache
 */
function withPrefetchCache(request, url, pathStart) {
  // Check if already cached
  return caches.open(PREFETCH_CACHE).then(function(cache) {
    return cache.match(request).then(function(cached) {
      if (cached) return cached;

      // Fetch from CDN and cache it
      return fetchDirect(request, pathStart).then(function(response) {
        if (response && response.ok) {
          // Clone before returning (Response body can only be read once)
          var toCache = response.clone();
          cache.put(request, toCache);
        }
        return response;
      });
    });
  });
}

/*
 * Core: Fetch directly from CDN with proper headers
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

  var headers = new Headers();
  headers.set('User-Agent', USER_AGENT);
  headers.set('Origin', VIDEASY_ORIGIN);
  headers.set('Referer', VIDEASY_ORIGIN + '/');
  headers.set('Accept', '*/*');
  headers.set('Accept-Encoding', 'identity');
  headers.set('Connection', 'keep-alive');

  var rangeHeader = request.headers.get('Range');
  if (rangeHeader) {
    headers.set('Range', rangeHeader);
  }

  var fetchOptions = {
    method: request.method,
    headers: headers,
    mode: 'cors',
    credentials: 'omit',
    cache: 'default',
  };

  return fetch(cdnUrl, fetchOptions).then(function(response) {
    if (!response.ok) {
      // Try without Origin/Referer
      var fb = new Headers();
      fb.set('User-Agent', USER_AGENT);
      if (rangeHeader) fb.set('Range', rangeHeader);

      return fetch(cdnUrl, {
        method: request.method,
        headers: fb,
        mode: 'cors',
        credentials: 'omit',
      }).then(function(r2) {
        if (!r2.ok) return fetch(request); // fallback to server proxy
        return addCorsHeaders(r2);
      }).catch(function() {
        return fetch(request); // fallback to server proxy
      });
    }
    return addCorsHeaders(response);
  }).catch(function() {
    return fetch(request); // fallback to server proxy
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

/*
 * Message handler: frontend can ask SW to prefetch a list of URLs
 */
self.addEventListener('message', function(event) {
  var data = event.data;
  if (!data || data.type !== 'prefetch-segments') return;      var urls = data.urls || [];
      if (urls.length === 0) return;

      caches.open(PREFETCH_CACHE).then(function(cache) {
        // Evict oldest entries if cache is getting full
        cache.keys().then(function(keys) {
          if (keys.length > MAX_CACHE_ENTRIES - 100) {
            for (var i = 0; i < 100 && i < keys.length; i++) {
              cache.delete(keys[i]);
            }
          }
        });
        var promises = urls.map(function(url) {
          return cache.match(url).then(function(hit) {
            if (hit) return; // already cached
            // Fetch and cache
            var pathStart = url.indexOf(PROXY_PREFIX);
            if (pathStart === -1) return;
            return fetchDirect({ url: url, headers: new Headers(), method: 'GET' }, pathStart)
              .then(function(resp) {
                if (resp && resp.ok) {
                  cache.put(url, resp.clone());
                }
              })
              .catch(function() {});
          });
        });
        Promise.all(promises);
      });
});
