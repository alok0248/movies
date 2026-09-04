/* ===== Client-Side Source Extraction ===== */
/*
 * Runs entirely in the browser.
 *  - Tries the speedracelight API DIRECTLY (the API sends CORS: *).
 *  - Falls back to the '/proxy/' URL ONLY when the browser service worker
 *    (sw-proxy.js) is active, so the request is still fetched by the browser
 *    itself — it never goes through the Django server.
 */
var ClientExtract = (function() {
  var API_DIRECT = 'https://api.speedracelight.com';
  var API_PROXY = '/proxy/api.speedracelight.com';
  var SERVERS = [
    ['cdn', 'Yoru'], ['vsrc', 'Neon'], ['m4uhd', 'Breach'],
    ['downloader2', 'Cypher'], ['lamovie', 'Omen'], ['meine', 'Killjoy'],
    ['hdmovie', 'Vyse'], ['superflix', 'Raze']
  ];
  var REQUEST_TIMEOUT = 8000;
  var SEED_TIMEOUT = 8000;

  /* ================= Cipher (unchanged port of the server decrypt) ================= */
  var F = [1116352408,1899447441,3049323471,3921009573,961987163,1508970993,
           2453635748,2870763221,3624381080,310598401,607225278,1426881987,
           1925078388,2162078206,2614888103,3248222580];
  var MAGIC = [109,118,109,49];

  function _w(e) {
    e = e >>> 0;
    e ^= e >>> 16; e = Math.imul(e, 2246822507) >>> 0;
    e ^= e >>> 13; e = Math.imul(e, 3266489909) >>> 0;
    e ^= e >>> 16;
    return e >>> 0;
  }
  function _v(e, t) {
    e = e >>> 0; t = t & 31;
    if (t === 0) return e;
    return ((e << t) | (e >>> (32 - t))) >>> 0;
  }
  function _fnv1a(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    return _w(h);
  }
  function _b(e) { return (e * (e + 1) & 1) === 0; }

  function decrypt(encStr, seedStr, tmdbId) {
    var b64 = encStr.replace(/-/g, '+').replace(/_/g, '/');
    b64 += '='.repeat((4 - b64.length % 4) % 4);
    var raw = atob(b64);
    var enc = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) enc[i] = raw.charCodeAt(i);

    var a = _w(_fnv1a(seedStr) ^ _w((tmdbId >>> 0) ^ 2654435769)) >>> 0;
    var S = new Array(61);
    for (var e = 0; e < 8; e++) {
      if (_b(e)) {
        var t = a % 61;
        a = _v((a + 2654435769) >>> 0, 7 + (7 & e));
        S[t] = (a ^ _w(a)) >>> 0;
        a = _w((a + t) >>> 0);
      } else {
        S[e] = F[15 & e];
      }
    }

    var acc = _w((2779096485 ^ a) >>> 0);
    var out = new Uint8Array(enc.length);
    var o = 0;
    for (var e = 0; e < enc.length; ) {
      var curAcc = acc, n = curAcc % 61, i = 0 - Number(n in S);
      var d = S[n] >>> 0;
      var aVal = (d ^ Math.imul(2654435769, o + 1) >>> 0) >>> 0;
      var l = (((curAcc ^ aVal) >>> 0) | ((curAcc & aVal & i) >>> 0)) >>> 0;
      var t_val = _w((l = _v(l + curAcc >>> 0, 31 & n) ^ _v(curAcc, 31 & Math.imul(n, 7)) >>> 0) + 2654435769 >>> 0);
      S[n] = t_val >>> 0; acc = t_val; o++;
      out[e++] = 255 & t_val;
      if (e < enc.length) out[e++] = (t_val >>> 8) & 255;
      if (e < enc.length) out[e++] = (t_val >>> 16) & 255;
      if (e < enc.length) out[e++] = (t_val >>> 24) & 255;
    }
    for (var e = 0; e < enc.length; e++) enc[e] ^= out[e];
    for (var e = 0; e < MAGIC.length; e++) {
      if (enc[e] !== MAGIC[e]) throw new Error('Decryption failed');
    }
    return new TextDecoder('utf-8').decode(enc.slice(4));
  }

  /* ================= Browser capability helpers ================= */

  /* True when sw-proxy.js currently controls this page (fetch under '/proxy/' stays in the browser). */
  function swProxyActive() {
    if (!('serviceWorker' in navigator)) return false;
    var c = navigator.serviceWorker.controller;
    if (c && c.scriptURL && c.scriptURL.indexOf('/sw-proxy.js') !== -1) return true;
    return false;
  }

  /* Wait briefly for the proxy SW to claim the page (first visit race). */
  function waitForSw(timeoutMs) {
    timeoutMs = timeoutMs || 2500;
    return new Promise(function(resolve) {
      if (swProxyActive()) { resolve(true); return; }
      if (!('serviceWorker' in navigator)) { resolve(false); return; }
      var done = false;
      function finish(val) { if (!done) { done = true; resolve(val); } }
      var to = setTimeout(function() { finish(swProxyActive()); }, timeoutMs);
      navigator.serviceWorker.ready.then(function(reg) {
        if (reg.active) {
          /* After ready, the controller may still update a tick later. */
          setTimeout(function() { finish(swProxyActive()); }, 250);
        } else {
          finish(false);
        }
      }).catch(function() { finish(false); });
      navigator.serviceWorker.addEventListener('controllerchange', function handler() {
        if (swProxyActive()) { clearTimeout(to); finish(true); }
      });
    });
  }

  /* Fetch with timeout + abort. mode: 'cors' for direct, 'same-origin' for /proxy/. */
  function fetchWithTimeout(url, mode, timeoutMs) {
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function() { try { ctrl.abort(); } catch (e) {} }, timeoutMs || REQUEST_TIMEOUT) : null;
    return fetch(url, {
      mode: mode,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Accept': 'application/json, text/plain, */*' },
      signal: ctrl ? ctrl.signal : undefined
    }).then(function(r) {
      if (timer) clearTimeout(timer);
      return r;
    }, function(err) {
      if (timer) clearTimeout(timer);
      throw err;
    });
  }

  /* Try DIRECT (browser → api.speedracelight.com, CORS-open). If that is blocked
     AND the browser proxy SW is active, retry via '/proxy/...' (still browser-side). */
  function fetchCorsSafe(urlPath, params, timeoutMs) {
    var directUrl = API_DIRECT + urlPath + '?' + params;
    var viaProxy = false;
    var proxyUrl = API_PROXY + urlPath + '?' + params;
    return waitForSw(1500).then(function(swOk) {
      return fetchWithTimeout(directUrl, 'cors', timeoutMs).then(
        function(r) { return { response: r, via: 'direct' }; },
        function(err) {
          if (!swOk) throw err;              /* never let /proxy/ reach the server */
          viaProxy = true;
          return fetchWithTimeout(proxyUrl, 'same-origin', timeoutMs).then(
            function(r) { return { response: r, via: 'proxy' }; },
            function(err2) { throw err2; }
          );
        }
      );
    }).then(function(res) {
      var r = res.response;
      if (r.status === 401 || r.status === 403) throw new Error('seed rejected');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    });
  }

  function getSeed(tmdbId) {
    return fetchCorsSafe('/seed', 'mediaId=' + encodeURIComponent(tmdbId), SEED_TIMEOUT)
      .then(function(text) {
        try { return JSON.parse(text).seed || ''; } catch (e) { return ''; }
      });
  }

  function fetchFromServer(serverKey, serverName, tmdbId, mediaType, seed, timestamp, season, episode) {
    var params = [
      'title=', 'mediaType=' + encodeURIComponent(mediaType), 'year=',
      'episodeId=' + encodeURIComponent(episode || '1'), 'seasonId=' + encodeURIComponent(season || '1'),
      'tmdbId=' + encodeURIComponent(tmdbId), 'imdbId=', 'enc=2',
      'seed=' + encodeURIComponent(seed), '_t=' + encodeURIComponent(timestamp)
    ].join('&');
    return fetchCorsSafe('/' + serverKey + '/sources-with-title', params, REQUEST_TIMEOUT)
      .then(function(text) {
        var decrypted = decrypt(text, seed, parseInt(tmdbId));
        var data = JSON.parse(decrypted);
        return { server: serverName, sources: data.sources || [], subtitles: data.subtitles || [] };
      });
  }

  /**
   * Extract sources for a title entirely in the browser.
   *
   * options.onSource(src)    — called as each playable source is found ({url, quality, language, server})
   * options.onSubtitles(sub) — called for each unique subtitle track found (raw payload item, has .url)
   * options.onDone(err)      — called once all servers finished. err is null on success (even 0 sources).
   * options.onStatus(msg)    — optional progress label, e.g. "Checked 2/8 servers…"
   *
   * The first source is delivered as quickly as possible; the rest stream in.
   */
  function extractSources(tmdbId, mediaType, season, episode, options) {
    options = options || {};
    var ts = Date.now().toString();
    var sent = {};
    var subSent = {};
    var completed = 0;
    var foundAny = false;
    var maxParallel = 4;
    var idx = 0;

    function reportStatus() {
      if (typeof options.onStatus === 'function') {
        options.onStatus('Scanning servers ' + completed + '/' + SERVERS.length + '…');
      }
    }

    function finish(err) {
      if (typeof options.onDone === 'function') options.onDone(err);
    }

    var sessionSeed = '';

    /* Run servers with small concurrency so results stream in fast but we don't hammer the API. */
    function worker() {
      if (idx >= SERVERS.length) return Promise.resolve();
      var cur = SERVERS[idx++];
      return fetchFromServer(cur[0], cur[1], tmdbId, mediaType, sessionSeed, ts, season, episode)
        .then(function(result) {
          var srcs = result.sources || [];
          for (var i = 0; i < srcs.length; i++) {
            var src = srcs[i];
            if (!src || !src.url || sent[src.url]) continue;
            sent[src.url] = 1;
            foundAny = true;
            var lang = src.language || src.audioLanguage || src.audio || '';
            if (/^\d+p?$/i.test(src.quality || '')) lang = lang || '';
            if (typeof options.onSource === 'function') {
              options.onSource({
                url: src.url,
                quality: src.quality || '?',
                language: lang,
                server: result.server
              });
            }
          }
          var subs = result.subtitles || [];
          for (var j = 0; j < subs.length; j++) {
            var sub = subs[j];
            if (!sub || !sub.url || subSent[sub.url]) continue;
            subSent[sub.url] = 1;
            if (typeof options.onSubtitles === 'function') {
              options.onSubtitles(sub);
            }
          }
        })
        .catch(function() { /* that server had nothing usable — keep going */ })
        .then(function() {
          completed++;
          reportStatus();
        })
        .then(function() { return worker(); });
    }

    getSeed(tmdbId).then(function(seed) {
      if (!seed) { finish('Could not fetch a session seed from the source API.'); return; }
      sessionSeed = seed;
      reportStatus();
      var runners = [];
      for (var i = 0; i < Math.min(maxParallel, SERVERS.length); i++) runners.push(worker());
      return Promise.all(runners).then(function() {
        finish(foundAny ? null : 'No servers returned any streams for this title.');
      });
    }, function(err) {
      finish('The source API could not be reached from your browser (' +
        (err && err.message ? err.message : 'network/CORS') + ').');
    }).catch(function(err) {
      finish('Stream lookup failed in your browser (' +
        (err && err.message ? err.message : 'unexpected error') + '). Try again.');
    });
  }

  return { extractSources: extractSources, getSeed: getSeed, decrypt: decrypt, swProxyActive: swProxyActive };
})();
