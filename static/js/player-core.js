/* ===== Shared Player Core - Streaming ===== */
var _allSources = []; var _mainHls = null; var _audioHls = null;
var _allSubs = [];          /* subtitle tracks collected client-side */
var _curMedia = null;       /* {tmdbId, type, season, episode} for subtitle fallback */
var _audioEl = null; var _dualMode = false; var _playerPlaying = false; var _audioOffset = 0;
var _startedPlay = false; var _lastSwOk = false;

/* ===== Buffering Overlay ===== */
function _showBuf(vid) {
  var p = vid.parentNode; if (!p) return;
  /* Never paint a buffering overlay on top of an active error/status panel. */
  if (p.querySelector && p.querySelector('.player-msg-panel')) return;
  var el = document.getElementById('bufOverlay');
  if (!el) {
    el = document.createElement('div'); el.id = 'bufOverlay'; el.className = 'buf-overlay';
    el.innerHTML = '<div class="buf-ring"></div><div class="buf-text">Buffering<span class="player-loading-dots"><span>.</span><span>.</span><span>.</span></span></div><div class="buf-info" id="bufInfo"></div><div class="buf-bar-wrap"><div class="buf-bar" id="bufBar"></div></div>';
    p.appendChild(el);
  }
  el.classList.add('show');
  _bufInterval = setInterval(function() {
    var bar = document.getElementById('bufBar'); var info = document.getElementById('bufInfo');
    if (!bar || vid.buffered.length === 0) return;
    var end = vid.buffered.end(vid.buffered.length - 1);
    var pct = vid.duration ? (end / vid.duration * 100) : 0;
    bar.style.width = Math.min(pct, 100) + '%';
    if (info) { var bufAhead = Math.max(0, end - vid.currentTime); info.textContent = 'Buffered: ' + Math.round(pct) + '% | Ahead: ' + Math.round(bufAhead) + 's'; }
  }, 300);
}
var _bufInterval = null;
function _hideBuf() {
  if (_bufInterval) { clearInterval(_bufInterval); _bufInterval = null; }
  var el = document.getElementById('bufOverlay'); if (el) el.classList.remove('show');
}
function _initBufEvents(vid) {
  vid.addEventListener('waiting', function() { _showBuf(vid); });
  vid.addEventListener('playing', function() { _hideBuf(); });
  vid.addEventListener('canplay', function() { _hideBuf(); });
  vid.addEventListener('seeking', function() { _showBuf(vid); });
  vid.addEventListener('seeked', function() { _hideBuf(); });
  var barWrap = document.createElement('div');
  barWrap.style.cssText = 'position:absolute;bottom:0;left:0;right:0;height:3px;background:rgba(255,255,255,.15);z-index:7;pointer-events:none;border-radius:0 0 16px 16px;overflow:hidden;';
  var barFill = document.createElement('div');
  barFill.id = 'persistentBufFill';
  barFill.style.cssText = 'height:100%;width:0%;background:linear-gradient(90deg,#e50914,#ff4d4d);transition:width .5s ease;border-radius:2px;';
  barWrap.appendChild(barFill); vid.parentNode.appendChild(barWrap);
  setInterval(function() {
    var fill = document.getElementById('persistentBufFill');
    if (!fill || vid.buffered.length === 0 || !vid.duration) return;
    fill.style.width = Math.min((vid.buffered.end(vid.buffered.length - 1) / vid.duration) * 100, 100) + '%';
  }, 500);
}

/* ===== Attempt-based playback engine =====
 * Every returned source is played in the BEST possible order, all purely in the browser:
 *   1. DIRECT from the CDN — works for CORS-open HLS CDNs, and for any MP4/progressive
 *      file, because <video> needs no CORS headers at all.
 *   2. Through the BROWSER service worker ('/proxy/...', intercepted by sw-proxy.js) —
 *      still fetched by the browser itself. Proxy attempts are SKIPPED when the SW is
 *      not active, so a request can never fall through to the Django server.
 * If a URL is fully exhausted we move to the next returned source, and so on.
 */
var _sourceQueue = [];
var _attemptQueue = [];
var _attemptIdx = 0;
var _failedPlayUrls = {};
var _activeSrcUrl = '';
var _favHost = ''; /* CDN host that played successfully last — tried first next time */
var _hostFails = {}; /* CDN host -> consecutive fatal failures; >=3 and we skip it for this run */

function _makeProxyUrl(url) {
  if (!url) return url;
  try { var u = new URL(url); return location.origin + '/proxy/' + u.hostname + u.pathname + u.search; } catch(e) { return url; }
}

function _looksHls(url) { return !!(url && url.indexOf('.m3u8') > -1); }
function _hlsJsUsable() { return !!(window.Hls && window.Hls.isSupported()); }

/* True when sw-proxy.js is actually controlling this page right now. */
function _swActiveNow() {
  if (typeof ClientExtract !== 'undefined' && ClientExtract.swProxyActive) return ClientExtract.swProxyActive();
  if (!('serviceWorker' in navigator)) return false;
  var c = navigator.serviceWorker.controller;
  return !!(c && c.scriptURL && c.scriptURL.indexOf('/sw-proxy.js') !== -1);
}

/* Wait for the SW to claim this page (first-load race) — never blocks longer than ~1.2s. */
function _swReady(maxWait) {
  return new Promise(function(resolve) {
    if (_swActiveNow()) { resolve(true); return; }
    if (!('serviceWorker' in navigator)) { resolve(false); return; }
    var done = false;
    function finish(v) { if (!done) { done = true; resolve(v); } }
    var to = setTimeout(function() { finish(_swActiveNow()); }, maxWait || 1200);
    try {
      navigator.serviceWorker.ready.then(function() {
        setTimeout(function() { clearTimeout(to); finish(_swActiveNow()); }, 200);
      }).catch(function() { clearTimeout(to); finish(false); });
      navigator.serviceWorker.addEventListener('controllerchange', function h() {
        if (_swActiveNow()) { clearTimeout(to); finish(true); }
      });
    } catch (e) { clearTimeout(to); finish(false); }
  });
}

/* Ordered play attempts for one source URL. */
function _attemptsForUrl(url, swOk) {
  var out = [{ label: 'direct', url: url }];
  if (swOk) out.push({ label: 'browser-proxy', url: _makeProxyUrl(url) });
  return out;
}

function _cdnHostOf(url) {
  if (!url) return '';
  try {
    var u = new URL(url);
    if (u.pathname.indexOf('/proxy/') === 0) return u.pathname.split('/')[2] || '';
    return u.hostname;
  } catch (e) { return ''; }
}

