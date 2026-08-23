/* ===== Shared Player Core - Streaming ===== */
var _allSources = []; var _mainHls = null; var _audioHls = null;
var _audioEl = null; var _dualMode = false; var _playerPlaying = false; var _audioOffset = 0;

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

function _proxyUrl(u){if(!u)return u;return "/proxy/"+u.replace("https://","").replace("http://","");}
function _proxyUrl(u){if(!u)return u;return "/proxy/"+u.replace("https://","").replace("http://","");}
var _triedUrls={};

function _playHls(url, mediaEl) {
  if (!mediaEl || !url) return;
  _triedUrls={}; _triedUrls[url]=1;
  if (_mainHls) { try{_mainHls.destroy();}catch(e){} _mainHls = null; }
  _tryHlsUrl(url, mediaEl, false);
}

function _tryHlsUrl(url, mediaEl, useProxy) {
  if (!mediaEl || !url) return;
  if (_mainHls) { try{_mainHls.destroy();}catch(e){} _mainHls = null; }
  var loadUrl = useProxy ? _proxyUrl(url) : url;

  if (loadUrl.indexOf('.m3u8') > -1 && window.Hls && window.Hls.isSupported()) {
    var _origHost="";try{_origHost=new URL(url).origin;}catch(e){}
    function _resolveUrl(u){if(!u)return u;if(u.indexOf("http")===0)return u;if(_origHost)return _origHost+u;return u;}

    var h = new window.Hls({
      maxBufferLength: 300, maxMaxBufferLength: 600,
      xhrSetup: function(xhr, reqUrl) {
        var resolved = useProxy ? '/proxy/'+_resolveUrl(reqUrl).replace('https://','').replace('http://','') : _resolveUrl(reqUrl);
        xhr.open('GET', resolved, true);
      },
      pLoader: function(config) {
        var loader = new window.Hls.DefaultConfig.loader(config);
        var originalLoad = loader.load.bind(loader);
        loader.load = function(cfg, callbacks, context) {
          if (cfg.url) { cfg.url = _resolveUrl(cfg.url); if (useProxy && cfg.url.indexOf('/proxy/')===-1) cfg.url='/proxy/'+cfg.url.replace('https://','').replace('http://',''); }
          originalLoad(cfg, callbacks, context);
        };
        return loader;
      },
      fLoader: function(config) {
        var loader = new window.Hls.DefaultConfig.loader(config);
        var originalLoad = loader.load.bind(loader);
        loader.load = function(cfg, callbacks, context) {
          if (cfg.url) { cfg.url = _resolveUrl(cfg.url); if (useProxy && cfg.url.indexOf('/proxy/')===-1) cfg.url='/proxy/'+cfg.url.replace('https://','').replace('http://',''); }
          originalLoad(cfg, callbacks, context);
        };
        return loader;
      }
    });
    h.loadSource(loadUrl); h.attachMedia(mediaEl);
    h.on(window.Hls.Events.MANIFEST_PARSED, function() { mediaEl.play().catch(function(){}); hidePlayerLoading(); });
    h.on(window.Hls.Events.ERROR, function(evt, data) {
      if (data.fatal) {
        h.destroy(); _mainHls = null;
        if (!useProxy) { console.error('Direct failed, proxy:', url.substring(0,60)); _tryHlsUrl(url, mediaEl, true); }
        else { console.error('Proxy failed, next source'); _tryNextSource(mediaEl); }
      }
    });
    _mainHls = h;
  } else if (loadUrl.indexOf('.m3u8') > -1 && mediaEl.canPlayType('application/vnd.apple.mpegurl')) {
    mediaEl.src = loadUrl;
    mediaEl.addEventListener('loadedmetadata', function() { mediaEl.play().catch(function(){}); }, {once:true});
    mediaEl.addEventListener('error', function() { _tryNextSource(mediaEl); }, {once:true});
  } else {
    mediaEl.src = loadUrl;
    mediaEl.play().catch(function() {});
    mediaEl.addEventListener('error', function() { _tryNextSource(mediaEl); }, {once:true});
  }
}

