/* ===== Client-Side Source Extraction ===== */
/* Runs entirely in the browser - no server needed for extraction */
var ClientExtract = (function() {
  var API = 'https://api.speedracelight.com';
  var SERVERS = [
    ['cdn', 'Yoru'], ['vsrc', 'Neon'], ['m4uhd', 'Breach'],
    ['downloader2', 'Cypher'], ['lamovie', 'Omen'], ['meine', 'Killjoy'],
    ['hdmovie', 'Vyse'], ['superflix', 'Raze']
  ];

  /* ---- Cipher constants ---- */
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

  /* ---- Public API ---- */
  function getSeed(tmdbId) {
    return fetch(API + '/seed?mediaId=' + tmdbId)
      .then(function(r) { return r.json(); })
      .then(function(d) { return d.seed || ''; });
  }

  function fetchFromServer(serverKey, serverName, tmdbId, mediaType, seed, timestamp, season, episode) {
    var params = [
      'title=', 'mediaType=' + mediaType, 'year=',
      'episodeId=' + (episode || '1'), 'seasonId=' + (season || '1'),
      'tmdbId=' + tmdbId, 'imdbId=', 'enc=2',
      'seed=' + encodeURIComponent(seed), '_t=' + timestamp
    ].join('&');
    var url = API + '/' + serverKey + '/sources-with-title?' + params;

    return fetch(url, {
      headers: {
        'Accept': 'application/json, text/plain, */*',
        'Cache-Control': 'no-cache'
      }
    })
    .then(function(r) {
      if (r.status === 401) throw new Error('seed rejected');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    })
    .then(function(text) {
      var decrypted = decrypt(text, seed, parseInt(tmdbId));
      var data = JSON.parse(decrypted);
      return { server: serverName, sources: data.sources || [], subtitles: data.subtitles || [] };
    });
  }

  /**
   * Fetch all sources for a movie/series from all servers.
   * Calls back with each source as it arrives.
   * Returns a promise that resolves when all servers are done.
   */
  function extractSources(tmdbId, mediaType, season, episode, onSource, onDone) {
    var ts = Date.now().toString();
    var sent = {};

    return getSeed(tmdbId).then(function(seed) {
      if (!seed) throw new Error('No seed');

      var promises = SERVERS.map(function(s) {
        return fetchFromServer(s[0], s[1], tmdbId, mediaType, seed, ts, season, episode)
          .then(function(result) {
            (result.sources || []).forEach(function(src) {
              if (src && src.url && !sent[src.url]) {
                sent[src.url] = 1;
                var lang = src.language || src.audioLanguage || src.audio || '';
                if (/^\d+p?$/i.test(src.quality || '')) lang = lang || '';
                onSource({
                  url: src.url,
                  quality: src.quality || '?',
                  language: lang,
                  server: result.server
                });
              }
            });
          })
          .catch(function(e) { /* skip failed server */ });
      });

      return Promise.all(promises).then(function() {
        if (onDone) onDone();
      });
    });
  }

  return { extractSources: extractSources, getSeed: getSeed, decrypt: decrypt };
})();