/* Remember which CDN host actually delivered media, so the next play starts there. */
function _rememberWorking(it) {
  var host = _cdnHostOf(it ? it.playUrl : '');
  if (host && host.indexOf('.') > -1) _favHost = host;
}

/* Normalize a quality label to a comparable rank (lower = better). */
function _qRank(q) {
  var s = String(q || '').toLowerCase();
  var m = { '2160p':0,'2160':0,'4k':0,'uhd':0,'1080p':1,'1080':1,'fullhd':1,'fhd':1,'720p':2,'720':2,'hd':2,'480p':3,'480':3,'sd':3,'360p':4,'360':4,'240p':5,'240':5,'auto':99,'autohls':99,'auto hls':99,'adaptive':99,'hls':99 };
  if (Object.prototype.hasOwnProperty.call(m, s)) return m[s];
  if (s.indexOf('2160') > -1 || s.indexOf('4k') > -1) return 0;
  if (s.indexOf('1080') > -1) return 1;
  if (s.indexOf('720') > -1) return 2;
  if (s.indexOf('480') > -1) return 3;
  if (s.indexOf('360') > -1) return 4;
  if (s.indexOf('auto') > -1 || s.indexOf('adaptive') > -1 || s.indexOf('hls') > -1) return 99;
  return 50;
}

/* Deduplicated source list, with the chosen URL first when provided. */
function _orderedSources(preferUrl) {
  var seen = {}; var list = [];
  _allSources.forEach(function(s) {
    if (!s || !s.url || seen[s.url]) return;
    seen[s.url] = 1;
    list.push(s);
  });
  if (preferUrl && seen[preferUrl] && list[0].url !== preferUrl) {
    for (var i = 1; i < list.length; i++) {
      if (list[i].url === preferUrl) { var it = list.splice(i, 1)[0]; list.unshift(it); break; }
    }
  }
  /* Keep the user's choice first, then other sources at the SAME quality before
     any different quality — a dead 1080p server falls back to another 1080p,
     never back to the previous 720p/Auto stream. The remembered fast CDN is only
     a tiebreak inside each quality group. */
  function _favCmp(a, b) {
    var ah = _cdnHostOf(a.url), bh = _cdnHostOf(b.url);
    if (ah === _favHost && bh !== _favHost) return -1;
    if (bh === _favHost && ah !== _favHost) return 1;
    return 0;
  }
  if (list.length > 1) {
    var head = list[0];
    var q0 = head ? _qRank(head._quality) : null;
    var sameQ = [], other = [];
    list.slice(1).forEach(function(s) {
      if (q0 !== null && _qRank(s._quality) === q0) sameQ.push(s); else other.push(s);
    });
    sameQ.sort(_favCmp);
    other.sort(_favCmp);
    list = [head].concat(sameQ, other);
  }
  return list;
}

/* ---- Cheap manifest probes ------------------------------------------------
 * Before attaching hls.js to a URL we lightly fetch its master playlist. Dead
 * CDNs (403/404/502, CORS-blocked) fail in a second or two and get skipped, so
 * playback starts on a source that actually responds instead of grinding
 * through the whole list blindly. Probes run in the browser only. */
var _probeCache = {};
var _probeTtlMs = 20000;   /* re-probe a URL after 20s so a fresh pick can recover */

function _probeUrl(playUrl, timeoutMs) {
  var cached = _probeCache[playUrl];
  if (cached && (Date.now() - cached.t) < _probeTtlMs) {
    return Promise.resolve(cached.ok);
  }
  timeoutMs = timeoutMs || 5000;
  var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  var timer = ctrl ? setTimeout(function() { try { ctrl.abort(); } catch (e) {} }, timeoutMs) : null;
  var isProxy = playUrl.indexOf('/proxy/') === 0;
  var opts = {
    method: 'GET',
    cache: 'no-store',
    mode: isProxy ? 'same-origin' : 'cors',
    /* No Range — mirrors exactly what hls.js will do when it attaches, so a
       probe pass means the manifest really loads. */
    headers: { 'Accept': '*/*' }
  };
  if (ctrl) opts.signal = ctrl.signal;
  return fetch(playUrl, opts).then(function(r) {
    if (timer) clearTimeout(timer);
    var ok = r.ok && (r.status >= 200 && r.status < 300);
    _probeCache[playUrl] = { ok: ok, t: Date.now() };
    return ok;
  }, function() {
    if (timer) clearTimeout(timer);
    _probeCache[playUrl] = { ok: false, t: Date.now() };
    return false;
  });
}

/* When the user explicitly picks a source, forget any stale probe result for it
   (direct + proxy route) so the choice gets a fresh chance immediately. */
window._pcInvalidateProbe = function(url) {
  delete _probeCache[url];
  var p = _makeProxyUrl(url);
  if (p !== url) delete _probeCache[p];
};

function _buildPlainQueue(sources, swOk) {
  var queue = [];
  sources.forEach(function(s) {
    _attemptsForUrl(s.url, swOk).forEach(function(a) {
      queue.push({ src: s, playUrl: a.url, label: a.label });
    });
  });
  return queue;
}

/* Probe every HLS candidate (direct + browser proxy) in parallel, wait for all
 * of them (hard-capped so the user is never stuck), then order the attempt
 * queue so sources that actually respond play first. Everything still remains
 * in the queue afterwards as a fallback, so a wrong probe can never lose a
 * source. MP4/progressive files are never probed — <video> plays them without
 * CORS and they keep their original order.
 *
 * When the user EXPLICITLY picked a source (keepFirst), that source is always
 * attempted first regardless of its probe result — a probe false-negative must
 * never silently veto an explicit choice; hls.js itself gets the real chance. */