function _tryNextSource(mediaEl) {
  var seen={};
  for (var i=0; i<_allSources.length; i++) {
    var s=_allSources[i]; var k=s.url;
    if (!_triedUrls[k] && !seen[k]) {
      _triedUrls[k]=1; seen[k]=1;
      console.log('Next source:', s.server, s.quality, s.url.substring(0,60));
      _tryHlsUrl(s.url, mediaEl, false);
      return;
    }
  }
  console.error('All sources exhausted');
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
  box.innerHTML='<div class="ctrl-toggle" id="dualToggle" onclick="toggleDualMode()">Dual Stream</div>'+qh+lh+'<span class="src-active-info" id="srcInfo"></span>';
  box.classList.add('open');
  _updateSrcInfo();
}

document.addEventListener('click',function(e){
  var btn=e.target.closest('[data-toggle]');
  if(btn){var id=btn.getAttribute('data-toggle');var dd=document.getElementById(id);if(!dd)return;var wasOpen=dd.classList.contains('open');document.querySelectorAll('.src-dd').forEach(function(d){if(d!==dd)d.classList.remove('open');});if(!wasOpen)dd.classList.add('open');else dd.classList.remove('open');_resetAutoFold();return;}
  var opt=e.target.closest('[data-action]');
  if(opt){var action=opt.getAttribute('data-action');var val=opt.getAttribute('data-value');document.querySelectorAll('.src-dd').forEach(function(d){d.classList.remove('open');});if(action==='pickQ'){_selQuality=val;_playSelected();_renderSourceList();}else if(action==='pickL'){_selLanguage=val;_playSelected();_renderSourceList();}_resetAutoFold();return;}
  /* Don't close dropdowns on outside click — only auto-fold timer closes them */
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

function _buildDualInline(){
  var box=document.getElementById('sourceSelector');
  var existing=document.getElementById('dualInline');
  if(existing) existing.remove();
  if(!_allSources.length) return;
  var vids=_allSources.filter(function(s){return s.quality&&s.quality!=='Auto';});
  if(!vids.length) vids=_allSources; var auds=_allSources;
  var vo=vids.map(function(s,i){return '<option value="'+i+'"'+(i===0?' selected':'')+'>'+s.quality+' - '+s.server+'</option>';}).join('');
  var ao=auds.map(function(s,i){return '<option value="'+i+'"'+(i===Math.min(1,auds.length-1)?' selected':'')+'>'+(s.language||s.quality)+' - '+s.server+'</option>';}).join('');
  var div=document.createElement('div');
  div.id='dualInline';
  div.className='src-dd';
  div.style.cssText='display:flex;gap:8px;align-items:center;flex-wrap:nowrap;overflow-x:auto;flex:1;';
  div.innerHTML='<div class="dual-row" style="margin:0;display:flex;align-items:center;gap:.4rem;flex:1"><label style="font-size:.6rem;color:rgba(255,255,255,.7);width:auto;flex-shrink:0">Video</label><select id="dualVidSel" onchange="switchDualVideo()" style="flex:1;padding:5px 8px;border-radius:6px;background:rgba(30,30,50,.95);border:1px solid rgba(255,255,255,.18);color:rgba(255,255,255,.95);font-size:.7rem;font-family:inherit;appearance:none;cursor:pointer;min-width:0">'+vo+'</select><input type="range" min="0" max="100" value="100" oninput="setDualVol(&apos;video&apos;,this.value)" style="width:60px;accent-color:#46d369;height:3px"><span class="dual-vol" id="dualVidVol" style="font-size:.6rem;color:rgba(255,255,255,.5);width:24px;text-align:center">100</span></div>'
    +'<div class="dual-row" style="margin:0;display:flex;align-items:center;gap:.4rem;flex:1"><label style="font-size:.6rem;color:rgba(255,255,255,.7);width:auto;flex-shrink:0">Audio</label><select id="dualAudSel" onchange="switchDualAudio()" style="flex:1;padding:5px 8px;border-radius:6px;background:rgba(30,30,50,.95);border:1px solid rgba(255,255,255,.18);color:rgba(255,255,255,.95);font-size:.7rem;font-family:inherit;appearance:none;cursor:pointer;min-width:0">'+ao+'</select><input type="range" min="0" max="100" value="100" oninput="setDualVol(&apos;audio&apos;,this.value)" style="width:60px;accent-color:#46d369;height:3px"><span class="dual-vol" id="dualAudVol" style="font-size:.6rem;color:rgba(255,255,255,.5);width:24px;text-align:center">100</span></div>'
    +'<div style="display:flex;align-items:center;gap:.3rem;flex-shrink:0"><button class="dual-sync-btn" onclick="adjustSync(-0.5)" style="padding:3px 8px;border-radius:6px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.08);color:#fff;font-size:.68rem;cursor:pointer">−.5s</button><span style="font-size:.6rem;color:rgba(255,255,255,.5);width:32px;text-align:center" id="syncOffset">0.0s</span><button class="dual-sync-btn" onclick="adjustSync(0.5)" style="padding:3px 8px;border-radius:6px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.08);color:#fff;font-size:.68rem;cursor:pointer">+.5s</button></div>';
  box.appendChild(div);
  switchDualVideo(); switchDualAudio();
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

function switchDualVideo(){
  var s=document.getElementById('dualVidSel');if(!s)return;var i=parseInt(s.value);var d=_allSources[i];
  if(d){var v=document.getElementById('mainVideo');if(v){_playHls(d.url,v);
    /* Sync audio to video position after switch */
    v.addEventListener('loadedmetadata',function(){
      if(_audioEl && _dualMode){_audioEl.currentTime=v.currentTime;_audioEl.play().catch(function(){});}
    },_syncOnceOpts);
  }}
}
var _syncOnceOpts={once:true};
function switchDualAudio(){
  var s=document.getElementById('dualAudSel');if(!s)return;var i=parseInt(s.value);var d=_allSources[i];if(!d)return;
  if(_audioHls){try{_audioHls.destroy();}catch(e){}_audioHls=null;}
  if(!_audioEl){_audioEl=document.createElement('audio');_audioEl.id='dualAudio';_audioEl.muted=false;document.body.appendChild(_audioEl);}
  _audioEl.muted=false; _audioEl.volume=1;
  if(d.url.indexOf('.m3u8')>-1&&window.Hls&&window.Hls.isSupported()){var h=new window.Hls({maxBufferLength:300,maxMaxBufferLength:600,
      xhrSetup:function(xhr,u){if(u&&u.indexOf('/proxy/')===-1){xhr.open('GET','/proxy/'+u.replace('https://','').replace('http://',''),true);}},
      fLoader:function(c){var l=new window.Hls.DefaultConfig.loader(c);var o=l.load.bind(l);l.load=function(cfg,cb,ctx){if(cfg.url&&cfg.url.indexOf('/proxy/')===-1){cfg.url='/proxy/'+cfg.url.replace('https://','').replace('http://','');}o(cfg,cb,ctx);};return l;}
    });h.loadSource((d.url));h.attachMedia(_audioEl);
    h.on(window.Hls.Events.MANIFEST_PARSED,function(){
      var v=document.getElementById('mainVideo');
      if(v){_audioEl.currentTime=v.currentTime+_audioOffset;}
      _audioEl.play().catch(function(){});
    });_audioHls=h;
  }else{_audioEl.src=(d.url);_audioEl.play().catch(function(){});}
}
function setDualVol(t,v){v=parseInt(v)/100;if(t==='video'){var e=document.getElementById('mainVideo');if(e)e.volume=v;var el=document.getElementById('dualVidVol');if(el)el.textContent=Math.round(v*100);}else{if(_audioEl)_audioEl.volume=v;var el=document.getElementById('dualAudVol');if(el)el.textContent=Math.round(v*100);}}

function adjustSync(delta){
  _audioOffset+=delta;
  var el=document.getElementById('syncOffset');
  if(el) el.textContent=(_audioOffset>0?'+':'')+_audioOffset.toFixed(1)+'s';
  var v=document.getElementById('mainVideo');
  if(v&&_audioEl){_audioEl.currentTime=v.currentTime+_audioOffset;}
}
/* Periodic sync: keep audio aligned with video */
var _syncInterval=null;
function _startSync(){
  if(_syncInterval) return;
  _syncInterval=setInterval(function(){
    if(!_dualMode||!_audioEl) return;
    var v=document.getElementById('mainVideo');
    if(!v) return;
    var diff=v.currentTime-(_audioEl.currentTime-_audioOffset);
    if(Math.abs(diff)>0.3){_audioEl.currentTime=v.currentTime+_audioOffset;}
    if(v.paused&&_audioEl&&!_audioEl.paused){_audioEl.pause();}
    else if(!v.paused&&_audioEl&&_audioEl.paused){_audioEl.currentTime=v.currentTime+_audioOffset;_audioEl.play().catch(function(){});}
    if(v.playbackRate!==_audioEl.playbackRate){_audioEl.playbackRate=v.playbackRate;}
  },250);
}
function _stopSync(){clearInterval(_syncInterval);_syncInterval=null;}
function toggleDualMode(){
  _dualMode=!_dualMode;
  var b=document.getElementById('dualToggle');
  if(b)b.classList.toggle('active',_dualMode);
  var box=document.getElementById('sourceSelector');
  /* Hide resolution/language, show video/audio selectors */
  var dds=box.querySelectorAll('.src-dd');
  var labels=box.querySelectorAll('.src-dd-label');
  var info=document.getElementById('srcInfo');
  var dualInline=document.getElementById('dualInline');
  if(_dualMode){
    dds.forEach(function(d){d.style.display='none';});
    labels.forEach(function(l){l.style.display='none';});
    if(info) info.style.display='none';
    _buildDualInline();
    _startSync();
  } else {
    dds.forEach(function(d){d.style.display='';});
    labels.forEach(function(l){l.style.display='';});
    if(info) info.style.display='';
    if(dualInline) dualInline.remove();
    if(_audioHls){try{_audioHls.destroy();}catch(e){}_audioHls=null;}
    if(_audioEl){_audioEl.pause();_audioEl.src='';}
    _stopSync();
  }
}

function _buildVideasyPlayer(container, apiUrl) {
  _allSources=[]; _playerPlaying=false;
  /* Video wrapper to properly contain video + overlay controls */
  var vw=document.createElement('div'); vw.className='player-video-wrap'; vw.style.cssText='position:relative;width:100%;';
  var vid=document.createElement('video'); vid.id='mainVideo'; vid.controls=true; vid.autoplay=true; vid.playsInline=true;
  vid.style.cssText='width:100%;aspect-ratio:16/9;background:#000;display:block;border-radius:16px 16px 0 0;object-fit:contain;';
  vw.appendChild(vid);
  _initBufEvents(vid);

  /* Sync audio to video events */
  function _syncAudioToVideo(){
    if(!_dualMode||!_audioEl) return;
    var v=vid;
    v.addEventListener('pause',function(){if(_audioEl&&!_audioEl.paused)_audioEl.pause();});
    v.addEventListener('play',function(){if(_audioEl&&_audioEl.paused){_audioEl.currentTime=v.currentTime+_audioOffset;_audioEl.play().catch(function(){});}});
    v.addEventListener('seeking',function(){if(_audioEl){_audioEl.currentTime=v.currentTime+_audioOffset;}});
    v.addEventListener('seeked',function(){if(_audioEl){_audioEl.currentTime=v.currentTime+_audioOffset;}});
    v.addEventListener('ratechange',function(){if(_audioEl){_audioEl.playbackRate=v.playbackRate;}});
  }
  _syncAudioToVideo();

  var sd=document.createElement('div'); sd.id='sourceSelector'; sd.className='src-selector'; vw.appendChild(sd);
  /* Stop clicks on controls bar from bubbling to player (which would close the bar)
     AND reset the auto-fold timer so the bar stays open while user interacts */
  sd.addEventListener('click',function(e){
    e.stopPropagation();
    if(_state==='open')_resetAutoFold();
    var btn=e.target.closest('[data-toggle]');
    if(btn){var id=btn.getAttribute('data-toggle');var dd=document.getElementById(id);if(!dd)return;var wasOpen=dd.classList.contains('open');document.querySelectorAll('.src-dd').forEach(function(d){if(d!==dd)d.classList.remove('open');});if(!wasOpen)dd.classList.add('open');else dd.classList.remove('open');return;}
    var opt=e.target.closest('[data-action]');
    if(opt){var action=opt.getAttribute('data-action');var val=opt.getAttribute('data-value');document.querySelectorAll('.src-dd').forEach(function(d){d.classList.remove('open');});if(action==='pickQ'){_selQuality=val;_playSelected();_renderSourceList();}else if(action==='pickL'){_selLanguage=val;_playSelected();_renderSourceList();}return;}
  });
  sd.addEventListener('mouseenter',function(){if(_state==='open')_resetAutoFold();});
  /* dual panel removed - using inline dual stream instead */
  container.appendChild(vw);

  /* === Collapse icon === */
  var collapseSvg='<svg viewBox="0 0 24 24"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/></svg>';
  var icon=document.createElement('div');
  icon.className='player-collapse-icon';
  icon.innerHTML=collapseSvg;
  vw.appendChild(icon);

  var _foldTimer=null, _iconHideTimer=null;
  var _state='hidden'; /* hidden | icon | open */
  var _foldAnimTimer=null;

  function _setBarVisible(visible){
    clearTimeout(_foldAnimTimer);
    if(visible){
      sd.classList.remove('folded','folding');
      sd.classList.add('open');
      sd.style.display='flex'; sd.style.opacity='1'; sd.style.transform='';
    } else {
      sd.classList.add('folding');
      sd.classList.remove('open');
      _foldAnimTimer=setTimeout(function(){
        sd.classList.remove('folding'); sd.classList.add('folded');
      },350);
    }
  }

  function _setIconVisible(visible){
    if(visible) icon.classList.add('show');
    else icon.classList.remove('show');
  }

  function _showIcon(){
    _state='icon';
    _setIconVisible(true);
  }

  function _hideIcon(){
    _setIconVisible(false);
    if(_state==='icon') _state='hidden';
  }

  function _foldBar(){
    _setBarVisible(false);
    clearTimeout(_showIconTimer);
    _showIconTimer=setTimeout(function(){_showIcon();},350);
  }

  var _showIconTimer=null;

  function _openBar(){
    clearTimeout(_foldTimer);
    clearTimeout(_showIconTimer);
    _state='open';
    _setIconVisible(false);
    _setBarVisible(true);
    _startAutoFold();
  }

  function _resetAutoFold(){
    _startAutoFold();
  }

  function _startAutoFold(){
    clearTimeout(_foldTimer);
    _foldTimer=setTimeout(function(){
      var ddOpen=document.querySelector('.src-dd.open');
      if(ddOpen){_startAutoFold();return;}
      _foldBar();
    },5000);
  }

  /* === Events === */
  /* Icon click: open bar (never close — only timer closes) */
  icon.addEventListener('click',function(e){e.stopPropagation();if(_state!=='open')_openBar();});

  function _insideBar(el){return el.closest('.src-selector')||el.closest('.player-collapse-icon')||el.closest('.dual-panel-inline');}
  vw.addEventListener('click',function(e){if(_insideBar(e.target))return;if(_state==='hidden')_showIcon();});

  /* Mouse enter: show icon if hidden */
  vw.addEventListener('mouseenter',function(){
    if(_state==='hidden') _showIcon();
  });

  /* Mouse leave: hide icon faster */
  vw.addEventListener('mouseleave',function(){
    if(_state==='icon'){
      clearTimeout(_iconHideTimer);
      _iconHideTimer=setTimeout(function(){_hideIcon();},1000);
    }
  });

  /* Touch support for mobile: single tap shows icon, tap again opens bar */
  vw.addEventListener('touchstart',function(e){
    if(_insideBar(e.target))return;
    if(_state==='hidden')_showIcon();
  },{passive:true});

  /* Start hidden */
  _state='hidden';
  _setBarVisible(false);

  /* === Fullscreen support === */
  function _onFullscreenChange(){
    if(document.fullscreenElement || document.webkitFullscreenElement){
      sd.style.fontSize='0.85rem';
      icon.style.width='42px';icon.style.height='42px';
      if(_state==='hidden') _showIcon();
    } else {
      sd.style.fontSize='';
      icon.style.width='';icon.style.height='';
    }
  }
  document.addEventListener('fullscreenchange',_onFullscreenChange);
  document.addEventListener('webkitfullscreenchange',_onFullscreenChange);

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
