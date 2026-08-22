/* ===== Shared Player Core ===== */
/* Source selection, dual stream, volume controls */
var _allSources = [];
var _mainHls = null;
var _audioHls = null;
var _audioEl = null;
var _dualMode = false;

function _playHls(url, mediaEl) {
    if (!mediaEl || !url) return;
    if (_mainHls) { try{_mainHls.destroy();}catch(e){} _mainHls = null; }
    if (url.indexOf('.m3u8') > -1 && window.Hls && window.Hls.isSupported()) {
        var h = new window.Hls({maxBufferLength:60, maxMaxBufferLength:120});
        h.loadSource(url);
        h.attachMedia(mediaEl);
        h.on(window.Hls.Events.MANIFEST_PARSED, function() { mediaEl.play().catch(function(){}); });
        _mainHls = h;
    } else if (url.indexOf('.m3u8') > -1 && mediaEl.canPlayType('application/vnd.apple.mpegurl')) {
        mediaEl.src = url;
        mediaEl.addEventListener('loadedmetadata', function() { mediaEl.play().catch(function(){}); }, {once:true});
    } else {
        mediaEl.src = url;
        mediaEl.play().catch(function(){});
    }
}

function _buildSourceList(sources) {
    _allSources = sources;
    var box = document.getElementById('sourceSelector');
    if (!box || !sources.length) return;
    var html = '<div class="src-label">Available Streams (' + sources.length + ')</div><div class="src-list">';
    sources.forEach(function(s, i) {
        var lang = s.language || '';
        var lbl = '<span class="src-quality">' + (s.quality||'?') + '</span>';
        if (lang) lbl += ' <span class="src-lang">' + lang + '</span>';
        lbl += ' <span class="src-server">' + (s.server||'') + '</span>';
        html += '<div class="src-item' + (i===0?' active':'') + '" data-idx="' + i + '" onclick="selectSource(' + i + ')">' + lbl + '</div>';
    });
    html += '</div>';
    box.innerHTML = html;
    box.classList.add('open');
}

function selectSource(idx) {
    var s = _allSources[idx];
    if (!s) return;
    document.querySelectorAll('#sourceSelector .src-item').forEach(function(el) {
        el.classList.toggle('active', parseInt(el.getAttribute('data-idx')) === idx);
    });
    var vid = document.getElementById('mainVideo');
    if (vid) _playHls(s.url, vid);
}

function _buildDualPanel(sources) {
    var panel = document.getElementById('dualPanel');
    if (!panel || !sources.length) return;
    var vids = sources.filter(function(s){return s.quality && s.quality !== 'Auto';});
    if (!vids.length) vids = sources;
    var auds = sources;
    var vOpts = vids.map(function(s,i){return '<option value="' + i + '"' + (i===0?' selected':'') + '>' + (s.quality||'?') + ' - ' + (s.server||'') + '</option>';}).join('');
    var aOpts = auds.map(function(s,i){return '<option value="' + i + '"' + (i===Math.min(1,auds.length-1)?' selected':'') + '>' + (s.language||s.quality||'?') + ' - ' + (s.server||'') + '</option>';}).join('');
    panel.innerHTML = '<div class="dual-row"><label>Video</label><select id="dualVidSel" onchange="switchDualVideo()">' + vOpts + '</select><input type="range" min="0" max="100" value="100" oninput="setDualVol(\'video\',this.value)"><span class="dual-vol" id="dualVidVol">100</span></div>' +
        '<div class="dual-row"><label>Audio</label><select id="dualAudSel" onchange="switchDualAudio()">' + aOpts + '</select><input type="range" min="0" max="100" value="100" oninput="setDualVol(\'audio\',this.value)"><span class="dual-vol" id="dualAudVol">100</span></div>';
    panel.classList.add('open');
    switchDualVideo();
    switchDualAudio();
}