function _buildSmartQueue(sources, swOk, cb, keepFirst) {
  var hlsCands = [];
  sources.forEach(function(s) { if (_looksHls(s.url)) hlsCands.push(s); });
  var plain = _buildPlainQueue(sources, swOk);
  if (hlsCands.length <= 1 || typeof window.Hls === 'undefined') {
    cb(plain);
    return;
  }

  var jobs = [];
  hlsCands.forEach(function(s) {
    jobs.push({ src: s, url: s.url, label: 'direct' });
    if (swOk) jobs.push({ src: s, url: _makeProxyUrl(s.url), label: 'browser-proxy' });
  });

  var pass = {};       /* src.url -> {playUrl, label} (direct preferred) */
  var pending = jobs.length;
  var done = false;

  function finish() {
    if (done) return;
    done = true;
    var queue = [];
    var seen = {};
    var seenPlay = {};
    if (keepFirst && sources[0]) {
      _attemptsForUrl(sources[0].url, swOk).forEach(function(a) {
        if (!seenPlay[a.playUrl]) {
          seenPlay[a.playUrl] = 1;
          queue.push({ src: sources[0], playUrl: a.url, label: a.label });
        }
      });
      seen[sources[0].url] = 1;
    }
    hlsCands.forEach(function(s) { /* probe order is sources order, so favHost stays first */
      if (pass[s.url] && !seen[s.url]) {
        seen[s.url] = 1;
        queue.push({ src: s, playUrl: pass[s.url].playUrl, label: pass[s.url].label });
      }
    });
    plain.forEach(function(a) { if (!seen[a.src.url]) queue.push(a); });
    cb(queue);
  }

  var safety = setTimeout(finish, 6000); /* never stall playback on slow probes */
  jobs.forEach(function(job) {
    _probeUrl(job.url).then(function(ok) {
      pending--;
      if (ok && !pass[job.src.url]) {
        pass[job.src.url] = { playUrl: job.url, label: job.label };
      }
      if (pending <= 0) { clearTimeout(safety); finish(); }
    });
  });
  if (!jobs.length) finish();
}

/* Public entry: play a specific source URL (falls back through the rest of the list). */
var _explicitPick = false;
var _firstPickSrc = null;
var _downgradeToastShown = false;
function _playHls(url, mediaEl) {
  if (!mediaEl || !url) return;
  _activeSrcUrl = url;
  _failedPlayUrls = {};
  _hostFails = {};
  _explicitPick = !!window._pcUserPicked;
  window._pcUserPicked = false;
  var sources = _orderedSources(url);
  _swReady().then(function(swOk) {
    _lastSwOk = swOk;
    _attemptQueue = [];
    _buildSmartQueue(sources, swOk, function(queue) {
      _attemptQueue = queue;
      _attemptIdx = 0;
      _firstPickSrc = queue.length ? queue[0].src : null;
      _downgradeToastShown = false;
      _setActiveByUrl(url);
      _tryNextAttempt(mediaEl);
    }, _explicitPick);
  });
}

function _tryHlsDirect(url, mediaEl, onFatal, onReady) {
  var h = new window.Hls({
    enableWorker: true, lowLatencyMode: false, maxBufferLength: 1800, maxMaxBufferLength: 7200,
    backBufferLength: 600, highBufferWatchdogPeriod: 0.1, nudgeOffset: 0.05, maxSeekHole: 120,
    fragLoadingTimeOut: 30000, manifestLoadingTimeOut: 15000, levelLoadingTimeOut: 15000,
    fragLoadingMaxRetry: 5, levelLoadingMaxRetry: 2, manifestLoadingMaxRetry: 2,
    startLevel: -1, capLevelToPlayerSize: false, stretchShortVideoTrack: true,
    maxAudioFramesDrift: 4, startFragPrefetch: true, maxBufferSize: 1073741824,
    maxBufferHole: 1.0, appendErrorMaxRetry: 10, debug: false
  });
  h.on(window.Hls.Events.MANIFEST_PARSED, function() {
    if (typeof onReady === 'function') onReady();
    mediaEl.play().catch(function(){});
    hidePlayerLoading();
  });
  h.on(window.Hls.Events.FRAG_BUFFERED, function() {
    if (!mediaEl.paused && mediaEl.currentTime > 0) mediaEl.play().catch(function(){});
  });
  h.on(window.Hls.Events.ERROR, function(evt, data) {
    if (!data.fatal) return;
    console.warn('[Player] attempt failed:', data.type, data.details);
    try { h.destroy(); } catch (e) {}
    _mainHls = null;
    if (typeof onFatal === 'function') onFatal(data);
  });
  (function() {
    var _sc = null;
    mediaEl.addEventListener('waiting', function() { clearTimeout(_sc); _sc = setTimeout(function() { if (!mediaEl.paused && mediaEl.readyState < 3 && h) { try { h.startLoad(mediaEl.currentTime || 0); } catch(ex) {} mediaEl.play().catch(function(){}); } }, 5000); });
    mediaEl.addEventListener('playing', function() { clearTimeout(_sc); });
    mediaEl.addEventListener('stalled', function() { setTimeout(function() { if (!mediaEl.paused && h) try { h.startLoad(mediaEl.currentTime); } catch(ex) {} }, 3000); });
  })();
  h.loadSource(url);
  h.attachMedia(mediaEl);
  return h;
}

function _tryNextAttempt(mediaEl) {
  while (_attemptIdx < _attemptQueue.length) {
    var it = _attemptQueue[_attemptIdx];
    if (_failedPlayUrls[it.playUrl]) { _attemptIdx++; continue; }
    /* A host that already killed several routes is dead for this title — skip it fast. */
    var host = _cdnHostOf(it.src ? it.src.url : '');
    if (host && (_hostFails[host] || 0) >= 3) {
      console.log('[Player] skipping dead host ' + host + ' (' + (_hostFails[host] || 0) + ' fails)');
      _failedPlayUrls[it.playUrl] = 1;
      _attemptIdx++;
      continue;
    }
    _playOneAttempt(it, mediaEl);
    return;
  }
  _allStreamsExhausted(mediaEl);
}

