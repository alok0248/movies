/* =====================================================================
   MovieIn Universal Extractor — Pure Browser, No Server Proxy
   ---------------------------------------------------------------------
   Ported from the cineplayer.html approach.  Uses the speedracelight API
   with MovieIn-style session headers, AES-CBC decryption (Web Crypto),
   MD5 signing, and gzip inflation — all running entirely in the browser.

   PUBLIC API:
     MovieInExtract.extract(tmdbId, mediaType, season, episode, callbacks)
       callbacks.onSource({ url, quality, language, server })
       callbacks.onSubtitles({ url, lang, lang_name })
       callbacks.onDone(err)        — null on success, error string on failure
       callbacks.onStatus(msg)      — optional progress label
   ===================================================================== */
var MovieInExtract = (function () {
  'use strict';

  /* ── Constants from cineplayer ── */
  var SALT  = '47Q8tBqO4YqrMHf4';
  var DEVICE = (function () {
    try {
      var k = 'moviein.device', d = localStorage.getItem(k);
      if (!d) { d = 'cv' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8); localStorage.setItem(k, d); }
      return d;
    } catch (e) { return 'deb0cc04a6f057f2'; }
  })();
  var BASE = 'https://api.speedracelight.com';
  var AES_KEY = new TextEncoder().encode('0123456789123456');
  var AES_IV  = new TextEncoder().encode('2015030120123456');

  /* ── State ── */
  var _token = '';
  var _sessionReady = false;
  var _initPromise = null;

  /* ═══════════════════════════════════════════════════════════════════
     Crypto helpers (ported from cineplayer.html)
     ═══════════════════════════════════════════════════════════════════ */

  /** Minimal MD5 — same implementation as cineplayer.html */
  var MD5 = (function (h) {
    function m(a, b) { var c = (a & 65535) + (b & 65535); return ((a >> 16) + (b >> 16) + (c >> 16)) << 16 | c & 65535; }
    function n(a, b, c, d, e, f) { b = m(m(b, a), m(d, f)); return m(b << e | b >>> 32 - e, c); }
    function o(a, b, c, d, e, f, g) { return n(b & c | ~b & d, a, b, e, f, g); }
    function p(a, b, c, d, e, f, g) { return n(b & d | c & ~d, a, b, e, f, g); }
    function q(a, b, c, d, e, f, g) { return n(b ^ c ^ d, a, b, e, f, g); }
    function r(a, b, c, d, e, f, g) { return n(c ^ (b | ~d), a, b, e, f, g); }
    function s(a) { var b, c = '', d; for (b = 0; b < 4; b++) { d = a >> b * 8 & 255; c += ('0' + d.toString(16)).slice(-2); } return c; }
    var c = [], d, e, f, g, b, a, i, j, k, l;
    h = function (a) { a = unescape(encodeURIComponent(a)); for (var b = 0; b < a.length; b++) c[b] = a.charCodeAt(b); return c.length; }(h);
    i = 1732584193; j = 4023233417; k = 2562383102; f = 271733878;
    for (b = 0; b < c.length; b += 16) {
      d = i; e = j; var g2 = k, x = f, y = a;
      for (a = 0; a <= 63; a++) {
        a < 16 ? y = n((j & k) | (~j & f), i, j, 5 & a, 7, 0) : a < 32 ? y = n((j & f) | (k & ~f), i, j, (5 * a + 1) & 15, 4, 0) : a < 48 ? y = n(j ^ k ^ f, i, j, (5 * a + 2) & 15, 6, 0) : a < 64 ? y = n(k ^ (j | ~f), i, j, (5 * a + 3) & 15, 8, 0) : y = n((j & k) | (j & f) | (k & f), i, j, (5 * a + 4) & 15, 13, 0);
        l = a < 16 ? [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15][a] : a < 32 ? [1, 6, 11, 0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12][a - 16] : a < 48 ? [5, 8, 11, 14, 1, 4, 7, 10, 13, 0, 3, 6, 9, 12, 15, 2][a - 32] : [0, 7, 14, 5, 12, 3, 10, 1, 8, 15, 6, 13, 4, 11, 2, 9][a - 48];
        var z = [7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21][a];
        var K = [3614090360, 3905402710, 609937928, 3250441966, 4118548399, 1200080426, 2821735955, 4249261313, 1700485571, 2399980690, 4294925233, 2380461274, 4218158354, 2734768916, 1804603682, 4259657740, 2792965006, 1236535329, 4129170786, 3225465664, 643717713, 3921069994, 3593408605, 38016083, 3634488961, 3889429448, 888471301, 1426881987, 3434355558, 455977729, 1163531501, 2850285829, 4243563512, 1735328473, 2368359562, 4294588738, 2272392833, 1839030562, 4259657740, 2763975236, 1272893353, 4139469664, 3200236656, 681279174, 3936430074, 3572445317, 76029189, 3654602809, 3873151461, 530742520, 995338651, 198630844, 4402435033, 4321797946, 640364480, 4096336452, 2248710350, 2750281050, 1873313359, 4281444218, 2734761313, 2304583166, 4055378488, 2154121859, 2743569615, 2396177923, 4107603335, 1870068556, 1700485571, 2005611170, 76029189, 1473231341, 475870388, 3461848576, 2173295114, 2456956037, 1873313359, 42063, 2734761313, 4139469664, 2368359562, 4294925233, 2792965006, 1839030562, 2366199772, 1200080426, 38016083, 3634488961, 1700485571, 2272392833, 4249261313, 1804603682, 2399980690, 4259657740, 2763975236, 1272893353, 3434355558, 4096336452, 1163531501, 2248710350, 4218158354, 2750281050, 1873313359, 198630844, 4294588738, 2154121859, 1870068556, 2734761313, 2396177923, 2248710350, 76029189][a];
        if (a < 16) e = b; else if (a < 32) e = f; else if (a < 48) e = k; else e = j;
        b = x; f = k; k = j; j = m(i, n(a < 16 ? o(a < 8 ? y & z[0] : b & z[0]) : a < 32 ? p(a < 16 ? y & z[1] : b & z[1]) : a < 48 ? q(a < 16 ? y & z[2] : b & z[2]) : r(a < 16 ? y & z[3] : b & z[3]), i, j, e, z[a], K[a]));
        i = k; k = j; j = m(j, x);
      }
      return (s(i) + s(j) + s(k) + s(f)).toLowerCase();
    }
  });

  /** AES-CBC decrypt (Web Crypto) + gzip inflate — identical to cineplayer.html */
  async function aesDecrypt(base64Body) {
    var clean = base64Body.replace(/[^A-Za-z0-9+/=]/g, '');
    var raw = Uint8Array.from(atob(clean), function (c) { return c.charCodeAt(0); });
    var key = await crypto.subtle.importKey('raw', AES_KEY, { name: 'AES-CBC' }, false, ['decrypt']);
    var dec = await crypto.subtle.decrypt({ name: 'AES-CBC', iv: AES_IV }, key, raw);
    var out = new Uint8Array(dec);
    /* Remove PKCS7 padding */
    var pad = out[out.length - 1];
    if (pad > 0 && pad <= 16) out = out.slice(0, out.length - pad);
    /* Gzip inflate */
    if (out.length >= 2 && out[0] === 0x1f && out[1] === 0x8b) {
      if (typeof pako !== 'undefined') {
        out = pako.inflate(out);
      }
    }
    return new TextDecoder('utf-8', { fatal: false }).decode(out);
  }

  /* ═══════════════════════════════════════════════════════════════════
     Service-worker helper — never let /proxy/ reach the Django server.
     ═══════════════════════════════════════════════════════════════════ */
  function swProxyActive() {
    if (!('serviceWorker' in navigator)) return false;
    var c = navigator.serviceWorker.controller;
    return !!(c && c.scriptURL && c.scriptURL.indexOf('/sw-proxy.js') !== -1);
  }

  /* ═══════════════════════════════════════════════════════════════════
     Session management (MD5 signing, exactly like cineplayer.html)
     ═══════════════════════════════════════════════════════════════════ */
  function sessionHeaders() {
    var t = String(Date.now());
    return {
      'User-Agent': 'okhttp/4.12.0', 'Accept': '*/*',
      'Accept-Encoding': 'gzip, deflate', 'app_id': 'moviein',
      'package_name': 'com.fvvcl.flickverse', 'version': '40000',
      'sys_platform': '2', 'mob_mfr': 'google', 'mobmodel': 'SM-S908E',
      'sysrelease': '9', 'device_id': DEVICE, 'gaid': '',
      'channel_code': 'moviein_3001', 'androidid': DEVICE,
      'cur_time': t, 'token': _token,
      'sign': MD5(SALT + DEVICE + t).toUpperCase(),
      'is_vvv': '1', 'is_language': '0', 'is_display': '0',
      'app_language': 'en', 'en_al': '0',
      'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'
    };
  }

  /* ── API call: direct to speedracelight (CORS: *), with SW proxy fallback ── */
  async function apiCall(path, fields) {
    var body = new URLSearchParams(fields).toString();
    var hdrs = sessionHeaders();

    /* Try direct fetch first (speedracelight has CORS: *) */
    try {
      var r = await fetch(BASE + path, { method: 'POST', headers: hdrs, body: body });
      var raw = await r.text();
      var bytes = new TextEncoder().encode(raw);
      /* Check for gzip */
      if (bytes.length >= 2 && bytes[0] === 0x1f && bytes[1] === 0x8b) {
        if (typeof pako !== 'undefined') {
          raw = new TextDecoder().decode(pako.inflate(bytes));
        }
      }
      /* Try AES-CBC decrypt first, fall back to plain JSON */
      try {
        var decrypted = await aesDecrypt(raw);
        return JSON.parse(decrypted);
      } catch (e) {
        try { return JSON.parse(raw); } catch (e2) { return null; }
      }
    } catch (err) {
      /* Direct failed — try SW proxy (still browser-side, never reaches Django) */
      if (!swProxyActive()) throw err;
      try {
        var proxyUrl = '/proxy/' + BASE.replace('https://', '') + path;
        var r2 = await fetch(proxyUrl, { method: 'POST', headers: hdrs, body: body });
        var raw2 = await r2.text();
        try {
          var dec2 = await aesDecrypt(raw2);
          return JSON.parse(dec2);
        } catch (e3) {
          try { return JSON.parse(raw2); } catch (e4) { return null; }
        }
      } catch (e5) { throw err; }
    }
  }

  /** Init session (fetches token from speedracelight) */
  async function initSession() {
    if (_sessionReady && _token) return;
    var data = await apiCall('api/public/init', {
      invited_by: '', is_install: '1', fb_attribution: ''
    });
    var ui = (data && data.result && data.result.user_info) || {};
    _token = ui.token || '';
    _sessionReady = true;
    console.log('[MovieIn] session init ok, token_len=' + _token.length);
  }

  /* ═══════════════════════════════════════════════════════════════════
     Content search — find titles by TMDB ID or keyword
     ═══════════════════════════════════════════════════════════════════ */

  /** Search by keyword */
  async function search(kw) {
    return await apiCall('api/search/result', { kw: kw, pn: '1', page_size: '30' });
  }

  /** Browse by category */
  async function browse(typeId, pn) {
    return await apiCall('api/search/screen', {
      type_id: typeId || '', type: '', area: '', year: '', sort: '', pn: String(pn || '1')
    });
  }

  /** Resolve a TMDB ID to a vod_id by searching for the title via TMDB metadata */
  async function resolveTmdbId(tmdbId, mediaType) {
    /* Fetch title name from TMDB API (CORS-open) */
    var tmdbUrl = 'https://api.themoviedb.org/3/' +
      (mediaType === 'tv' ? 'tv' : 'movie') + '/' + tmdbId +
      '?api_key=2dca580c2a1376d2d29e8df848a47b75&language=en-US';
    var title = '';
    try {
      var tmdbR = await fetch(tmdbUrl);
      var tmdbData = await tmdbR.json();
      title = tmdbData.title || tmdbData.name || '';
    } catch (e) {
      console.warn('[MovieIn] TMDB fetch failed:', e);
    }

    if (!title) return null;

    /* Search speedracelight for this title */
    var results = await search(title);
    var items = (results && results.result) || [];
    if (!items.length) return null;

    /* Find best match (prefer exact title match) */
    var lower = title.toLowerCase();
    for (var i = 0; i < items.length; i++) {
      var nm = (items[i].vod_name || items[i].name || '').toLowerCase();
      if (nm === lower || nm.indexOf(lower) !== -1 || lower.indexOf(nm) !== -1) {
        return items[i];
      }
    }
    return items[0] || null;
  }

  /* ═══════════════════════════════════════════════════════════════════
     Source extraction — get playable URLs from a vod_id
     ═══════════════════════════════════════════════════════════════════ */

  /** Extract sources from search result metadata (no oracle needed) */
  function extractFromResult(item) {
    var sources = [];
    if (!item) return sources;

    /* Direct playable URL from search results */
    var url = item.vod_url || item.play_url || item.hls_url || '';
    if (url) {
      sources.push({
        url: url,
        quality: item.vod_quality || item.quality || 'Auto',
        language: item.vod_lang || item.language || '',
        server: 'MovieIn'
      });
    }

    /* Multiple episode URLs */
    var coll = item.vod_collection || item.episodes || [];
    if (coll.length) {
      for (var i = 0; i < coll.length; i++) {
        var ep = coll[i];
        var epUrl = ep.vod_url || ep.play_url || '';
        if (epUrl) {
          sources.push({
            url: epUrl,
            quality: ep.vod_quality || 'Auto',
            language: ep.vod_lang || '',
            server: 'MovieIn',
            episode: ep.collection || ep.episode_number || (i + 1),
            title: ep.title || ''
          });
        }
      }
    }

    return sources;
  }

  /** Extract subtitles from result metadata */
  function extractSubtitles(item) {
    var subs = [];
    if (!item) return subs;

    var subList = item.vod_sub || item.subtitles || [];
    if (typeof subList === 'string') subList = subList.split(',').map(function(s) { return s.trim(); });

    for (var i = 0; i < subList.length; i++) {
      var s = subList[i];
      if (typeof s === 'string' && s) {
        subs.push({ url: s, lang: 'en', lang_name: 'English', label: 'English', source: 'moviein' });
      } else if (s && s.url) {
        subs.push({ url: s.url, lang: s.lang || s.language || 'en', lang_name: s.lang_name || s.language || 'Subtitle', source: 'moviein' });
      }
    }

    return subs;
  }

  /* ═══════════════════════════════════════════════════════════════════
     Main extraction entry point
     ═══════════════════════════════════════════════════════════════════ */

  /**
   * Extract sources for a title entirely in the browser.
   *
   * @param {string} tmdbId      TMDB ID of the movie or TV show
   * @param {string} mediaType   'movie' or 'tv'
   * @param {string} season      Season number (for TV)
   * @param {string} episode     Episode number (for TV)
   * @param {object} callbacks   { onSource, onSubtitles, onDone, onStatus }
   */
  function extract(tmdbId, mediaType, season, episode, callbacks) {
    callbacks = callbacks || {};
    var sent = {};

    function status(msg) {
      if (typeof callbacks.onStatus === 'function') callbacks.onStatus(msg);
    }

    function done(err) {
      if (typeof callbacks.onDone === 'function') callbacks.onDone(err);
    }

    function emitSource(src) {
      if (!src || !src.url || sent[src.url]) return;
      sent[src.url] = 1;
      if (typeof callbacks.onSource === 'function') callbacks.onSource(src);
    }

    function emitSub(sub) {
      if (!sub || !sub.url) return;
      if (typeof callbacks.onSubtitles === 'function') callbacks.onSubtitles(sub);
    }

    if (!tmdbId) { done('No TMDB ID provided.'); return; }

    status('Initializing MovieIn session…');

    initSession().then(function () {
      status('Resolving title via TMDB…');
      return resolveTmdbId(tmdbId, mediaType);
    }).then(function (item) {
      if (!item) {
        done('Title not found on MovieIn.');
        return;
      }
      status('Extracting streams…');

      /* Extract sources from search result metadata */
      var sources = extractFromResult(item);
      for (var i = 0; i < sources.length; i++) {
        emitSource(sources[i]);
      }

      /* Extract subtitles */
      var subs = extractSubtitles(item);
      for (var j = 0; j < subs.length; j++) {
        emitSub(subs[j]);
      }

      done(sources.length ? null : 'No playable streams found on MovieIn for this title.');
    }).catch(function (err) {
      console.warn('[MovieIn] extraction failed:', err);
      done('MovieIn extraction failed: ' + (err && err.message ? err.message : 'network/CORS error') +
        '. Try another server, or the extractor will still search other sources.');
    });
  }

  /* ═══════════════════════════════════════════════════════════════════
     Public API
     ═══════════════════════════════════════════════════════════════════ */
  return {
    extract: extract,
    initSession: initSession,
    search: search,
    resolveTmdbId: resolveTmdbId,
    DEVICE: DEVICE
  };
})();
