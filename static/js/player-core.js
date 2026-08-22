/* ===== Shared Player Core - Streaming ===== */
var _allSources = []; var _mainHls = null; var _audioHls = null;
var _audioEl = null; var _dualMode = false; var _playerPlaying = false;

function _proxyUrl(url) {
  if (!url) return url;
  if (url.indexOf('/proxy/') === -1) {
    return '/proxy/' + url.replace('https://', '').replace('http://', '');
  }
  return url;
}

/* ===== Buffering Overlay ===== */
function _showBuf(vid) {
  var p = vid.parentNode; if (!p) return;
  var el = document.getElementById('bufOverlay');
  if (!el) {
    el = document.createElement('div');
    el.id = 'bufOverlay';
    el.className = 'buf-overlay';
    el.innerHTML = '<div class="buf-ring"></div><div class="buf-text">Buffering<span class="player-loading-dots"><span>.</span><span>.</span><span>.</span></span></div><div class="buf-bar-wrap"><div class="buf-bar" id="bufBar"></div></div>';
    p.appendChild(el);
  }
  el.classList.add('show');
  // Update progress bar
  _bufInterval = setInterval(function() {
    var bar = document.getElementById('bufBar');
    if (!bar || vid.buffered.length === 0) return;
    var end = vid.buffered.end(vid.buffered.length - 1);
    var pct = vid.duration ? (end / vid.duration * 100) : 0;
    bar.style.width = Math.min(pct, 100) + '%';
  }, 300);
}
var _bufInterval = null;
function _hideBuf() {
  if (_bufInterval) { clearInterval(_bufInterval); _bufInterval = null; }
  var el = document.getElementById('bufOverlay');
  if (el) { el.classList.remove('show'); }
}
function _initBufEvents(vid) {
  vid.addEventListener('waiting', function() { _showBuf(vid); });
  vid.addEventListener('playing', function() { _hideBuf(); });
  vid.addEventListener('canplay', function() { _hideBuf(); });
  vid.addEventListener('seeking', function() { _showBuf(vid); });
  vid.addEventListener('seeked', function() { _hideBuf(); });
}

function _playHls(url, mediaEl) {
  if (!mediaEl || !url) return;
  if (_mainHls) { try{_mainHls.destroy();}catch(e){} _mainHls = null; }
  var proxied = _proxyUrl(url);
  if (url.indexOf('.m3u8') > -1 && window.Hls && window.Hls.isSupported()) {
    var h = new window.Hls({
      maxBufferLength: 60,
      maxMaxBufferLength: 120,
      xhrSetup: function(xhr, reqUrl) {
        /* Proxy ALL requests through our server */
        if (reqUrl && reqUrl.indexOf('/proxy/') === -1) {
          var newUrl = '/proxy/' + reqUrl.replace('https://', '').replace('http://', '');
          xhr.open('GET', newUrl, true);
        }
      },
      pLoader: function(config) {
        var loader = new window.Hls.DefaultConfig.loader(config);
        var originalLoad = loader.load.bind(loader);
        loader.load = function(config, callbacks, context) {
          /* Rewrite playlist URLs to proxy */
          if (config.url && config.url.indexOf('/proxy/') === -1) {
            config.url = '/proxy/' + config.url.replace('https://', '').replace('http://', '');
          }
          if (config.url && config.urlTransform) {
            config.url = config.urlTransform(config.url);
          }
          originalLoad(config, callbacks, context);
        };
        return loader;
      },
      fLoader: function(config) {
        var loader = new window.Hls.DefaultConfig.loader(config);
        var originalLoad = loader.load.bind(loader);
        loader.load = function(config, callbacks, context) {
          if (config.url && config.url.indexOf('/proxy/') === -1) {
            config.url = '/proxy/' + config.url.replace('https://', '').replace('http://', '');
          }
          originalLoad(config, callbacks, context);
        };
        return loader;
      }
    });
    h.loadSource(proxied); h.attachMedia(mediaEl);
    h.on(window.Hls.Events.MANIFEST_PARSED, function() { mediaEl.play().catch(function(){}); });
    h.on(window.Hls.Events.ERROR, function(evt, data) {
      if (data.fatal) {
        console.error('HLS fatal error:', data.type, data.details);
        h.destroy(); _mainHls = null;
        mediaEl.src = proxied;
        mediaEl.play().catch(function(){});
      }
    });
    _mainHls = h;
  } else if (url.indexOf('.m3u8') > -1 && mediaEl.canPlayType('application/vnd.apple.mpegurl')) {
    mediaEl.src = proxied;
    mediaEl.addEventListener('loadedmetadata', function() { mediaEl.play().catch(function(){}); }, {once:true});
  } else { mediaEl.src = url; mediaEl.play().catch(function(){}); }
}