function _playOneAttempt(it, mediaEl) {
  _failedPlayUrls[it.playUrl] = 1;
  var url = it.playUrl;
  var src = it.src || {};
  if (src.url) _activeSrcUrl = src.url;   /* keep the tracks UI in sync with the real attempt */
  /* The user picked a specific quality and we had to move off it — say so once. */
  if (_explicitPick && !_downgradeToastShown && _firstPickSrc && src.url !== _firstPickSrc.url) {
    var fq = _firstPickSrc._quality || '?', aq = src._quality || '?';
    if (_qRank(fq) !== _qRank(aq)) {
      _downgradeToastShown = true;
      _showPickToast(fq + " isn't available right now — playing " +
        (String(aq).toLowerCase().indexOf('auto') > -1 ? 'Auto (best available)' : aq) + '.');
    }
  }
  var name = (src.server || 'Server') + (src.quality && src.quality !== '?' ? ' · ' + src.quality : '');
  console.log('[Player] trying: ' + name + ' [' + it.label + '] ' + url.substring(0, 90));
  if (_mainHls) { try { _mainHls.destroy(); } catch (e) {} _mainHls = null; }
  if (_audioEl) { try { _audioEl.pause(); } catch (e) {} }

  function advance() {
    if (_audioHls) { try { _audioHls.destroy(); } catch (e) {} _audioHls = null; }
    var failedHost = _cdnHostOf(it.src ? it.src.url : '');
    if (failedHost) {
      _hostFails[failedHost] = (_hostFails[failedHost] || 0) + 1;
    }
    _attemptIdx++;
    _tryNextAttempt(mediaEl);
  }

  /* HLS via hls.js (MSE). Direct first — CDNs that send CORS play instantly with no proxy. */
  if (_looksHls(url) && _hlsJsUsable()) {
    var h = _tryHlsDirect(url, mediaEl, advance, function() { _rememberWorking(it); });
    _mainHls = h;
    return;
  }

  /* HLS via native Safari playback. */
  if (_looksHls(url) && mediaEl.canPlayType && mediaEl.canPlayType('application/vnd.apple.mpegurl')) {
    mediaEl.addEventListener('loadedmetadata', function() { _rememberWorking(it); mediaEl.play().catch(function(){}); hidePlayerLoading(); }, { once: true });
    mediaEl.addEventListener('error', advance, { once: true });
    mediaEl.src = url;
    return;
  }

  /* MP4 / progressive / anything else — a <video> element plays cross-origin without CORS. */
  if (mediaEl.canPlayType && url.indexOf('.m3u8') > -1 && !_hlsJsUsable() &&
      !(mediaEl.canPlayType('application/vnd.apple.mpegurl'))) {
    /* HLS but no hls.js and no native support — can't play, skip. */
    advance();
    return;
  }
  mediaEl.addEventListener('error', advance, { once: true });
  mediaEl.addEventListener('canplay', function() { _rememberWorking(it); hidePlayerLoading(); }, { once: true });
  mediaEl.src = url;
  mediaEl.play().catch(function(){});
}

/* Small status toast for pick fallbacks. */
function _showPickToast(msg) {
  try {
    var prev = document.getElementById('pickToast');
    if (prev) prev.remove();
    var toast = document.createElement('div');
    toast.id = 'pickToast';
    toast.textContent = msg;
    toast.style.cssText = 'position:fixed;top:74px;left:50%;transform:translateX(-50%);z-index:99999;background:rgba(229,9,20,.92);color:#fff;padding:10px 18px;border-radius:999px;font-size:.8rem;font-weight:700;box-shadow:0 10px 34px rgba(0,0,0,.5);pointer-events:none;max-width:min(92vw,560px);text-align:center;';
    document.body.appendChild(toast);
    setTimeout(function() { toast.style.opacity = '0'; toast.style.transition = 'opacity .5s'; }, 3800);
    setTimeout(function() { toast.remove(); }, 4400);
  } catch (e) {}
}

function _allStreamsExhausted(mediaEl) {
  /* Sources that arrive while we are playing/falling back are appended, never lost. */
  var known = {};
  _attemptQueue.forEach(function(it) { if (it && it.src && it.src.url) known[it.src.url] = 1; });
  var more = _allSources.filter(function(s) { return s && s.url && !known[s.url]; });
  if (more.length) {
    console.log('[Player] more sources arrived after playback start: ' + more.length);
    _swReady().then(function(swOk) {
      _lastSwOk = swOk;
      more.forEach(function(s) {
        _attemptsForUrl(s.url, swOk).forEach(function(a) {
          _attemptQueue.push({ src: s, playUrl: a.url, label: a.label });
        });
      });
      _tryNextAttempt(mediaEl);
    });
    return;
  }
  console.error('All streams exhausted');
  hidePlayerLoading();
  var wrap = mediaEl && mediaEl.closest ? mediaEl.closest('.player-video-wrap') : null;
  var container = wrap && wrap.parentNode ? wrap.parentNode : null;
  if (!container) return;
  var tried = _attemptQueue.length;
  var hint = '';
  if (!_swActiveNow() && _looksHls((mediaEl.currentSrc || ''))) {
    hint = ' Your browser could not play any stream directly (they need a browser proxy, which needs a secure HTTPS connection). Try the browser version on HTTPS, or use Try Again.';
  }
  _showPlayerMsg(container, {
    icon: 'fa-ban',
    title: 'All streams failed to play',
    detail: 'Tried ' + tried + ' stream route' + (tried === 1 ? '' : 's') + ' and none could start.' + hint + ' Try again, or pick another stream from the list below.',
    retry: true
  });
}

function _advanceSource(mediaEl) {
  /* Legacy hook: skip the whole remaining attempt list of the current source. */
  var cur = _attemptQueue[_attemptIdx];
  var curUrl = cur ? cur.src.url : null;
  while (_attemptIdx < _attemptQueue.length) {
    var it = _attemptQueue[_attemptIdx];
    if (curUrl && it.src.url !== curUrl) break;
    _failedPlayUrls[it.playUrl] = 1;
    _attemptIdx++;
  }
  _tryNextAttempt(mediaEl);
}

function _setActiveByUrl(url) {
  for (var i = 0; i < _uniqueSources.length; i++) {
    if (_uniqueSources[i].s.url === url) { _activeSrcIdx = i; return; }
  }
}

function _openPopup() {
  var match = _findBestMatch();
  if (match) window.open(match.url, '_blank', 'width=1200,height=700,menubar=no,toolbar=no');
}

/* ===== Language detection from source metadata ===== */
var _knownLangs = {'hindi':1,'english':1,'tamil':1,'telugu':1,'kannada':1,'bengali':1,'malayalam':1,'marathi':1,'german':1,'spanish':1,'portuguese':1,'french':1,'japanese':1,'korean':1,'chinese':1,'hinglish':1,'thai':1,'indonesian':1,'turkish':1,'arabic':1,'russian':1,'polish':1,'italian':1,'dutch':1};

function _detectLangFromSource(s) {
  var fields = [s.language, s.audioLanguage, s.audio, s.title, s.label];
  for (var i = 0; i < fields.length; i++) {
    var f = (fields[i] || '').toLowerCase().trim();
    if (_knownLangs[f]) return fields[i].charAt(0).toUpperCase() + fields[i].slice(1);
  }
  /* Scan full JSON for known language names */
  var txt = JSON.stringify(s).toLowerCase();
  for (var k in _knownLangs) { if (txt.indexOf('"' + k + '"') > -1 || txt.indexOf(k + '\\') > -1) return k.charAt(0).toUpperCase() + k.slice(1); }
  return 'Original';
}

