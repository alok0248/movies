// Service Worker: transparently proxies CDN video requests through our server
// This allows the browser to play CDN streams that would otherwise be blocked by CORS/obfuscation

var PROXY_PREFIX = '/proxy/';

self.addEventListener('install', function(e) {
  self.skipWaiting();
});

self.addEventListener('activate', function(e) {
  e.waitUntil(clients.claim());
});

self.addEventListener('fetch', function(e) {
  var url = e.request.url;

  // Only proxy requests to known CDN domains
  var shouldProxy = false;
  var domains = [
    'peakstorm.top', 'ashencloud.top', 'moon.peakstorm.top',
    'sun.peakstorm.top', 'slast430did.com', 'i-arch-400.slast430did.com',
    'vimeos.zip', 'p5.vimeos.zip',
    'workers.dev', 'vr6q4oelwfusw.workers.dev'
  ];

  for (var i = 0; i < domains.length; i++) {
    if (url.indexOf(domains[i]) !== -1) {
      shouldProxy = true;
      break;
    }
  }

  if (!shouldProxy) return;

  // Parse the URL and route through proxy
  try {
    var u = new URL(url);
    var proxyUrl = PROXY_PREFIX + u.hostname + u.pathname + u.search;

    e.respondWith(
      fetch(proxyUrl, {
        method: e.request.method,
        headers: e.request.headers,
        mode: 'cors',
        credentials: 'same-origin'
      }).then(function(response) {
        if (!response.ok) {
          // If proxy fails, try direct
          return fetch(e.request);
        }
        return response;
      }).catch(function() {
        return fetch(e.request);
      })
    );
  } catch(err) {
    // Let browser handle non-CDN requests normally
  }
});
