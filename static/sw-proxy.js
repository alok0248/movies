/*
 * Browser Proxy Service Worker
 * Intercepts /proxy/host/path requests and fetches directly from CDN.
 * This bypasses the Django server for video segment downloads,
 * resulting in much faster buffering.
 */

const PROXY_PREFIX = '/proxy/';
const VIDEASY_ORIGIN = 'https://videasy.to';
const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';

// Cache for m3u8 manifests (short TTL to stay fresh)
const manifestCache = new Map();
const MANIFEST_CACHE_TTL = 3000; // 3 seconds

self.addEventListener('install', function(event) {
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', function(event) {
  var url = event.request.url;
  var pathStart = url.indexOf(PROXY_PREFIX);

  // Only intercept /proxy/ requests
  if (pathStart === -1) return;

  // Skip m3u8 manifests — let the server handle manifest rewriting
  if (url.indexOf('.m3u8') !== -1 && url.indexOf('.m3u8?') === -1 &&
      url.substring(url.lastIndexOf('/') + 1).indexOf('.m3u8') !== -1) {
    // Let manifest requests pass through to server for rewriting
    // (they are tiny and need proper URL rewriting)
    // BUT check: if the manifest URL already contains the full proxy path
    // from the server-rewritten manifest, let it pass through
    return;
  }

  event.respondWith(
    fetchDirect(event.request, pathStart)
  );
});

function fetchDirect(request, pathStart) {
  var url = request.url;
  // Extract the CDN host + path from /proxy/cdn.host/rest/of/path
  var proxyPath = url.substring(pathStart + PROXY_PREFIX.length);

  // Reconstruct the original CDN URL
  var cdnUrl;
  if (proxyPath.startsWith('http/')) {
    cdnUrl = 'http://' + proxyPath.substring(5);
  } else {
    cdnUrl = 'https://' + proxyPath;
  }

  // Check manifest cache
  var cacheKey = cdnUrl;
  var cached = manifestCache.get(cacheKey);
  if (cached && (Date.now() - cached.time) < MANIFEST_CACHE_TTL) {
    return Promise.resolve(new Response(cached.body, {
      status: 200,
      headers: cached.headers
    }));
  }

  // Build a new request with proper CDN headers
  var headers = new Headers();
  headers.set('User-Agent', USER_AGENT);
  headers.set('Origin', VIDEASY_ORIGIN);
  headers.set('Referer', VIDEASY_ORIGIN + '/');
  headers.set('Accept', '*/*');
  headers.set('Accept-Encoding', 'identity');
  headers.set('Connection', 'keep-alive');

  // Copy Range header if present (important for resuming)
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
      // CDN rejected — try without Origin/Referer
      var fallbackHeaders = new Headers();
      fallbackHeaders.set('User-Agent', USER_AGENT);
      if (rangeHeader) fallbackHeaders.set('Range', rangeHeader);

      return fetch(cdnUrl, {
        method: request.method,
        headers: fallbackHeaders,
        mode: 'cors',
        credentials: 'omit',
      }).then(function(r2) {
        if (!r2.ok) {
          // If CORS still fails, fall back to server proxy
          return fetch(request);
        }
        return addCorsHeaders(r2);
      }).catch(function() {
        // Network error — fall back to server proxy
        return fetch(request);
      });
    }

    // Cache small responses (manifests, init segments)
    var contentType = response.headers.get('Content-Type') || '';
    var contentLength = parseInt(response.headers.get('Content-Length') || '0', 10);

    if (contentType.indexOf('mpegurl') !== -1 || contentLength < 100000) {
      response.clone().text().then(function(body) {
        var hdrs = {};
        response.headers.forEach(function(v, k) { hdrs[k] = v; });
        manifestCache.set(cacheKey, {
          body: body,
          headers: hdrs,
          time: Date.now()
        });
      });
    }

    return addCorsHeaders(response);
  }).catch(function(err) {
    // Direct CDN fetch failed — fall back to server proxy
    console.log('[SW-Proxy] Direct fetch failed, falling back to server proxy:', err.message);
    return fetch(request);
  });
}

function addCorsHeaders(response) {
  var newHeaders = new Headers(response.headers);
  newHeaders.set('Access-Control-Allow-Origin', '*');
  newHeaders.set('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS');
  newHeaders.set('Access-Control-Allow-Headers', 'Range, Origin, Accept, Referer, User-Agent');

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: newHeaders
  });
}