/* ===== Fetch HLS manifest to detect audio track languages ===== */
var _langCache = {}; /* url -> {langs:[], resolved:false} */
function _detectLangFromManifest(url, callback) {
  if (!url || url.indexOf('.m3u8') === -1) { callback([]); return; }
  if (_langCache[url]) { callback(_langCache[url].langs || []); return; }
  _langCache[url] = {langs:[], resolved:false};
  var tryUrls = [url];
  if (_swActiveNow()) tryUrls.push(_makeProxyUrl(url));
  var bodyP = tryUrls.reduce(function(chain, u) {
    return chain.catch(function() {
      return fetch(u, {cache: 'no-store', mode: (u === url ? 'cors' : 'same-origin')}).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      });
    });
  }, Promise.reject(new Error('start')));
  bodyP.then(function(text){
    var langs = [];
    /* Parse #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="...",NAME="...",LANGUAGE="..." */
    var re = /#EXT-X-MEDIA:[^\n]*/gi; var m;
    while ((m = re.exec(text)) !== null) {
      var tag = m[0];
      var langMatch = tag.match(/LANGUAGE="([^"]+)"/i);
      var nameMatch = tag.match(/NAME="([^"]+)"/i);
      var autoMatch = tag.match(/DEFAULT=(YES|NO)/i);
      if (langMatch && langMatch[1] && langMatch[1].length > 1) {
        langs.push({code: langMatch[1], name: nameMatch ? nameMatch[1] : langMatch[1], isDefault: autoMatch ? autoMatch[1]==='YES' : false});
      }
    }
    /* Deduplicate */
    var seen = {}; var unique = [];
    langs.forEach(function(l) { if (!seen[l.code]) { seen[l.code] = 1; unique.push(l); } });
    _langCache[url] = {langs:unique, resolved:true};
    callback(unique);
  }).catch(function() { _langCache[url] = {langs:[], resolved:true}; callback([]); });
}


/* ===== Source management ===== */
function _addSource(s) {
  /* Ignore duplicates of a URL we already track (many servers return the same master). */
  for (var i = 0; i < _allSources.length; i++) {
    if (_allSources[i].url === s.url) return;
  }
  s._lang = _detectLangFromSource(s);
  s._quality = s.quality || '?';
  s._server = s.server || s.server_name || '?';
  _allSources.push(s);
  _renderSourceList();
  /* Asynchronously detect actual audio tracks from manifest */
  _detectLangFromManifest(s.url, function(detectedLangs) {
    if (detectedLangs.length > 0) {
      /* Update source with detected languages */
      s._detectedLangs = detectedLangs;
      /* Pick the most specific language */
      var nonAuto = detectedLangs.filter(function(l) { return l.code !== 'und' && l.code !== 'zxx'; });
      if (nonAuto.length === 1) s._lang = nonAuto[0].name || nonAuto[0].code;
      else if (nonAuto.length > 1) s._lang = nonAuto.map(function(l) { return l.name || l.code; }).join(' / ');
      else if (detectedLangs.length === 1) s._lang = detectedLangs[0].name || detectedLangs[0].code;
      else s._lang = detectedLangs.length + ' audio track' + (detectedLangs.length > 1 ? 's' : '');
      _renderSourceList();
    }
  });
}

var _activeSrcIdx = 0; var _uniqueSources = [];

function _renderSourceList() {
  if (typeof _pcRefreshSources === 'function') _pcRefreshSources();
  var box = document.getElementById('sourceSelector');
  if (!box || !_allSources.length) return;
  /* Don't group — show EVERY source individually with server name, quality, language */
  var qo = {'2160p':0,'2160':0,'4k':0,'1080p':1,'1080':1,'720p':2,'720':2,'480p':3,'480':3,'360p':4,'360':4,'auto':99};
  var sorted = _allSources.map(function(s,i) { return {s:s, idx:i}; });
  sorted.sort(function(a,b) {
    var sa = a.s._server || ''; var sb = b.s._server || '';
    if (sa !== sb) return sa.localeCompare(sb);
    var qa = (a.s._quality||'').toLowerCase(); var qb = (b.s._quality||'').toLowerCase();
    return (qo[qa]||50) - (qo[qb]||50);
  });
  _uniqueSources = sorted;
  /* Build scrollable list */
  var html = '';
  sorted.forEach(function(item, i) {
    var s = item.s;
    var lang = s._lang || 'Original';
    var qual = s._quality || '?';
    var server = s._server || '?';
    var active = (i === _activeSrcIdx) ? ' active' : '';
    /* Build language tags — show detected audio tracks if available */
    var langTags = '';
    if (s._detectedLangs && s._detectedLangs.length > 1) {
      langTags = s._detectedLangs.map(function(l) {
        var label = l.name || l.code;
        if (label.length > 12) label = label.substring(0, 12) + '…';
        return '<span class="src-lang-detected">' + label + '</span>';
      }).join('');
    }
    html += '<div class="src-item' + active + '" data-src-idx="' + i + '" onclick="_pickStream(' + i + ')">';
    html += '<span class="src-lang">' + lang + '</span>';
    html += '<span class="src-q">' + qual + '</span>';
    html += '<span class="src-server">' + server + '</span>';
    if (langTags) html += '<span class="src-langs">' + langTags + '</span>';
    html += '</div>';
  });
  var popBtn = '<div class="ctrl-pop-btn" id="popBtn" onclick="_openPopup()" title="Open in popup player">&#9654; Pop</div>';
  var info = '<span class="src-active-info" id="srcInfo">' + sorted.length + ' stream' + (sorted.length !== 1 ? 's' : '') + '</span>';
  box.innerHTML = '<div class="ctrl-toggle" id="dualToggle" onclick="toggleDualMode()">Dual Stream</div>' +
    '<div class="src-list">' + html + '</div>' + popBtn + info;
  box.classList.add('open');
  _updateSrcInfo();
}

function _pickStream(idx) {
  if (!_uniqueSources[idx]) return;
  _activeSrcIdx = idx;
  var s = _uniqueSources[idx].s;
  var v = document.getElementById('mainVideo');
  if (v) _playHls(s.url, v);
  _renderSourceList();
}

function _playSelected() { _pickStream(_activeSrcIdx); }

function _findBestMatch() {
  if (_uniqueSources[_activeSrcIdx]) return _uniqueSources[_activeSrcIdx];
  return _uniqueSources[0] || null;
}

function _updateSrcInfo() {
  var info = document.getElementById('srcInfo');
  if (!info) return;
  info.textContent = _uniqueSources.length + ' stream' + (_uniqueSources.length !== 1 ? 's' : '');
}