function _addSource(s) { _allSources.push(s); _renderSourceList(); }

var _selQuality = null; var _selLanguage = null; var _selServer = null;

function _renderSourceList(){
  var box=document.getElementById('sourceSelector');
  if(!box||!_allSources.length)return;
  var seen={};var unique=[];
  _allSources.forEach(function(s,i){var k=(s.quality||'?')+'|'+(s.language||'')+'|'+(s.server||'');if(!seen[k]){seen[k]=1;unique.push({s:s,idx:i});}});
  var qMap={};unique.forEach(function(u){var q=u.s.quality||'?';if(!qMap[q])qMap[q]=[];qMap[q].push(u);});
  var lMap={};unique.forEach(function(u){var l=u.s.language||'Original';if(!lMap[l])lMap[l]=[];lMap[l].push(u);});
  var qualities=Object.keys(qMap).sort(function(a,b){var o={'2160p':0,'2160':0,'1080p':1,'1080':1,'720p':2,'720':2,'480p':3,'480':3,'360p':4,'360':4,'Auto':99,'Vimeos':98};return(o[a]||50)-(o[b]||50);});
  var languages=Object.keys(lMap).sort();
  if(!_selQuality&&qualities.length)_selQuality=qualities[0];
  if(!_selLanguage&&languages.length)_selLanguage=languages[0];
  var qh='<div class="src-dd" id="qDD"><span class="src-dd-label">Resolution</span>';
  qh+='<div class="src-dd-btn" data-toggle="qDD">'+(_selQuality||'Quality')+' <span class="dd-icon">&#9660;</span></div>';
  qh+='<div class="src-dd-menu">';
  qualities.forEach(function(q){var cls=q===_selQuality?' chosen':'';qh+='<div class="src-dd-opt'+cls+'" data-action="pickQ" data-value="'+q+'">'+q+'<span class="opt-count">'+qMap[q].length+'</span></div>';});
  qh+='</div></div>';
  var lh='<div class="src-dd" id="lDD"><span class="src-dd-label">Language</span>';
  lh+='<div class="src-dd-btn" data-toggle="lDD">'+(_selLanguage||'Language')+' <span class="dd-icon">&#9660;</span></div>';
  lh+='<div class="src-dd-menu">';
  languages.forEach(function(l){var cls=l===_selLanguage?' chosen':'';lh+='<div class="src-dd-opt'+cls+'" data-action="pickL" data-value="'+l+'">'+l+'<span class="opt-count">'+lMap[l].length+'</span></div>';});
  lh+='</div></div>';
  box.innerHTML=qh+lh+'<span class="src-active-info" id="srcInfo"></span>';
  box.classList.add('open');
  _updateSrcInfo();
}

document.addEventListener('click',function(e){
  var btn=e.target.closest('[data-toggle]');
  if(btn){var id=btn.getAttribute('data-toggle');var dd=document.getElementById(id);if(!dd)return;var wasOpen=dd.classList.contains('open');document.querySelectorAll('.src-dd').forEach(function(d){d.classList.remove('open');});if(!wasOpen)dd.classList.add('open');return;}
  var opt=e.target.closest('[data-action]');
  if(opt){var action=opt.getAttribute('data-action');var val=opt.getAttribute('data-value');document.querySelectorAll('.src-dd').forEach(function(d){d.classList.remove('open');});if(action==='pickQ'){_selQuality=val;_playSelected();_renderSourceList();}else if(action==='pickL'){_selLanguage=val;_playSelected();_renderSourceList();}return;}
  if(!e.target.closest('.src-dd'))document.querySelectorAll('.src-dd').forEach(function(d){d.classList.remove('open');});
});

function _playSelected() {
  var match = _findBestMatch();
  if (match) {
    var v = document.getElementById('mainVideo');
    if (v) _playHls(match.s.url, v);
  }
}

