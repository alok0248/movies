/* ===== Shared Player Core - Streaming ===== */
var _allSources = []; var _mainHls = null; var _audioHls = null;
var _audioEl = null; var _dualMode = false; var _playerPlaying = false; var _audioOffset = 0;

/* ===== Buffering Overlay ===== */
function _showBuf(vid) {
  var p = vid.parentNode; if (!p) return;
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

var _sourceQueue = []; var _srcIdx = 0; var _triedUrls = {};

function _makeProxyUrl(url) {
  if (!url) return url;
  try { var u = new URL(url); return location.origin + '/proxy/' + u.hostname + u.pathname + u.search; } catch(e) { return url; }
}

function _playHls(url, mediaEl) {
  if (!mediaEl || !url) return;
  _srcIdx = 0; _sourceQueue = []; _triedUrls = {};
  _allSources.forEach(function(s) { _sourceQueue.push(s); });
  if (_sourceQueue.length && _sourceQueue[0].url !== url) {
    for (var i = 0; i < _sourceQueue.length; i++) {
      if (_sourceQueue[i].url === url) { _sourceQueue.splice(i, 1); break; }
    }
    _sourceQueue.unshift({url: url, quality: '?', language: 'Original', server: 'first'});
  }
  _tryCurrentSource(mediaEl);
}

function _tryHlsDirect(url, mediaEl) {
  var h = new window.Hls({
    enableWorker: true, lowLatencyMode: false, maxBufferLength: 1800, maxMaxBufferLength: 7200,
    backBufferLength: 600, highBufferWatchdogPeriod: 0.1, nudgeOffset: 0.05, maxSeekHole: 120,
    fragLoadingTimeOut: 60000, manifestLoadingTimeOut: 20000, levelLoadingTimeOut: 30000,
    fragLoadingMaxRetry: 12, levelLoadingMaxRetry: 8, manifestLoadingMaxRetry: 8,
    startLevel: -1, capLevelToPlayerSize: false, stretchShortVideoTrack: true,
    maxAudioFramesDrift: 4, startFragPrefetch: true, maxBufferSize: 1073741824,
    maxBufferHole: 1.0, appendErrorMaxRetry: 10, debug: false
  });
  h.loadSource(url); h.attachMedia(mediaEl); return h;
}

function _tryCurrentSource(mediaEl) {
  if (_srcIdx >= _sourceQueue.length) {
    console.error('All sources exhausted');
    hidePlayerLoading();
    var wrap = mediaEl && mediaEl.closest ? mediaEl.closest('.player-video-wrap') : null;
    var container = wrap && wrap.parentNode ? wrap.parentNode : null;
    if (container) {
      _showPlayerMsg(container, {
        icon: 'fa-ban',
        title: 'All streams failed to play',
        detail: 'None of the returned sources could start. Try again, or try a different server / official embed.',
        retry: true
      });
    }
    return;
  }
  var s = _sourceQueue[_srcIdx];
  if (_triedUrls[s.url]) { _srcIdx++; _tryCurrentSource(mediaEl); return; }
  _triedUrls[s.url] = 1; console.log('PLAY: ' + s.server + ' ' + s.quality + ' ' + s.url.substring(0, 80));
  if (_mainHls) { try{_mainHls.destroy();}catch(e){} _mainHls = null; }
  /* Always route through browser SW proxy — no direct CDN, no server proxy */
  var playUrl = _makeProxyUrl(s.url);
  if (s.url.indexOf('.m3u8') > -1 && window.Hls && window.Hls.isSupported()) {
    var h = _tryHlsDirect(playUrl, mediaEl);
    h.on(window.Hls.Events.MANIFEST_PARSED, function() { mediaEl.play().catch(function(){}); hidePlayerLoading(); });
    h.on(window.Hls.Events.FRAG_BUFFERED, function() { if (!mediaEl.paused && mediaEl.currentTime > 0) mediaEl.play().catch(function(){}); });
    (function() {
      var _sc = null;
      mediaEl.addEventListener('waiting', function() { clearTimeout(_sc); _sc = setTimeout(function() { if (!mediaEl.paused && mediaEl.readyState < 3 && h) { try { h.startLoad(mediaEl.currentTime || 0); } catch(ex) {} mediaEl.play().catch(function(){}); } }, 5000); });
      mediaEl.addEventListener('playing', function() { clearTimeout(_sc); });
      mediaEl.addEventListener('stalled', function() { setTimeout(function() { if (!mediaEl.paused && h) try { h.startLoad(mediaEl.currentTime); } catch(ex) {} }, 3000); });
    })();
    h.on(window.Hls.Events.ERROR, function(evt, data) {
      if (!data.fatal) return;
      console.warn('[Player] Source failed, trying next:', data.type, data.details);
      h.destroy(); _mainHls = null;
      _advanceSource(mediaEl);
    });
    _mainHls = h;
  } else if (s.url.indexOf('.m3u8') > -1 && mediaEl.canPlayType('application/vnd.apple.mpegurl')) {
    /* Safari native HLS — still route through SW proxy */
    mediaEl.src = playUrl;
    mediaEl.addEventListener('loadedmetadata', function() { mediaEl.play().catch(function(){}); }, {once:true});
    mediaEl.addEventListener('error', function() { _advanceSource(mediaEl); }, {once:true});
  } else {
    /* Direct MP4 / other — route through SW proxy */
    mediaEl.src = playUrl; mediaEl.play().catch(function () {});
    mediaEl.addEventListener('error', function() { _advanceSource(mediaEl); }, {once:true});
  }
}
function _advanceSource(mediaEl) { _srcIdx++; _tryCurrentSource(mediaEl); }

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
  var proxyUrl = _makeProxyUrl(url);
  fetch(proxyUrl, {cache: 'no-store'}).then(function(r){return r.text();}).then(function(text){
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
    var h = new window.Hls({enableWorker:true,lowLatencyMode:false,maxBufferLength:900,maxMaxBufferLength:1800,backBufferLength:60,highBufferWatchdogPeriod:0.3,nudgeOffset:0.1,maxSeekHole:60,fragLoadingTimeOut:30000,levelLoadingTimeOut:15000,manifestLoadingTimeOut:15000,fragLoadingMaxRetry:8,levelLoadingMaxRetry:6,manifestLoadingMaxRetry:6,startLevel:-1,capLevelToPlayerSize:true,stretchShortVideoTrack:true,maxAudioFramesDrift:4,startFragPrefetch:true,maxBufferSize:209715200});
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
  _dualMode = !_dualMode;
  var b = document.getElementById('dualToggle'); if (b) b.classList.toggle('active', _dualMode);
  var box = document.getElementById('sourceSelector');
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

/* ===== Main Player Builder ===== */
function _buildVideasyPlayer(container, apiUrl, retryFn) {
  _allSources = []; _playerPlaying = false;
  _retryFn = typeof retryFn === 'function' ? retryFn : null;
  var vw = document.createElement('div'); vw.className = 'player-video-wrap'; vw.style.cssText = 'position:relative;width:100%;';
  var vid = document.createElement('video'); vid.id = 'mainVideo'; vid.controls = true; vid.autoplay = true; vid.playsInline = true;
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
  var sd = document.createElement('div'); sd.id = 'sourceSelector'; sd.className = 'src-selector'; vw.appendChild(sd);
  sd.addEventListener('click', function(e) { e.stopPropagation(); });
  sd.addEventListener('mouseenter', function() {});
  container.appendChild(vw);
  /* Collapse icon */
  var collapseSvg = '<svg viewBox="0 0 24 24"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/></svg>';
  var icon = document.createElement('div'); icon.className = 'player-collapse-icon'; icon.innerHTML = collapseSvg; vw.appendChild(icon);
  var _foldTimer = null, _iconHideTimer = null, _showIconTimer = null, _foldAnimTimer = null;
  var _state = 'hidden';
  function _setBarVisible(visible) {
    clearTimeout(_foldAnimTimer);
    if (visible) { sd.classList.remove('folded','folding'); sd.classList.add('open'); sd.style.display = 'flex'; sd.style.opacity = '1'; sd.style.transform = ''; }
    else { sd.classList.add('folding'); sd.classList.remove('open'); _foldAnimTimer = setTimeout(function() { sd.classList.remove('folding'); sd.classList.add('folded'); }, 350); }
  }
  function _setIconVisible(visible) { if (visible) icon.classList.add('show'); else icon.classList.remove('show'); }
  function _showIcon() { _state = 'icon'; _setIconVisible(true); }
  function _hideIcon() { _setIconVisible(false); if (_state === 'icon') _state = 'hidden'; }
  function _foldBar() { _setBarVisible(false); clearTimeout(_showIconTimer); _showIconTimer = setTimeout(function() { _showIcon(); }, 350); }
  function _openBar() { clearTimeout(_foldTimer); clearTimeout(_showIconTimer); _state = 'open'; _setIconVisible(false); _setBarVisible(true); _startAutoFold(); }
  function _startAutoFold() { clearTimeout(_foldTimer); _foldTimer = setTimeout(function() { var ddOpen = document.querySelector('.src-dd.open'); if (ddOpen) { _startAutoFold(); return; } _foldBar(); }, 5000); }
  icon.addEventListener('click', function(e) { e.stopPropagation(); if (_state !== 'open') _openBar(); });
  function _insideBar(el) { return el.closest('.src-selector') || el.closest('.player-collapse-icon') || el.closest('.dual-panel-inline'); }
  vw.addEventListener('click', function(e) { if (_insideBar(e.target)) return; if (_state === 'hidden') _showIcon(); });
  vw.addEventListener('mouseenter', function() { if (_state === 'hidden') _showIcon(); });
  vw.addEventListener('mouseleave', function() { if (_state === 'icon') { clearTimeout(_iconHideTimer); _iconHideTimer = setTimeout(function() { _hideIcon(); }, 1000); } });
  vw.addEventListener('touchstart', function(e) { if (_insideBar(e.target)) return; if (_state === 'hidden') _showIcon(); }, {passive: true});
  _state = 'hidden'; _setBarVisible(false);
  function _onFullscreenChange() {
    if (document.fullscreenElement || document.webkitFullscreenElement) { sd.style.fontSize = '0.85rem'; icon.style.width = '42px'; icon.style.height = '42px'; if (_state === 'hidden') _showIcon(); }
    else { sd.style.fontSize = ''; icon.style.width = ''; icon.style.height = ''; }
  }
  document.addEventListener('fullscreenchange', _onFullscreenChange);
  document.addEventListener('webkitfullscreenchange', _onFullscreenChange);
  var params = {};
  (apiUrl.split('?')[1] || '').split('&').forEach(function(p) { var kv = p.split('='); params[kv[0]] = decodeURIComponent(kv[1] || ''); });
  var tmdbId = params.tmdb_id || (typeof movieId !== 'undefined' ? movieId : '');
  if (!tmdbId) {
    _showPlayerMsg(container, {
      icon: 'fa-exclamation-triangle',
      title: 'Missing TMDB ID',
      detail: 'This title has no media ID, so no stream lookup can run.',
      retry: false
    });
    return;
  }
  _fetchFromServer(container, apiUrl, vid);
  vid.addEventListener('playing', function() { hidePlayerLoading(); }, {once:true});
  vid.addEventListener('canplay', function() { hidePlayerLoading(); }, {once:true});
  vid.addEventListener('error', function() { hidePlayerLoading(); }, {once:true});
}

var _retryFn = null;

/* ===== Honest player states: clear message instead of an endless spinner ===== */
function _showPlayerMsg(container, opts) {
  opts = opts || {};
  hidePlayerLoading();
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