function selectSource(idx) {
  var s = _allSources[idx]; if (!s) return;
  var v = document.getElementById('mainVideo'); if (v) _playHls(s.url, v);
}

/* ===== Dual Audio Mode ===== */
function _buildDualInline() {
  var box = document.getElementById('sourceSelector');
  if (!box) return;
  var existing = document.getElementById('dualInline'); if (existing) existing.remove();
  if (!_allSources.length) return;
  var vids = _allSources; var auds = _allSources;
  var vo = vids.map(function(s,i) { return '<option value="'+i+'"'+(i===0?' selected':'')+'>'+(s._server||'?')+' - '+(s._quality||'?')+'</option>'; }).join('');
  var ao = auds.map(function(s,i) { return '<option value="'+i+'"'+(i===Math.min(1,auds.length-1)?' selected':'')+'>'+(s._server||'?')+' - '+(s._lang||s._quality||'?')+'</option>'; }).join('');
  var div = document.createElement('div'); div.id = 'dualInline'; div.className = 'src-dd';
  div.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:nowrap;overflow-x:auto;flex:1;';
  div.innerHTML = '<div class="dual-row" style="margin:0;display:flex;align-items:center;gap:.4rem;flex:1"><label style="font-size:.6rem;color:rgba(255,255,255,.7);width:auto;flex-shrink:0">Video</label><select id="dualVidSel" onchange="switchDualVideo()" style="flex:1;padding:5px 8px;border-radius:6px;background:rgba(30,30,50,.95);border:1px solid rgba(255,255,255,.18);color:rgba(255,255,255,.95);font-size:.7rem;font-family:inherit;appearance:none;cursor:pointer;min-width:0">'+vo+'</select><input type="range" min="0" max="100" value="100" oninput="setDualVol(\'video\',this.value)" style="width:60px;accent-color:#46d369;height:3px"><span class="dual-vol" id="dualVidVol" style="font-size:.6rem;color:rgba(255,255,255,.5);width:24px;text-align:center">100</span></div>'
    + '<div class="dual-row" style="margin:0;display:flex;align-items:center;gap:.4rem;flex:1"><label style="font-size:.6rem;color:rgba(255,255,255,.7);width:auto;flex-shrink:0">Audio</label><select id="dualAudSel" onchange="switchDualAudio()" style="flex:1;padding:5px 8px;border-radius:6px;background:rgba(30,30,50,.95);border:1px solid rgba(255,255,255,.18);color:rgba(255,255,255,.95);font-size:.7rem;font-family:inherit;appearance:none;cursor:pointer;min-width:0">'+ao+'</select><input type="range" min="0" max="100" value="100" oninput="setDualVol(\'audio\',this.value)" style="width:60px;accent-color:#46d369;height:3px"><span class="dual-vol" id="dualAudVol" style="font-size:.6rem;color:rgba(255,255,255,.5);width:24px;text-align:center">100</span></div>'
    + '<div style="display:flex;align-items:center;gap:.3rem;flex-shrink:0"><button class="dual-sync-btn" onclick="adjustSync(-0.5)" style="padding:3px 8px;border-radius:6px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.08);color:#fff;font-size:.68rem;cursor:pointer">−.5s</button><span style="font-size:.6rem;color:rgba(255,255,255,.5);width:32px;text-align:center" id="syncOffset">0.0s</span><button class="dual-sync-btn" onclick="adjustSync(0.5)" style="padding:3px 8px;border-radius:6px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.08);color:#fff;font-size:.68rem;cursor:pointer">+.5s</button></div>';
  box.appendChild(div);
  switchDualVideo(); switchDualAudio();
}

function switchDualVideo() {
  var s = document.getElementById('dualVidSel'); if (!s) return;
  var i = parseInt(s.value); var d = _allSources[i];
  if (d) { var v = document.getElementById('mainVideo'); if (v) { _playHls(d.url, v); v.addEventListener('loadedmetadata', function() { if (_audioEl && _dualMode) { _audioEl.currentTime = v.currentTime; _audioEl.play().catch(function(){}); } }, {once:true}); } }
}
var _syncOnceOpts = {once:true};
function switchDualAudio() {
  var s = document.getElementById('dualAudSel'); if (!s) return;
  var i = parseInt(s.value); var d = _allSources[i]; if (!d) return;
  if (_audioHls) { try{_audioHls.destroy();}catch(e){} _audioHls = null; }
  if (!_audioEl) { _audioEl = document.createElement('audio'); _audioEl.id = 'dualAudio'; _audioEl.muted = false; document.body.appendChild(_audioEl); }
  _audioEl.muted = false; _audioEl.volume = 1;
  if (d.url.indexOf('.m3u8') > -1 && window.Hls && window.Hls.isSupported()) {
    var h = new window.Hls({enableWorker:true,lowLatencyMode:false,maxBufferLength:900,maxMaxBufferLength:1800,backBufferLength:60,highBufferWatchdogPeriod:0.3,nudgeOffset:0.1,maxSeekHole:60,fragLoadingTimeOut:30000,levelLoadingTimeOut:15000,manifestLoadingTimeOut:15000,fragLoadingMaxRetry:5,levelLoadingMaxRetry:2,manifestLoadingMaxRetry:2,startLevel:-1,capLevelToPlayerSize:true,stretchShortVideoTrack:true,maxAudioFramesDrift:4,startFragPrefetch:true,maxBufferSize:209715200});
    h.loadSource(d.url); h.attachMedia(_audioEl);
    h.on(window.Hls.Events.MANIFEST_PARSED, function() { var v = document.getElementById('mainVideo'); if (v) { _audioEl.currentTime = v.currentTime + _audioOffset; } _audioEl.play().catch(function(){}); });
    _audioHls = h;
  } else { _audioEl.src = d.url; _audioEl.play().catch(function(){}); }
}
function setDualVol(t, v) { v = parseInt(v) / 100; if (t === 'video') { var e = document.getElementById('mainVideo'); if (e) e.volume = v; var el = document.getElementById('dualVidVol'); if (el) el.textContent = Math.round(v * 100); } else { if (_audioEl) _audioEl.volume = v; var el = document.getElementById('dualAudVol'); if (el) el.textContent = Math.round(v * 100); } }
function adjustSync(delta) {
  _audioOffset += delta;
  var el = document.getElementById('syncOffset'); if (el) el.textContent = (_audioOffset > 0 ? '+' : '') + _audioOffset.toFixed(1) + 's';
  var v = document.getElementById('mainVideo'); if (v && _audioEl) _audioEl.currentTime = v.currentTime + _audioOffset;
}
var _syncInterval = null;
function _startSync() {
  if (_syncInterval) return;
  _syncInterval = setInterval(function() {
    if (!_dualMode || !_audioEl) return;
    var v = document.getElementById('mainVideo'); if (!v) return;
    var diff = v.currentTime - (_audioEl.currentTime - _audioOffset);
    if (Math.abs(diff) > 0.3) _audioEl.currentTime = v.currentTime + _audioOffset;
    if (v.paused && _audioEl && !_audioEl.paused) _audioEl.pause();
    else if (!v.paused && _audioEl && _audioEl.paused) { _audioEl.currentTime = v.currentTime + _audioOffset; _audioEl.play().catch(function(){}); }
    if (v.playbackRate !== _audioEl.playbackRate) _audioEl.playbackRate = v.playbackRate;
  }, 250);
}
function _stopSync() { clearInterval(_syncInterval); _syncInterval = null; }
function toggleDualMode() {
  var box = document.getElementById('sourceSelector');
  if (!box) return;
  _dualMode = !_dualMode;
  var b = document.getElementById('dualToggle'); if (b) b.classList.toggle('active', _dualMode);
  if (_dualMode) {
    var srcList = box.querySelector('.src-list'); if (srcList) srcList.style.display = 'none';
    _buildDualInline(); _startSync();
  } else {
    var srcList = box.querySelector('.src-list'); if (srcList) srcList.style.display = '';
    var dualInline = document.getElementById('dualInline'); if (dualInline) dualInline.remove();
    if (_audioHls) { try{_audioHls.destroy();}catch(e){} _audioHls = null; }
    if (_audioEl) { _audioEl.pause(); _audioEl.src = ''; }
    _stopSync();
  }
}