function _findBestMatch() {
  var seen = {}; var unique = [];
  _allSources.forEach(function(s, i) {
    var k = (s.quality||'?') + '|' + (s.language||'') + '|' + (s.server||'');
    if (!seen[k]) { seen[k]=1; unique.push({s:s,idx:i}); }
  });

  /* Find sources matching both quality and language */
  var matches = unique.filter(function(u) {
    var qMatch = !_selQuality || u.s.quality === _selQuality;
    var l = u.s.language || 'Original';
    var lMatch = !_selLanguage || l === _selLanguage;
    return qMatch && lMatch;
  });

  /* Fallback: match quality only */
  if (!matches.length) {
    matches = unique.filter(function(u) {
      return !_selQuality || u.s.quality === _selQuality;
    });
  }

  /* Fallback: first source */
  return matches[0] || unique[0] || null;
}

function _updateSrcInfo() {
  var info = document.getElementById('srcInfo');
  if (!info) return;
  var seen = {}; var unique = [];
  _allSources.forEach(function(s, i) {
    var k = (s.quality||'?') + '|' + (s.language||'') + '|' + (s.server||'');
    if (!seen[k]) { seen[k]=1; unique.push({s:s,idx:i}); }
  });
  info.textContent = unique.length + ' streams';
}

function selectSource(idx) {
  var s=_allSources[idx]; if(!s) return;
  _selQuality = s.quality || null;
  _selLanguage = s.language || 'Original';
  var v=document.getElementById('mainVideo'); if(v) _playHls(s.url,v);
}

function _buildDualPanel(sources) {
  var p=document.getElementById('dualPanel'); if(!p||!sources.length) return;
  var vids=sources.filter(function(s){return s.quality&&s.quality!=='Auto';});
  if(!vids.length) vids=sources; var auds=sources;
  var vo=vids.map(function(s,i){return '<option value="'+i+'"'+(i===0?' selected':'')+'>'+s.quality+' - '+s.server+'</option>';}).join('');
  var ao=auds.map(function(s,i){return '<option value="'+i+'"'+(i===Math.min(1,auds.length-1)?' selected':'')+'>'+(s.language||s.quality)+' - '+s.server+'</option>';}).join('');
  p.innerHTML='<div class="dual-row"><label>Video</label><select id="dualVidSel" onchange="switchDualVideo()">'+vo+'</select><input type="range" min="0" max="100" value="100" oninput="setDualVol(\'video\',this.value)"><span class="dual-vol" id="dualVidVol">100</span></div>'+'<div class="dual-row"><label>Audio</label><select id="dualAudSel" onchange="switchDualAudio()">'+ao+'</select><input type="range" min="0" max="100" value="100" oninput="setDualVol(\'audio\',this.value)"><span class="dual-vol" id="dualAudVol">100</span></div>';
  p.classList.add('open'); switchDualVideo(); switchDualAudio();
}

function switchDualVideo(){var s=document.getElementById('dualVidSel');if(!s)return;var i=parseInt(s.value);var d=_allSources[i];if(d){var v=document.getElementById('mainVideo');if(v)_playHls(d.url,v);}}
function switchDualAudio(){var s=document.getElementById('dualAudSel');if(!s)return;var i=parseInt(s.value);var d=_allSources[i];if(!d)return;if(_audioHls){try{_audioHls.destroy();}catch(e){}_audioHls=null;}if(!_audioEl){_audioEl=document.createElement('audio');_audioEl.id='dualAudio';_audioEl.crossOrigin='anonymous';document.body.appendChild(_audioEl);}if(d.url.indexOf('.m3u8')>-1&&window.Hls&&window.Hls.isSupported()){var h=new window.Hls({maxBufferLength:60,maxMaxBufferLength:120,
      xhrSetup:function(xhr,u){if(u&&u.indexOf('/proxy/')===-1){xhr.open('GET','/proxy/'+u.replace('https://','').replace('http://',''),true);}},
      fLoader:function(c){var l=new window.Hls.DefaultConfig.loader(c);var o=l.load.bind(l);l.load=function(cfg,cb,ctx){if(cfg.url&&cfg.url.indexOf('/proxy/')===-1){cfg.url='/proxy/'+cfg.url.replace('https://','').replace('http://','');}o(cfg,cb,ctx);};return l;}
    });var p2=_proxyUrl(d.url);h.loadSource(p2);h.attachMedia(_audioEl);h.on(window.Hls.Events.MANIFEST_PARSED,function(){_audioEl.play().catch(function(){});});_audioHls=h;}else{_audioEl.src=_proxyUrl(d.url);_audioEl.play().catch(function(){});}}
