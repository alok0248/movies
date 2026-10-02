/**
 * MBTV (MovieBoxTV / aoneroom) client-side source extractor.
 *
 * Ported from the reverse-engineered portable logic (movieboxtv_player.py).
 * All protocol logic runs in the browser: guest JWT bootstrap, request
 * signing, dub/language resolution, episode listing and play-link
 * extraction. The Django server only relays the already-signed HTTP
 * request to tv.aoneroom.com (no logic, no secrets) and proxies media
 * bytes for playback.
 *
 * Callback contract (same as the other player extractors):
 *   MBTVExtract.extract(tmdbId, mediaType, season, episode, {
 *     onStatus:  function(text),
 *     onSource:  function({url, quality, language, server}),
 *     onDone:    function(err)
 *   })
 *
 * Flow per title:
 *   1. Resolve TMDB id -> title/year in browser (TMDB API is CORS-open)
 *   2. Search the MBTV catalog for that title (signed request)
 *   3. Pick best match (exact > substring > word overlap, +year bonus)
 *   4. Resolve dubs (languages) for the matched subject
 *   5. Fetch play-info for each dub (+season/episode) and emit every
 *      resource (resolution) as a source. Signed mp4 first, DASH/HLS
 *      streams last, exactly like the portable player.
 */
(function (global) {
  'use strict';

  var SECRET_ONLINE = '76iRl07s0xSN9jqmEWAt79EBJZulIQIsV64FZr2O';
  var CONNECT_HOST = 'tv.aoneroom.com';
  var API_HOST = 'api6.aoneroom.com';
  var DEVICE_ID = 'a3f9c2e17b8d4e059f6a1c2b3d4e5f60';

  var RELAY_URL = '/ajax/mbtv/relay/';
  var MEDIA_PROXY_URL = '/mbtv-media/';
  var TMDB_API = 'https://api.themoviedb.org/3';

  /* Request-scoped tokens so other sites cannot reuse our endpoints. */
  var RELAY_SALT = 'mbtv-relay-v1';
  var MEDIA_SALT = 'mbtv-media-v1';

  /* ======================================================================
   * MD5 + HMAC-MD5 — Web Crypto deliberately excludes MD5 but the
   * aoneroom gateway signs with HMAC-MD5. Per RFC 1321 the round
   * constants are T[i] = floor(2^32 * |sin(i+1)|); they are GENERATED
   * here rather than hardcoded so a typo is impossible.
   * ==================================================================== */
  var MD5_S = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
  var MD5_K = (function () {
    var k = [];
    for (var i = 0; i < 64; i++) {
      k.push(Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296));
    }
    return k;
  })();

  function rotl(n, b) { return ((n << b) | (n >>> (32 - b))) | 0; }

  function utf8Bytes(str) {
    var out = [], i, c;
    str = unescape(encodeURIComponent(str));
    for (i = 0; i < str.length; i++) {
      c = str.charCodeAt(i);
      out.push(c & 0xff);
    }
    return out;
  }

  function bytesToWords(bytes) {
    var words = [], i;
    for (i = 0; i < bytes.length; i++) {
      words[i >> 2] = (words[i >> 2] || 0) | (bytes[i] << ((i % 4) * 8));
    }
    return words;
  }

  /* Core MD5 over a byte array; returns the 4-word state as unsigned.
     Padding is byte-level per RFC 1321: append 0x80, pad to 56 mod 64,
     then the 64-bit little-endian bit length. */
  function md5Bytes(bytes) {
    var bitLen = bytes.length * 8;
    var padded = bytes.slice();
    padded.push(0x80);
    while (padded.length % 64 !== 56) padded.push(0);
    var lenLo = bitLen >>> 0;
    var lenHi = Math.floor(bitLen / 4294967296) >>> 0;
    var li;
    for (li = 0; li < 4; li++) padded.push((lenLo >>> (li * 8)) & 0xff);
    for (li = 0; li < 4; li++) padded.push((lenHi >>> (li * 8)) & 0xff);
    var msg = bytesToWords(padded);

    var h = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476];
    var i, off;
    for (off = 0; off < msg.length; off += 16) {
      var m = [];
      for (i = 0; i < 16; i++) m.push(msg[off + i] >>> 0);
      var a = h[0], b = h[1], c = h[2], d = h[3];
      for (i = 0; i < 64; i++) {
        var f, g, tmp;
        if (i < 16) { f = (b & c) | (~b & d); g = i; }
        else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
        else if (i < 48) { f = b ^ c ^ d; g = (3 * i + 5) % 16; }
        else { f = c ^ (b | ~d); g = (7 * i) % 16; }
        tmp = d; d = c; c = b;
        b = (b + rotl((a + f + m[g] + MD5_K[i]) | 0, MD5_S[(i >> 4) * 4 + (i % 4)])) | 0;
        a = tmp;
      }
      h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
    }
    return h;
  }

  function wordsToHex(words) {
    var out = '', i, j, n;
    for (i = 0; i < words.length; i++) {
      n = words[i] >>> 0;
      for (j = 0; j < 4; j++) {
        out += ((n >> (j * 8 + 4)) & 0x0f).toString(16) + ((n >> (j * 8)) & 0x0f).toString(16);
      }
    }
    return out;
  }

  function md5hex(str) { return wordsToHex(md5Bytes(utf8Bytes(str))); }

  /* HMAC-MD5 (RFC 2104) over byte arrays; returns 4-word digest. */
  function hmacMd5Words(keyBytes, msgBytes) {
    if (keyBytes.length > 64) keyBytes = bytesFromWords(md5Bytes(keyBytes));
    var ipad = [], opad = [], i;
    for (i = 0; i < 64; i++) {
      var kb = i < keyBytes.length ? keyBytes[i] : 0;
      ipad.push(kb ^ 0x36);
      opad.push(kb ^ 0x5c);
    }
    var inner = md5Bytes(ipad.concat(msgBytes));
    var innerBytes = bytesFromWords(inner);
    return md5Bytes(opad.concat(innerBytes));
  }

  function bytesFromWords(words) {
    var out = [], i, n;
    for (i = 0; i < words.length; i++) {
      n = words[i];
      out.push(n & 0xff, (n >>> 8) & 0xff, (n >>> 16) & 0xff, (n >>> 24) & 0xff);
    }
    return out;
  }

  function wordsToB64(words) {
    var bytes = bytesFromWords(words);
    var chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
    var out = '', k, b1, b2, b3;
    for (k = 0; k < bytes.length; k += 3) {
      b1 = bytes[k]; b2 = bytes[k + 1] || 0; b3 = bytes[k + 2] || 0;
      out += chars[b1 >> 2] +
        chars[((b1 & 3) << 4) | (b2 >> 4)] +
        chars[((b2 & 15) << 2) | (b3 >> 6)] +
        chars[b3 & 63];
    }
    if (bytes.length % 3 === 1) return out.slice(0, -2) + '==';
    if (bytes.length % 3 === 2) return out.slice(0, -1) + '=';
    return out;
  }

  /* ======================================================================
   * Guest token + device blob (dj/c.java and xg/b.java from the APK)
   * ==================================================================== */
  function guestToken(tsMs) {
    var ts = String(tsMs != null ? tsMs : Date.now());
    return ts + ',' + md5hex(ts.split('').reverse().join(''));
  }

  function deviceInfo() {
    return JSON.stringify({
      package_name: 'com.community.mbox.tv',
      version_name: '1.1.6', version_code: 50040011,
      os: 'android', os_version: '13', device_id: DEVICE_ID,
      install_store: 'gp', brand: 'TECNO', model: 'TECNO KL5',
      system_language: 'en', net: 'wifi',
      region: 'US', timezone: 'America/New_York', sp_code: ''
    });
  }

  /* ======================================================================
   * Request signing (GatewaySignManager.doSign). Canonical string is
   * 7 newline-joined fields: METHOD / content-type / content-length /
   * bodylen-or-blank / ts / body-md5 / path?sorted-query.
   * ==================================================================== */
  function sortedQuery(qs) {
    var pairs = [];
    qs.split('&').forEach(function (part) {
      if (!part) return;
      var eq = part.indexOf('=');
      var k = eq < 0 ? part : part.slice(0, eq);
      var v = eq < 0 ? '' : part.slice(eq + 1);
      pairs.push([decodeURIComponent(k), decodeURIComponent(v)]);
    });
    pairs.sort(function (a, b) { return a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0); });
    return pairs.map(function (kv) { return kv[0] + '=' + kv[1]; }).join('&');
  }

  function signHeader(method, path, query, body, tsOverride) {
    var ts = tsOverride || Date.now();
    var p1 = '', p2 = '', bmd5 = '';
    if (body) {
      p1 = 'application/json; charset=UTF-8';
      p2 = String(utf8Bytes(body).length);
      bmd5 = md5hex(body);
    }
    var canon = [method.toUpperCase(), p1, p2, body ? p2 : '', String(ts), bmd5,
      path + (query ? '?' + sortedQuery(query) : '')].join('\n');
    var secretBytes = atob(SECRET_ONLINE).split('').map(function (ch) { return ch.charCodeAt(0) & 0xff; });
    var digest = hmacMd5Words(secretBytes, utf8Bytes(canon));
    return ts + '|2|' + wordsToB64(digest);
  }

  /* ======================================================================
   * Relay — same-origin Django endpoint that forwards the already-signed
   * request to aoneroom. All signing happens HERE; the relay adds nothing
   * (static device headers excepted) and holds no secrets.
   * ==================================================================== */
  function relayRequest(method, path, query, body, jwt) {
    var fullQuery = query ? query + '&host=' + API_HOST : 'host=' + API_HOST;
    var ts = Date.now();
    var headers = {
      'x-tr-signature': signHeader(method, path, fullQuery, body, ts),
      'X-Client-Token': guestToken(ts),
      'X-Client-Info': deviceInfo(),
      'X-Client-Status': '0'
    };
    if (jwt) headers['Authorization'] = 'Bearer ' + jwt;
    var payload = {
      m: method,
      p: path,
      q: fullQuery,
      b: body || '',
      h: headers,
      t: ts,
      x: md5hex(RELAY_SALT + '|' + method + '|' + path + '|' + fullQuery + '|' + (body || ''))
    };
    return fetch(RELAY_URL, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.error || ('relay HTTP ' + r.status));
        });
      }
      var jwt = r.headers.get('X-MBTV-JWT') || '';
      return r.json().then(function (j) { j.__jwt = jwt; return j; });
    });
  }

  /* ======================================================================
   * JWT cache (localStorage, like bff_jwt.txt in the portable player)
   * ==================================================================== */
  var JWT_KEY = 'mbtv_bff_jwt';
  function jwtExpiry(jwt) {
    try {
      var part = jwt.split('.')[1];
      part += new Array(-part.length % 4 + 1).join('=');
      var json = atob(part.replace(/-/g, '+').replace(/_/g, '/'));
      return (JSON.parse(json) || {}).exp || 0;
    } catch (e) { return 0; }
  }

  function getJwt() {
    var cached = '';
    try { cached = localStorage.getItem(JWT_KEY) || ''; } catch (e) { }
    if (cached && jwtExpiry(cached) > Date.now() / 1000 + 300) {
      return Promise.resolve(cached);
    }
    return relayRequest('GET', '/wefeed-tv-bff/user/info', '')
      .then(function (resp) {
        var jwt = resp.__jwt;
        if (!jwt) throw new Error('bootstrap failed: no token in relay response');
        try { localStorage.setItem(JWT_KEY, jwt); } catch (e) { }
        return jwt;
      });
  }

  /* ======================================================================
   * BFF calls (search, dubs, seasons, episodes, play-info)
   * ==================================================================== */
  function bffSearch(keyword, jwt, subjectType, page, perPage) {
    var q = 'keyword=' + encodeURIComponent(keyword) +
      '&page=' + (page || 1) +
      '&perPage=' + (perPage || 20) +
      '&subjectType=' + (subjectType || 0);
    return relayRequest('GET', '/wefeed-tv-bff/search/result', q, '', jwt)
      .then(function (r) { return (r.data || {}).items || []; });
  }

  /* search can come back empty right after a prior request (burst throttle,
     same class of problem as play-info) — back off and retry before giving
     up, otherwise the extractor reports a false "not available". */
  function bffSearchRetry(keyword, jwt, subjectType, page, perPage, tries) {
    tries = tries || 3;
    return bffSearch(keyword, jwt, subjectType, page, perPage).then(function (items) {
      if (items.length > 0 || tries <= 1) return items;
      return new Promise(function (res) { setTimeout(res, 900); })
        .then(function () { return bffSearchRetry(keyword, jwt, subjectType, page, perPage, tries - 1); });
    });
  }

  function dubInfo(sid, jwt) {
    return relayRequest('GET', '/wefeed-tv-bff/subject/dub-info', 'subjectId=' + sid, '', jwt)
      .then(function (r) { return (r.data || {}).items || []; });
  }

  function subjectResource(sid, jwt, se) {
    var q = 'subjectId=' + sid + '&page=1&perPage=50' + (se != null ? '&se=' + se : '');
    return relayRequest('GET', '/wefeed-tv-bff/subject/resource', q, '', jwt)
      .then(function (r) { return (r.data || {}).items || []; });
  }

  function playInfo(sid, jwt, se, ep) {
    var q = 'subjectId=' + sid + (se != null ? '&se=' + se : '') + (ep != null ? '&ep=' + ep : '');
    return relayRequest('GET', '/wefeed-tv-bff/subject/play-info', q, '', jwt)
      .then(function (r) { return r.data || {}; });
  }

  /* play-info intermittently answers with an empty body (burst rate-limit):
     whole languages silently vanish from the tracks list when it does.
     Back off and retry before giving up on a dub. */
  function playInfoRetry(sid, jwt, se, ep, tries) {
    tries = tries || 5;
    return playInfo(sid, jwt, se, ep).then(function (data) {
      if ((data.resources && data.resources.length) || (data.streams && data.streams.length)) return data;
      if (tries <= 1) return data;
      return new Promise(function (res) { setTimeout(res, 1000 + 500 * (5 - tries)); }).then(function () {
        return playInfoRetry(sid, jwt, se, ep, tries - 1);
      });
    });
  }

  /* "Original Audio" → "Original", "Hindi dub" → "Hindi". */
  function normLang(n) {
    n = String(n || '').trim();
    if (!n) return 'Original';
    if (/^original/i.test(n)) return 'Original';
    return n.replace(/\s*dub$/i, '').trim() || n;
  }

  /* "480" → "480p" so DASH resolutions match the mp4 labels. */
  function normRes(r) {
    var s = String(r == null ? '' : r).trim();
    return /^\d+$/.test(s) ? s + 'p' : (s || '?');
  }

  /* ======================================================================
   * Match scoring (same thresholds as the portable /api/bff/match)
   * ==================================================================== */
  function scoreMatch(title, candidate, year) {
    var t = (candidate.title || '').replace(/\[[^\]]*\]/g, '').trim().toLowerCase();
    var n = (title || '').trim().toLowerCase();
    if (!t) return 0;
    var score = 0;
    if (t === n) score = 1.0;
    else if (n.indexOf(t) > -1 || t.indexOf(n) > -1) score = 0.7;
    else {
      var a = n.split(/\s+/), b = t.split(/\s+/);
      var setB = {};
      b.forEach(function (w) { setB[w] = 1; });
      var inter = 0;
      a.forEach(function (w) { if (setB[w]) inter++; });
      score = 0.5 * inter / Math.max(a.length, 1);
    }
    var iy = (candidate.releaseDate || '').substring(0, 4);
    if (year && iy === String(year)) score += 0.25;
    else if (year && iy && /^\d+$/.test(iy) && /^\d+$/.test(String(year)) &&
      Math.abs(parseInt(iy, 10) - parseInt(year, 10)) <= 1) score += 0.1;
    return score;
  }

  /* ======================================================================
   * TMDB title resolution — through the same-origin Django endpoint so the
   * site's TMDB key never reaches the browser.
   * ==================================================================== */
  function resolveTmdbTitle(tmdbId, mediaType) {
    var token = md5hex('mbtv-title-v1|' + tmdbId + '|' + (mediaType === 'tv' ? 'tv' : 'movie'));
    return fetch('/ajax/mbtv/title/', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: String(tmdbId), type: mediaType === 'tv' ? 'tv' : 'movie', x: token })
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.error || ('title HTTP ' + r.status));
        });
      }
      return r.json();
    }).then(function (d) {
      return { title: d.title || '', year: String(d.year || '') };
    });
  }

  /* ======================================================================
   * Play extraction (mirrors extract_playable in the portable logic)
   * ==================================================================== */
  function fixUrl(u, cookie) {
    if (!u) return '';
    if (u.indexOf('//') === 0) u = 'https:' + u;
    if (cookie) {
      u += (u.indexOf('?') > -1 ? '&' : '?') +
        cookie.replace(/;\s*$/, '').split(';').map(function (s) { return s.trim(); }).filter(Boolean).join('&');
    }
    return u;
  }

  function resPx(r) {
    try { return parseInt(String(r.resolution || '0p').replace(/[pP]/g, ''), 10) || 0; }
    catch (e) { return 0; }
  }

  function extractPlayable(data) {
    var d = data || {};
    var resources = (d.resources || []).filter(function (r) { return r && r.url; });
    if (resources.length) {
      return {
        all: resources.map(function (r) {
          return { url: fixUrl(r.url), resolution: r.resolution, codec: r.codec, format: 'mp4' };
        })
      };
    }
    var streams = (d.streams || []).filter(function (s) { return s && s.url; });
    if (streams.length) {
      var out = streams.map(function (s) {
        var fmt = String(s.format || '').toLowerCase();
        var url = s.url || '';
        fmt = (fmt === 'dash' || url.indexOf('.mpd') > -1) ? 'dash'
          : (url.indexOf('.m3u8') > -1 ? 'hls' : 'mp4');
        return { url: fixUrl(url, s.signCookie), resolution: s.resolutions, codec: s.codecName, format: fmt };
      });
      return { all: out };
    }
    return null;
  }

  function mediaProxy(u) {
    return MEDIA_PROXY_URL + '?u=' + encodeURIComponent(u) + '&x=' + md5hex(MEDIA_SALT + u);
  }

  /* ======================================================================
   * Public entry point
   * ==================================================================== */
  var MBTVExtract = {
    name: 'Cineplay',

    extract: function (tmdbId, mediaType, season, episode, callbacks) {
      var onStatus = callbacks.onStatus || function () { };
      var onSource = callbacks.onSource || function () { };
      var onDone = callbacks.onDone || function () { };
      var canceled = false;
      var seenUrls = {};
      var jwt = '';
      var sourceCount = 0;

      function finish(err) {
        if (!canceled) onDone(err || null);
      }

      onStatus('Looking up title…');

      resolveTmdbTitle(tmdbId, mediaType).then(function (info) {
        if (canceled) return;
        if (!info.title) throw new Error('Could not resolve title for TMDB id ' + tmdbId);
        onStatus('Searching Cineplay catalog…');
        return getJwt().then(function (token) {
          jwt = token;
          return bffSearchRetry(info.title, jwt, mediaType === 'tv' ? 2 : 1, 1, 20);
        }).then(function (items) {
          if (canceled) return;
          var best = null, bestScore = 0;
          items.forEach(function (it) {
            if (!it || !it.subjectId) return;
            var s = scoreMatch(info.title, { title: it.title || '', releaseDate: it.releaseDate || '' }, info.year);
            if (s > bestScore) { best = it; bestScore = s; }
          });
          if (!best || bestScore < 0.35) throw new Error('Title not available on Cineplay');
          onStatus('Found: ' + best.title + ' — checking languages…');
          var sid = String(best.subjectId);
          return dubInfo(sid, jwt).then(function (dubs) {
            if (canceled) return;
            var langs = dubs.length ? dubs : [{ subjectId: sid, lanName: 'Original' }];
            /* Movies must be queried without se/ep — sending se=1&ep=1 for a
               film makes play-info come back empty. */
            var isTv = String(mediaType) === 'tv';
            var se = (isTv && season != null && season !== '') ? parseInt(season, 10) : null;
            var ep = (isTv && episode != null && episode !== '') ? parseInt(episode, 10) : null;
            var finished = false;

            function emitDub(dub, di) {
              var dubSid = String(dub.subjectId || sid);
              var lanName = normLang(dub.lanName);
              return playInfoRetry(dubSid, jwt, se, ep).then(function (pData) {
                if (canceled) return;
                var playable = extractPlayable(pData);
                onStatus('Audio ' + (di + 1) + '/' + langs.length + ' (' + lanName + '): ' +
                  (playable ? playable.all.length + (playable.all.length === 1 ? ' stream found' : ' streams found')
                   : 'still looking…'));
                if (!playable) return;
                playable.all.forEach(function (res) {
                  if (!res.url || seenUrls[res.url]) return;
                  seenUrls[res.url] = 1;
                  sourceCount++;
                  onSource({
                    url: mediaProxy(res.url),
                    rawUrl: res.url,
                    quality: normRes(res.resolution),
                    language: lanName,
                    server: 'Cineplay' + (res.format !== 'mp4' ? ' (' + res.format.toUpperCase() + ')' : ''),
                    format: res.format
                  });
                });
              });
            }

            /* Sequential, not parallel — parallel bursts are what makes
               play-info come back empty and drop languages from the list.
               Small spacing between dubs keeps the per-IP throttle calm. */
            var chain = Promise.resolve();
            langs.forEach(function (dub, di) {
              chain = chain.then(function () {
                var wait = di === 0 ? 0 : new Promise(function (res) { setTimeout(res, 350); });
                /* Retry the whole dub once when the request itself drops —
                   a single failed fetch must not erase an audio language. */
                var attemptDub = function (t) {
                  return emitDub(dub, di).catch(function () {
                    if (t <= 1 || canceled) return;
                    return new Promise(function (res) { setTimeout(res, 1200); })
                      .then(function () { return attemptDub(t - 1); });
                  });
                };
                return wait.then(function () { return attemptDub(2); });
              }).catch(function () { });
            });
            chain.then(function () {
              if (!finished && !canceled) {
                finished = true;
                finish(sourceCount === 0 ? new Error('No playable Cineplay stream for this title') : null);
              }
            });
          });
        });
      }).catch(function (err) {
        finish(err instanceof Error ? err : new Error(String(err)));
      });

      return {
        cancel: function () { canceled = true; },
        /* onDone is deferred: player-core counts extractors done. Signal
           done once at least one source was emitted OR everything failed. */
        notifySuccess: function () { finish(null); }
      };
    },

    /* Self-test used by the smoke test page (must equal Python md5). */
    _md5hex: md5hex,
    _sign: signHeader,
    _guestToken: guestToken,
    /* Debug hooks for live API probing from the console. */
    _debug: {
      relayRequest: relayRequest,
      getJwt: getJwt,
      bffSearch: bffSearch,
      dubInfo: dubInfo,
      playInfo: playInfo
    }
  };

  /* Emit success after a short grace period when sources were found —
     player-core's onDone just marks the extractor finished. */
  var origExtract = MBTVExtract.extract;
  MBTVExtract.extract = function (tmdbId, mediaType, season, episode, callbacks) {
    var handle = origExtract(tmdbId, mediaType, season, episode, callbacks);
    var origSource = callbacks.onSource || function () { };
    var doneSent = false;
    callbacks.onSource = function (src) {
      origSource(src);
      if (!doneSent) {
        doneSent = true;
        setTimeout(function () { handle.notifySuccess(); }, 400);
      }
    };
    return handle;
  };

  global.MBTVExtract = MBTVExtract;
})(window);