/* ===== Client-side source extraction (browser only, no server) ===== */
function _fetchClientSources(container, params, vid) {
  if (typeof ClientExtract === 'undefined' || !ClientExtract.extractSources) {
    _showPlayerMsg(container, {
      icon: 'fa-unlink',
      title: 'Browser extractor unavailable',
      detail: 'The client-side stream extractor did not load. Hard-refresh the page (Ctrl/Cmd+Shift+R) and try again.',
      retry: true
    });
    return;
  }
  var tmdbId = params.tmdb_id || params.tmdbId;
  var mediaType = params.type || params.mediaType || params.media_type || 'movie';
  var season = params.season || params.season_id || '';
  var episode = params.episode || params.episode_id || '';
  _curMedia = { tmdbId: String(tmdbId || ''), type: mediaType, season: String(season || ''), episode: String(episode || '') };
  var startedPlay = false;
  console.log('[Player] Extracting sources in-browser:', mediaType, tmdbId, season ? 'S' + season : '', episode ? 'E' + episode : '');

  function setStatus(t) {
    var el = document.querySelector('.player-loading-subtext');
    if (el) el.textContent = t || '';
  }

  ClientExtract.extractSources(tmdbId, mediaType, season, episode, {
    onStatus: function(t) { setStatus(t); },
    onSource: function(src) {
      _addSource(src);
      /* Start playing the first usable stream right away; the rest keep arriving. */
      if (!startedPlay && !_startedPlay) {
        startedPlay = true;
        _startedPlay = true;
        _playerPlaying = true;
        hidePlayerLoading();
        _playHls(src.url, vid);
      }
    },
    onSubtitles: function(sub) {
      if (!sub || !sub.url) return;
      for (var si = 0; si < _allSubs.length; si++) {
        if (_allSubs[si].url === sub.url) return;
      }
      var code = String(sub.lang || sub.language || sub.lang_name || 'en').substring(0, 2);
      var name = sub.language || sub.lang_name || sub.lang || 'Subtitle';
      _allSubs.push({ url: sub.url, lang: code, lang_name: name, label: name, source: 'stream' });
      if (typeof _pcRefreshSources === 'function') _pcRefreshSources();
    },
    onDone: function(err) {
      setStatus('');
      if (_allSources.length) return;
      _showPlayerMsg(container, {
        icon: err ? 'fa-unlink' : 'fa-film',
        title: err ? 'Stream lookup failed' : 'No streams found for this title',
        detail: err
          ? String(err) + ' Streams are now searched straight from your browser (no server involved), so a network or CORS block on the source API can cause this. Try again, or use another server button / official embed below.'
          : 'No server returned a playable stream for this ' + (mediaType === 'tv' ? 'episode' : 'title') + '. Try again later, or use another server button / official embed below.',
        retry: true
      });
    }
  });
}

/* ===== Main Player Builder ===== */
function _buildVideasyPlayer(container, apiUrl, retryFn) {
  _allSources = []; _allSubs = []; _curMedia = null; _playerPlaying = false;
  _startedPlay = false; _lastSwOk = false;
  _retryFn = typeof retryFn === 'function' ? retryFn : null;
  var vw = document.createElement('div'); vw.className = 'player-video-wrap'; vw.style.cssText = 'position:relative;width:100%;';
  /* Native controls are OFF — the detail pages render their own control bar
     (nf-controls), so enabling the browser's controls stacked a second player UI. */
  var vid = document.createElement('video'); vid.id = 'mainVideo'; vid.controls = false; vid.autoplay = true; vid.playsInline = true;
  vid.style.cssText = 'width:100%;aspect-ratio:16/9;background:#000;display:block;border-radius:16px 16px 0 0;object-fit:contain;';
  vw.appendChild(vid); _initBufEvents(vid);
  function _syncAudioToVideo() {
    if (!_dualMode || !_audioEl) return;
    vid.addEventListener('pause', function() { if (_audioEl && !_audioEl.paused) _audioEl.pause(); });
    vid.addEventListener('play', function() { if (_audioEl && _audioEl.paused) { _audioEl.currentTime = vid.currentTime + _audioOffset; _audioEl.play().catch(function(){}); } });
    vid.addEventListener('seeking', function() { if (_audioEl) _audioEl.currentTime = vid.currentTime + _audioOffset; });
    vid.addEventListener('seeked', function() { if (_audioEl) _audioEl.currentTime = vid.currentTime + _audioOffset; });
    vid.addEventListener('ratechange', function() { if (_audioEl) _audioEl.playbackRate = vid.playbackRate; });
  }
  _syncAudioToVideo();
  container.appendChild(vw);
  if (typeof _pcInitUI === 'function') _pcInitUI(vw, vid);
  /* The old in-player source/resolution bar (#sourceSelector) has been removed —
     the Tracks sheet (tracks-ui.js) is the single Audio/Resolution/Dual selector. */
  var isClientMode = !!(apiUrl && typeof apiUrl === 'object');
  var params = {};
  if (isClientMode) {
    params = apiUrl;
  } else {
    (apiUrl.split('?')[1] || '').split('&').forEach(function(p) { var kv = p.split('='); params[kv[0]] = decodeURIComponent(kv[1] || ''); });
  }
  var tmdbId = params.tmdb_id || params.tmdbId || (typeof movieId !== 'undefined' ? movieId : '');
  if (!tmdbId) {
    _showPlayerMsg(container, {
      icon: 'fa-exclamation-triangle',
      title: 'Missing TMDB ID',
      detail: 'This title has no media ID, so no stream lookup can run.',
      retry: false
    });
    return;
  }
  if (isClientMode) {
    _fetchClientSources(container, params, vid);
  } else {
    _fetchFromServer(container, apiUrl, vid);
  }
  vid.addEventListener('playing', function() { hidePlayerLoading(); }, {once:true});
  vid.addEventListener('canplay', function() { hidePlayerLoading(); }, {once:true});
  vid.addEventListener('error', function() { hidePlayerLoading(); }, {once:true});
}