function setDualVol(t,v){v=parseInt(v)/100;if(t==='video'){var e=document.getElementById('mainVideo');if(e)e.volume=v;var el=document.getElementById('dualVidVol');if(el)el.textContent=Math.round(v*100);}else{if(_audioEl)_audioEl.volume=v;var el=document.getElementById('dualAudVol');if(el)el.textContent=Math.round(v*100);}}
function toggleDualMode(){_dualMode=!_dualMode;var b=document.getElementById('dualToggle');if(b)b.classList.toggle('active',_dualMode);var p=document.getElementById('dualPanel');if(p)p.classList.toggle('open',_dualMode);if(_dualMode){_buildDualPanel(_allSources);}else{if(_audioHls){try{_audioHls.destroy();}catch(e){}_audioHls=null;}if(_audioEl){_audioEl.pause();_audioEl.src='';}}}

function _buildVideasyPlayer(container, apiUrl) {
  _allSources=[]; _playerPlaying=false;
  /* Video wrapper to properly contain video + overlay controls */
  var vw=document.createElement('div'); vw.className='player-video-wrap'; vw.style.cssText='position:relative;width:100%;';
  var vid=document.createElement('video'); vid.id='mainVideo'; vid.controls=true; vid.autoplay=true; vid.playsInline=true;
  vid.style.cssText='width:100%;aspect-ratio:16/9;background:#000;display:block;border-radius:16px 16px 0 0;object-fit:contain;';
  vw.appendChild(vid);
  _initBufEvents(vid);
  var sd=document.createElement('div'); sd.id='sourceSelector'; sd.className='src-selector'; vw.appendChild(sd);
  var cr=document.createElement('div'); cr.className='player-controls-row';
  cr.innerHTML='<div class="ctrl-toggle" id="dualToggle" onclick="toggleDualMode()">Dual Stream</div>';
  vw.appendChild(cr);
  var dp=document.createElement('div'); dp.id='dualPanel'; dp.className='dual-panel'; vw.appendChild(dp);
  container.appendChild(vw);

  /* Parse tmdb_id, type, season, episode from apiUrl or global vars */
  var params = {};
  (apiUrl.split('?')[1] || '').split('&').forEach(function(p){var kv=p.split('=');params[kv[0]]=decodeURIComponent(kv[1]||'');});
  var tmdbId = params.tmdb_id || (typeof movieId !== 'undefined' ? movieId : '');
  var mediaType = params.type || 'movie';
  var season = params.season || '1';
  var episode = params.episode || '1';

  if (!tmdbId) {
    container.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#888;font-size:.9rem">Missing TMDB ID</div>';
    hidePlayerLoading();
    return;
  }

  /* Client-side extraction - runs entirely in browser, no server needed */
  if (typeof ClientExtract !== 'undefined') {
    ClientExtract.extractSources(tmdbId, mediaType, season, episode,
      function onSource(s) {
        _addSource(s);
        if (!_playerPlaying) {
          _playerPlaying = true;
          _playHls(s.url, vid);
          hidePlayerLoading();
        }
      },
      function onDone() {
        if (!_playerPlaying) {
          container.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#888;font-size:.9rem">No streams found</div>';
          hidePlayerLoading();
        }
      }
    ).catch(function(err) {
      console.error('Client extraction failed:', err);
      /* Fallback to server API */
      _fetchFromServer(container, apiUrl, vid);
    });
  } else {
    /* Fallback to server API */
    _fetchFromServer(container, apiUrl, vid);
  }

  vid.addEventListener('playing',function(){hidePlayerLoading();},{once:true});
  vid.addEventListener('error',function(){hidePlayerLoading();},{once:true});
  setTimeout(function(){hidePlayerLoading();},20000);
}

function _fetchFromServer(container, apiUrl, vid) {
  fetch(apiUrl).then(function(r){return r.json()}).then(function(d){
    if(d.success&&d.results&&d.results.length){
      d.results.forEach(function(s){_addSource(s);});
      _playerPlaying=true;
      _playHls(d.results[0].url,vid);
      hidePlayerLoading();
    }else{
      container.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#888;font-size:.9rem">No streams found</div>';
      hidePlayerLoading();
    }
  }).catch(function(err){
    console.error('Server fetch failed:',err);
    container.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#888;font-size:.9rem">Failed to load streams</div>';
    hidePlayerLoading();
  });
}
