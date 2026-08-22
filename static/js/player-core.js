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

function _renderSourceList() {
  var box = document.getElementById('sourceSelector');
  if (!box || !_allSources.length) return;
  var seen = {}; var unique = [];
  _allSources.forEach(function(s, i) {
    var k = (s.quality||'?') + '|' + (s.language||'') + '|' + (s.server||'');
    if (!seen[k]) { seen[k]=1; unique.push({s:s,idx:i}); }
  });
  var h = '<div class="src-label">Streams (' + unique.length + ')</div><div class="src-list">';
  unique.forEach(function(u) {
    var s=u.s, lang=s.language||'';
    var l='<span class="src-quality">'+(s.quality||'?')+'</span>';
    if(lang) l+=' <span class="src-lang">'+lang+'</span>';
    l+=' <span class="src-server">'+(s.server||'')+'</span>';
    var a=(u.idx===0)?' active':'';
    h+='<div class="src-item"'+a+' data-idx='+u.idx+' onclick="selectSource('+u.idx+')">'+l+'</div>';
  });
  h+='</div>'; box.innerHTML=h; box.classList.add('open');
}

function selectSource(idx) {
  var s=_allSources[idx]; if(!s) return;
  document.querySelectorAll('#sourceSelector .src-item').forEach(function(e){
    e.classList.toggle('active',parseInt(e.getAttribute('data-idx'))===idx);
  });
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
  var vid=document.createElement('video'); vid.id='mainVideo'; vid.controls=true; vid.autoplay=true; vid.playsInline=true;
  vid.style.cssText='width:100%;height:100%;background:#000;display:block;border-radius:16px 16px 0 0;';
  container.appendChild(vid);
  var sd=document.createElement('div'); sd.id='sourceSelector'; sd.className='src-selector'; container.appendChild(sd);
  var cr=document.createElement('div'); cr.className='player-controls-row';
  cr.innerHTML='<div class="ctrl-toggle" id="dualToggle" onclick="toggleDualMode()">Dual Stream</div>';
  container.appendChild(cr);
  var dp=document.createElement('div'); dp.id='dualPanel'; dp.className='dual-panel'; container.appendChild(dp);

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