function switchDualVideo() {
    var sel = document.getElementById('dualVidSel');
    if (!sel) return;
    var idx = parseInt(sel.value);
    var s = _allSources[idx];
    if (s) { var vid = document.getElementById('mainVideo'); if (vid) _playHls(s.url, vid); }
}

function switchDualAudio() {
    var sel = document.getElementById('dualAudSel');
    if (!sel) return;
    var idx = parseInt(sel.value);
    var s = _allSources[idx];
    if (!s) return;
    if (_audioHls) { try{_audioHls.destroy();}catch(e){} _audioHls = null; }
    if (!_audioEl) {
        _audioEl = document.createElement('audio');
        _audioEl.id = 'dualAudio';
        _audioEl.crossOrigin = 'anonymous';
        document.body.appendChild(_audioEl);
    }
    if (s.url.indexOf('.m3u8') > -1 && window.Hls && window.Hls.isSupported()) {
        var h = new window.Hls({maxBufferLength:60, maxMaxBufferLength:120});
        h.loadSource(s.url);
        h.attachMedia(_audioEl);
        h.on(window.Hls.Events.MANIFEST_PARSED, function() { _audioEl.play().catch(function(){}); });
        _audioHls = h;
    } else {
        _audioEl.src = s.url;
        _audioEl.play().catch(function(){});
    }
}

function setDualVol(type, val) {
    val = parseInt(val) / 100;
    if (type === 'video') {
        var vid = document.getElementById('mainVideo'); if (vid) vid.volume = val;
        var el = document.getElementById('dualVidVol'); if (el) el.textContent = Math.round(val * 100);
    } else {
        if (_audioEl) _audioEl.volume = val;
        var el = document.getElementById('dualAudVol'); if (el) el.textContent = Math.round(val * 100);
    }
}

function toggleDualMode() {
    _dualMode = !_dualMode;
    var btn = document.getElementById('dualToggle');
    if (btn) btn.classList.toggle('active', _dualMode);
    var panel = document.getElementById('dualPanel');
    if (panel) panel.classList.toggle('open', _dualMode);
    if (_dualMode) {
        _buildDualPanel(_allSources);
    } else {
        if (_audioHls) { try{_audioHls.destroy();}catch(e){} _audioHls = null; }
        if (_audioEl) { _audioEl.pause(); _audioEl.src = ''; }
    }
}

function _buildVideasyPlayer(container, apiUrl) {
    fetch(apiUrl).then(function(r){return r.json()}).then(function(data) {
        if (!data.success || !data.results || !data.results.length) {
            container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#888;font-size:.9rem;">No streams found. Try another server.</div>';
            hidePlayerLoading();
            return;
        }
        var sources = data.results;
        var vid = document.createElement('video');
        vid.id = 'mainVideo';
        vid.controls = true;
        vid.autoplay = true;
        vid.playsInline = true;
        vid.style.cssText = 'width:100%;height:100%;background:#000;display:block;border-radius:16px 16px 0 0;';
        container.appendChild(vid);
        var srcDiv = document.createElement('div');
        srcDiv.id = 'sourceSelector';
        srcDiv.className = 'src-selector';
        container.appendChild(srcDiv);
        var ctrlRow = document.createElement('div');
        ctrlRow.className = 'player-controls-row';
        ctrlRow.innerHTML = '<div class="ctrl-toggle" id="dualToggle" onclick="toggleDualMode()">Dual Stream</div>';
        container.appendChild(ctrlRow);
        var dualDiv = document.createElement('div');
        dualDiv.id = 'dualPanel';
        dualDiv.className = 'dual-panel';
        container.appendChild(dualDiv);
        _playHls(sources[0].url, vid);
        _buildSourceList(sources);
        vid.addEventListener('playing', function() { hidePlayerLoading(); }, {once: true});
        vid.addEventListener('error', function() { hidePlayerLoading(); }, {once: true});
        setTimeout(function() { hidePlayerLoading(); }, 5000);
    }).catch(function(e) {
        hidePlayerLoading();
        container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#888;font-size:.9rem;">Failed to load streams. Try another server.</div>';
    });
}