var _retryFn = null;

/* Stop everything that could still be playing/buffering underneath an error state. */
function _stopActivePlayback() {
  try {
    if (_mainHls) { _mainHls.destroy(); _mainHls = null; }
    if (_audioHls) { _audioHls.destroy(); _audioHls = null; }
    if (_audioEl) { try { _audioEl.pause(); _audioEl.removeAttribute('src'); } catch (e) {} }
    var v = document.getElementById('mainVideo');
    if (v) {
      try { v.pause(); v.removeAttribute('src'); v.load(); } catch (e) {}
    }
  } catch (e) {}
  _hideBuf();
}

/* ===== Honest player states: clear message instead of an endless spinner ===== */
function _showPlayerMsg(container, opts) {
  opts = opts || {};
  hidePlayerLoading();
  _stopActivePlayback();
  /* Make sure the series 'extracting' splash never lingers over an error state */
  try {
    var splash = document.getElementById('epSplash');
    if (splash) {
      splash.classList.remove('visible');
      splash.classList.add('hidden');
      splash.style.opacity = '0';
      splash.style.pointerEvents = 'none';
    }
  } catch(e) {}
  var wrap = container.querySelector('.player-video-wrap');
  var target = wrap || container;
  var old = target.querySelector('.player-msg-panel');
  if (old) old.remove();
  var panel = document.createElement('div');
  panel.className = 'player-msg-panel';
  panel.style.cssText = 'position:absolute;inset:0;z-index:8;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;padding:20px;background:rgba(8,9,13,.88);backdrop-filter:blur(4px);' +
    '-webkit-backdrop-filter:blur(4px);border-radius:16px;box-sizing:border-box;';
  var icon = opts.icon || 'fa-circle-info';
  var title = opts.title || 'Something went wrong';
  var detail = opts.detail || '';
  var html = '<i class="fas ' + icon + '" style="font-size:1.6rem;color:rgba(255,255,255,.45);"></i>';
  html += '<div style="font-size:.95rem;font-weight:700;color:rgba(255,255,255,.92);">' + title + '</div>';
  if (detail) html += '<div style="font-size:.78rem;color:rgba(255,255,255,.5);line-height:1.5;max-width:520px;">' + detail + '</div>';
  var showRetry = opts.retry !== false && _retryFn;
  if (showRetry) {
    html += '<button type="button" class="player-msg-retry" style="margin-top:6px;padding:9px 22px;border:none;border-radius:999px;background:linear-gradient(135deg,#e50914,#ff4d4d);color:#fff;font-size:.8rem;font-weight:700;cursor:pointer;font-family:inherit;box-shadow:0 6px 20px rgba(229,9,20,.35);">' +
      '<i class="fas fa-redo-alt" style="margin-right:6px;"></i>Try Again</button>';
  }
  panel.innerHTML = html;
  target.appendChild(panel);
  var btn = panel.querySelector('.player-msg-retry');
  if (btn) btn.addEventListener('click', function() {
    panel.remove();
    if (_retryFn) _retryFn();
  });
  return panel;
}

/* Page-level 'hide' helper used by templates may not exist on every page. */
if (typeof window.hidePlayerLoading !== 'function' && typeof hidePlayerLoading !== 'function') {
  window.hidePlayerLoading = function() {};
}

function _fetchFromServer(container, apiUrl, vid) {
  console.log('[Player] Fetching sources from:', apiUrl);
  var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
  var timer = null;
  if (controller) {
    timer = setTimeout(function() { try { controller.abort(); } catch(e) {} }, 30000);
  }
  var fetchOpts = { credentials: 'same-origin' };
  if (controller) fetchOpts.signal = controller.signal;

  fetch(apiUrl, fetchOpts).then(function(r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function(d) {
    clearTimeout(timer);
    console.log('[Player] Results:', d.results ? d.results.length : 0, 'success:', d.success);
    if (d.success && d.results && d.results.length) {
      d.results.forEach(function(s) { _addSource(s); });
      _playerPlaying = true;
      _playHls(d.results[0].url, vid);
      hidePlayerLoading();
    } else if (d.success === false) {
      var errText = d.error || 'The source API could not provide streams.';
      _showPlayerMsg(container, {
        icon: 'fa-server',
        title: 'Stream lookup failed',
        detail: 'The source lookup reported: ' + errText + ' If you configured another player (server buttons) or an official/YouTube embed under Players in the admin, try it instead.',
        retry: true
      });
    } else {
      _showPlayerMsg(container, {
        icon: 'fa-film',
        title: 'No streams found for this title',
        detail: 'The source servers returned nothing for this movie' + (apiUrl.indexOf('type=tv') !== -1 ? ' / episode' : '') + '. You can try again, or add an official/YouTube embed under Players in the admin dashboard.',
        retry: true
      });
    }
  }).catch(function(err) {
    clearTimeout(timer);
    console.error('[Player] Fetch failed:', err && err.message ? err.message : err);
    var isTimeout = !!(err && err.name === 'AbortError');
    _showPlayerMsg(container, {
      icon: isTimeout ? 'fa-hourglass-half' : 'fa-unlink',
      title: isTimeout ? 'Stream search timed out' : 'Could not reach the source service',
      detail: isTimeout
        ? 'The lookup took too long and was stopped so you are not stuck on a spinner. Check your server connection and try again.'
        : 'The request failed (' + (err && err.message ? err.message : 'network error') + '). Try again in a moment.',
      retry: true
    });
  });
}
