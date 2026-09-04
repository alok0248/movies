/* =====================================================================
   Professional Audio / Resolution / Dual-Stream controller (tracks UI)
   ---------------------------------------------------------------------
   Adds a "Tracks" pill over the in-page player (movie & series detail
   pages both use player-core.js). Clicking it opens a glass settings
   sheet with:
     1. Audio — every available audio language
     2. Resolution — every available quality
     3. Dual Stream — play one stream's video together with another
        stream's audio, with independent volume and sync controls.

   Everything runs client-side against the sources already collected by
   player-core.js. Depends on player-core.js being loaded first.
   ===================================================================== */
var _pcWrapEl = null, _pcVidEl = null;
var _pcChip = null, _pcChipBtn = null, _pcSheet = null, _pcBody = null;
var _pcOpen = false, _pcDual = false;
var _pcPrefLang = null, _pcPrefRes = null;
var _pcInjected = false;
var _pcVolBefore = null;       /* main video volume before dual took over */
var _pcDualVidUrl = '', _pcDualAudUrl = '';
var _pcSubUrl = null, _pcSubTrack = null, _pcSubOverlayEl = null, _pcSubBlobUrl = null;
var _pcSubFallback = false, _pcSubFetching = false, _pcSubGen = 0;
var _pcPrefSubUrl = null;    /* user's subtitle choice — persists across player rebuilds */
var _pcPrefSubMedia = null;  /* media key the subtitle was chosen for */
var _pcSubLoading = false;
function _pcMediaKey() {
  var m = (typeof _curMedia !== 'undefined' && _curMedia) ? _curMedia : null;
  if (!m || !m.tmdbId) return '';
  return m.type + ':' + m.tmdbId + ':' + (m.season || '') + ':' + (m.episode || '');
}

function _pcInjectStyle() {
  if (_pcInjected || !document.head) return;
  _pcInjected = true;
  var st = document.createElement('style');
  st.id = 'pcTracksStyle';
  st.textContent = [
    '.pc-tracks{position:absolute;top:10px;right:12px;z-index:30;display:flex;align-items:center;gap:6px;opacity:0;pointer-events:none;transform:translateY(-4px);transition:opacity .25s ease,transform .25s ease}',
    '.pc-tracks.show{opacity:1;pointer-events:auto;transform:translateY(0)}',
    '.pc-tracks-chip{display:inline-flex;align-items:center;gap:7px;max-width:220px;padding:6px 12px;border-radius:999px;background:rgba(10,10,18,.72);border:1px solid rgba(255,255,255,.16);color:#fff;font-size:.72rem;font-weight:600;cursor:pointer;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 6px 18px rgba(0,0,0,.35);transition:all .15s;user-select:none;white-space:nowrap}',
    '.pc-tracks-chip:hover{background:rgba(30,30,48,.88);border-color:var(--brand,#e50914)}',
    '.pc-tracks-chip svg{flex-shrink:0;opacity:.85}',
    '.pc-tracks-chip .pc-chip-q{color:#a78bfa;font-weight:700}',
    '.pc-tracks-chip .pc-chip-lang{color:#e67e22;font-weight:600;overflow:hidden;text-overflow:ellipsis;max-width:96px}',
    '.pc-tracks-chip .pc-chip-dual{color:#46d369}',
    '.pc-sheet{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) scale(.96);z-index:31;width:min(430px,calc(100% - 16px));max-height:86%;display:none;flex-direction:column;border-radius:16px;background:rgba(16,16,28,.94);border:1px solid rgba(255,255,255,.12);box-shadow:0 24px 80px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.06);backdrop-filter:blur(20px) saturate(1.3);-webkit-backdrop-filter:blur(20px) saturate(1.3);overflow:hidden;color:#fff}',
    '.pc-sheet.open{display:flex;animation:pcSheetIn .22s cubic-bezier(.2,.9,.3,1.2) forwards}',
    '@keyframes pcSheetIn{from{opacity:0;transform:translate(-50%,-50%) scale(.92)}to{opacity:1;transform:translate(-50%,-50%) scale(1)}}',
    '.pc-sheet-head{display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,.07);flex-shrink:0}',
    '.pc-sheet-head .pc-back{cursor:pointer;font-size:.8rem;color:rgba(255,255,255,.6);background:none;border:none;padding:4px 6px;border-radius:6px;transition:all .15s}',
    '.pc-sheet-head .pc-back:hover{color:#fff;background:rgba(255,255,255,.08)}',
    '.pc-sheet-head .pc-title{font-size:.8rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;flex:1}',
    '.pc-sheet-head .pc-close{cursor:pointer;color:rgba(255,255,255,.6);background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);width:26px;height:26px;border-radius:50%;font-size:.8rem;line-height:1;transition:all .15s}',
    '.pc-sheet-head .pc-close:hover{color:#fff;background:rgba(255,255,255,.14)}',
    '.pc-sheet-body{overflow-y:auto;padding:6px 12px 14px;scrollbar-width:thin}',
    '.pc-sec{display:flex;align-items:center;gap:6px;padding:12px 4px 6px;font-size:.62rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:rgba(255,255,255,.35)}',
    '.pc-sec .pc-sec-hint{margin-left:auto;font-weight:500;letter-spacing:0;text-transform:none;font-size:.62rem;color:rgba(255,255,255,.28)}',
    '.pc-row{display:flex;align-items:center;gap:10px;width:100%;padding:8px 10px;border-radius:9px;cursor:pointer;border:1px solid transparent;transition:all .12s;background:transparent;color:rgba(255,255,255,.75);font-size:.78rem;text-align:left}',
    '.pc-row:hover{background:rgba(255,255,255,.07);color:#fff}',
    '.pc-row.on{background:rgba(229,9,20,.14);border-color:rgba(229,9,20,.45);color:#fff}',
    '.pc-row .pc-check{width:16px;height:16px;border-radius:50%;border:2px solid rgba(255,255,255,.25);display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:.55rem;color:transparent;transition:all .12s}',
    '.pc-row.on .pc-check{border-color:var(--brand,#e50914);background:var(--brand,#e50914);color:#fff}',
    '.pc-row .pc-txt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.pc-row .pc-sub{font-size:.62rem;color:rgba(255,255,255,.35);flex-shrink:0;max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.pc-row .pc-badge{padding:2px 8px;border-radius:999px;font-size:.62rem;font-weight:700;flex-shrink:0;background:rgba(108,92,231,.18);color:#a78bfa}',
    '.pc-row .pc-lang-badge{padding:2px 8px;border-radius:999px;font-size:.62rem;font-weight:700;flex-shrink:0;background:rgba(230,126,34,.16);color:#e67e22}',
    '.pc-row .pc-srv{color:rgba(255,255,255,.3);font-size:.6rem;flex-shrink:0;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.pc-dual-row{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:9px;cursor:pointer;border:1px solid rgba(255,255,255,.09);background:rgba(255,255,255,.03);transition:all .12s}',
    '.pc-dual-row:hover{background:rgba(255,255,255,.07)}',
    '.pc-dual-row .pc-txt{flex:1;min-width:0}',
    '.pc-dual-row .pc-txt b{display:block;font-size:.78rem;font-weight:600;color:rgba(255,255,255,.85)}',
    '.pc-dual-row .pc-txt span{display:block;font-size:.64rem;color:rgba(255,255,255,.4);margin-top:1px}',
    '.pc-switch{position:relative;width:38px;height:21px;flex-shrink:0;border-radius:999px;background:rgba(255,255,255,.14);transition:background .2s;cursor:pointer}',
    '.pc-switch::after{content:"";position:absolute;top:2px;left:2px;width:17px;height:17px;border-radius:50%;background:#fff;transition:left .2s}',
    '.pc-switch.on{background:var(--brand,#e50914)}',
    '.pc-switch.on::after{left:19px}',
    '.pc-dual-controls{padding:4px 0 2px}',
    '.pc-dc{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:9px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);margin-bottom:7px}',
    '.pc-dc .pc-dc-label{font-size:.58rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:rgba(255,255,255,.4);width:38px;flex-shrink:0}',
    '.pc-dc .pc-dc-label.vid{color:#a78bfa}',
    '.pc-dc .pc-dc-label.aud{color:#e67e22}',
    '.pc-dc select{flex:1;min-width:0;padding:5px 8px;border-radius:7px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:#fff;font-size:.68rem;font-family:inherit;appearance:none;cursor:pointer}',
    '.pc-dc select:focus{outline:none;border-color:var(--brand,#e50914)}',
    '.pc-dc select option{background:#14141f;color:#fff}',
    '.pc-dc .pc-vol{width:56px;height:3px;accent-color:var(--brand,#e50914);flex-shrink:0}',
    '.pc-dc .pc-vol-val{font-size:.6rem;color:rgba(255,255,255,.5);width:26px;text-align:right;font-family:Consolas,monospace;flex-shrink:0}',
    '.pc-sync{display:flex;align-items:center;justify-content:center;gap:6px;padding:2px 0 4px}',
    '.pc-sync button{padding:3px 9px;border-radius:6px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);color:rgba(255,255,255,.75);font-size:.64rem;cursor:pointer;transition:all .12s}',
    '.pc-sync button:hover{background:rgba(255,255,255,.14);color:#fff}',
    '.pc-sync .pc-sync-val{font-size:.64rem;color:#81c784;font-family:Consolas,monospace;min-width:52px;text-align:center}',
    '.pc-sheet .pc-note{padding:10px 4px 2px;font-size:.62rem;color:rgba(255,255,255,.32);line-height:1.5}',
    '.pc-sheet-body::-webkit-scrollbar{width:6px}.pc-sheet-body::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:3px}',
    'html[data-theme="light"] .pc-tracks-chip{background:rgba(255,255,255,.85);border-color:rgba(0,0,0,.12);color:#1a1a2e}',
    'html[data-theme="light"] .pc-sheet{background:rgba(250,250,252,.96);border-color:rgba(0,0,0,.1);color:#1a1a2e}',
    'html[data-theme="light"] .pc-row{color:rgba(26,26,46,.75)}',
    'html[data-theme="light"] .pc-row:hover{background:rgba(0,0,0,.05);color:#111}',
    'html[data-theme="light"] .pc-sheet-head .pc-back{color:rgba(26,26,46,.6)}',
    'html[data-theme="light"] .pc-close{background:rgba(0,0,0,.05);border-color:rgba(0,0,0,.1);color:rgba(26,26,46,.6)}',
    'html[data-theme="light"] .pc-dc select{background:rgba(0,0,0,.05);border-color:rgba(0,0,0,.12);color:#1a1a2e}',
    'html[data-theme="light"] .pc-dc select option{background:#fff;color:#1a1a2e}',
    '.pc-row .pc-sub-badge{padding:2px 8px;border-radius:999px;font-size:.62rem;font-weight:700;flex-shrink:0;background:rgba(70,211,105,.14);color:#46d369}',
    '.pc-sub-overlay{position:absolute;left:7%;right:7%;bottom:15%;z-index:24;text-align:center;pointer-events:none;font-size:clamp(.78rem,2.3vw,1.2rem);line-height:1.5;color:#fff;text-shadow:0 2px 10px rgba(0,0,0,.95),0 0 3px rgba(0,0,0,.9);font-weight:500;display:none;white-space:pre-line;max-height:24%;overflow:hidden}'
  ].join('\n');
  document.head.appendChild(st);
}

function _pcLangOf(s) {
  return s && (s._lang || s.language || s.audioLanguage || s.audio || 'Original');
}
function _pcQOf(s) {
  return s ? (s._quality || s.quality || '?') : '?';
}
function _pcResNum(q) {
  var n = parseInt(q, 10) || 0;
  return n;
}
/* Canonical quality label for a source: when a server stuffed the LANGUAGE into
   the quality field ("Hindi", "English"…), that stream has no real resolution
   info — treat it as "Auto" so it never shows up as a fake resolution row. */
function _pcCanonRes(src) {
  var q = _pcQOf(src);
  if (!q || q === '?') return 'Auto';
  if (_pcLangEq(q, _pcLangOf(src))) return 'Auto';
  return q;
}
function _pcResRank(q) {
  var n = _pcResNum(q);
  if (n >= 2160) return 0;
  if (n >= 1080) return 1;
  if (n >= 720) return 2;
  if (n >= 480) return 3;
  return 4;
}
function _pcLangEq(a, b) {
  a = String(a || '').toLowerCase(); b = String(b || '').toLowerCase();
  return a === b || (a && b && (a.indexOf(b) > -1 || b.indexOf(a) > -1));
}
function _pcActiveSrc() {
  if (_activeSrcUrl) {
    for (var i = 0; i < _allSources.length; i++) {
      if (_allSources[i].url === _activeSrcUrl) return _allSources[i];
    }
  }
  if (_uniqueSources && _uniqueSources[_activeSrcIdx]) return _uniqueSources[_activeSrcIdx].s;
  return _allSources[0] || null;
}
function _pcSortedByBest(list) {
  return list.sort(function(a, b) {
    var r = _pcResRank(_pcQOf(a)) - _pcResRank(_pcQOf(b));
    if (r) return r;
    if (_favHost) {
      var ah = '', bh = '';
      try { ah = new URL(a.url).hostname; } catch (e) {}
      try { bh = new URL(b.url).hostname; } catch (e) {}
      if (ah === _favHost) return -1;
      if (bh === _favHost) return 1;
    }
    return String(a._server || '').localeCompare(String(b._server || ''));
  });
}
function _pcCandidates(prefLang, prefRes) {
  var out = [];
  for (var i = 0; i < _allSources.length; i++) {
    var s = _allSources[i];
    if (!s || !s.url) continue;
    if (prefLang && !_pcLangEq(_pcLangOf(s), prefLang)) continue;
    if (prefRes && !_pcResEq(_pcCanonRes(s), prefRes)) continue;
    out.push(s);
  }
  return _pcSortedByBest(out);
}

/* ===== Chip ===== */
function _pcChipLabel() {
  var dualTag = _pcDual ? '<span class="pc-chip-dual">DUAL</span>' : '';
  var a = _pcActiveSrc();
  if (!a) return '<span class="pc-chip-q">Tracks</span>';
  var q = _pcQOf(a), lang = _pcLangOf(a);
  var qTag = q && q !== '?' ? '<span class="pc-chip-q">' + q + '</span>' : '';
  var lTag = lang && lang !== 'Original' ? '&nbsp;·&nbsp;<span class="pc-chip-lang">' + lang + '</span>' : '';
  return dualTag + qTag + lTag;
}
function _pcShowChip(show) {
  if (!_pcChip) return;
  if (show) _pcChip.classList.add('show'); else _pcChip.classList.remove('show');
}
function _pcRefreshSources() {
  if (!_pcWrapEl) return;
  if (_allSources && _allSources.length) {
    if (_pcChipBtn) _pcChipBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="9" cy="6" r="2" fill="rgba(10,10,18,.7)"/><circle cx="15" cy="12" r="2" fill="rgba(10,10,18,.7)"/><circle cx="8" cy="18" r="2" fill="rgba(10,10,18,.7)"/></svg><span class="pc-chip-txt">' + _pcChipLabel() + '</span>';
    _pcShowChip(true);
  } else {
    _pcShowChip(false);
  }
  _pcReapplySub();
  if (_pcOpen) _pcRenderSheet();
}
/* If the user had a subtitle on and the player rebuilt (episode switch /
   failed-stream retry wipes the <video>), re-attach it once tracks return.
   Only auto-applies when the SAME media is being played, so switching to
   another episode/title never inherits the old file. */
function _pcReapplySub() {
  if (!_pcPrefSubUrl || _pcSubUrl || _pcSubLoading) return;
  var curKey = _pcMediaKey();
  if (_pcPrefSubMedia && curKey && curKey !== _pcPrefSubMedia) { _pcPrefSubUrl = null; _pcPrefSubMedia = null; return; }
  if (!curKey || !_allSubs || !_allSubs.length) return;   /* media not set yet — wait */
  for (var i = 0; i < _allSubs.length; i++) {
    if (_allSubs[i] && _allSubs[i].url === _pcPrefSubUrl) {
      _pcSetSub(_allSubs[i]);
      return;
    }
  }
}

/* ===== Sheet ===== */
function _pcInitUI(vw, vid) {
  _pcInjectStyle();
  _pcWrapEl = vw; _pcVidEl = vid;
  _pcTeardownSubs();   /* fresh player build — drop subtitle state from the previous one */

  if (!_pcChip) {
    _pcChip = document.createElement('div');
    _pcChip.className = 'pc-tracks';
    _pcChip.innerHTML = '<div class="pc-tracks-chip" title="Audio &amp; Resolution"><span class="pc-chip-txt">Tracks</span></div>';
    _pcChipBtn = _pcChip.querySelector('.pc-tracks-chip');
    _pcChip.addEventListener('click', function(e) {
      e.stopPropagation();
      if (_pcOpen) _pcClose(); else _pcOpenSheet();
    });
  }
  if (!_pcSheet) {
    _pcSheet = document.createElement('div');
    _pcSheet.className = 'pc-sheet';
    _pcSheet.innerHTML =
      '<div class="pc-sheet-head">' +
        '<button class="pc-back" id="pcBack" style="display:none">←</button>' +
        '<span class="pc-title" id="pcTitle">Audio &amp; Resolution</span>' +
        '<button class="pc-close" id="pcClose">✕</button>' +
      '</div>' +
      '<div class="pc-sheet-body" id="pcBody"></div>';
    var back = _pcSheet.querySelector('#pcBack');
    back.addEventListener('click', function(e) {
      e.stopPropagation();
      if (_pcDual) { _pcExitDual(); _pcRenderSheet(); }
    });
    _pcSheet.querySelector('#pcClose').addEventListener('click', function(e) {
      e.stopPropagation(); _pcClose();
    });
    _pcSheet.addEventListener('click', function(e) { e.stopPropagation(); });
    _pcBody = _pcSheet.querySelector('#pcBody');
  }
  /* The detail pages empty the player container and rebuild it on episode
     switch / stream retry — re-attach the (once-created) chip + sheet then.
     NOTE: after the old wrapper is removed from the DOM, _pcChip.parentNode
     still points at that detached wrapper, so test by ancestry, not parent. */
  function _attachTo(el) {
    if (!el) return;
    var inDoc = false, p = el;
    while (p) { if (p === vw) { inDoc = true; break; } p = p.parentNode; }
    if (inDoc) return;
    if (el.parentNode) { try { el.parentNode.removeChild(el); } catch (e) {} }
    vw.appendChild(el);
  }
  _attachTo(_pcChip);
  _attachTo(_pcSheet);
  /* Chip visibility: after sources arrive the pill stays; when the video is
     playing we dim it until the mouse moves again (professional auto-hide). */
  var hideTimer = null;
  function poke() {
    if (_pcChip) _pcChip.classList.add('show');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function() {
      var v = _pcVidEl;
      if (v && !v.paused && !_pcOpen) _pcShowChip(false);
    }, 2600);
  }
  vw.addEventListener('click', function(e) {
    if (!_pcOpen) return;
    if (e.target && e.target.closest && e.target.closest('.pc-sheet')) return;
    if (e.target && e.target.closest && e.target.closest('.pc-tracks')) return;
    _pcClose();
  });
  vw.addEventListener('mousemove', poke);
  vw.addEventListener('mouseenter', poke);
  vw.addEventListener('mouseleave', function() {
    var v = _pcVidEl;
    if (v && !v.paused && !_pcOpen) setTimeout(function() { _pcShowChip(false); }, 400);
  });
  vw.addEventListener('touchstart', poke, { passive: true });
  vid.addEventListener('pause', function() { _pcShowChip(true); _pcRefreshSources(); });
  vid.addEventListener('playing', function() { _pcShowChip(true); _pcRefreshSources(); setTimeout(function() { if (!_pcOpen && !vid.paused) _pcShowChip(false); }, 2600); });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && _pcOpen) _pcClose();
  });
}
function _pcOpenSheet() {
  _pcOpen = true;
  _pcRefreshSources();
  if (_pcChip) _pcChip.classList.add('show');
  _pcSheet.classList.add('open');
}
function _pcClose() {
  _pcOpen = false;
  _pcSheet.classList.remove('open');
  _pcShowChip(true);
}

function _pcEl(tag, cls, html) {
  var el = document.createElement(tag);
  if (cls) el.className = cls;
  if (html !== undefined) el.innerHTML = html;
  return el;
}
function _pcAddSection(body, label, hint) {
  var sec = _pcEl('div', 'pc-sec', label + (hint ? '<span class="pc-sec-hint">' + hint + '</span>' : ''));
  body.appendChild(sec);
  return sec;
}
function _pcAddRow(container, opts) {
  var row = _pcEl('div', 'pc-row' + (opts.on ? ' on' : ''));
  row.innerHTML = '<span class="pc-check">✓</span>' +
    (opts.badgeHtml || '') +
    '<span class="pc-txt">' + (opts.txt || '') + '</span>' +
    (opts.sub ? '<span class="pc-sub">' + opts.sub + '</span>' : '');
  row.addEventListener('click', function() { opts.onClick(); });
  container.appendChild(row);
  return row;
}

function _pcRenderSheet() {
  if (!_pcBody) return;
  _pcBody.innerHTML = '';
  var active = _pcActiveSrc();
  _pcEnsureSubs();

  /* ---- Dual section ---- */
  var dualRow = _pcEl('div', 'pc-dual-row');
  dualRow.innerHTML = '<div class="pc-txt"><b>Dual Stream</b><span>Play one stream’s video with another stream’s audio (independent volume + sync)</span></div><div class="pc-switch' + (_pcDual ? ' on' : '') + '"></div>';
  dualRow.addEventListener('click', function() {
    if (_pcDual) { _pcExitDual(); } else { _pcEnterDual(); }
  });
  _pcBody.appendChild(dualRow);
  if (_pcDual) {
    _pcRenderDualControls(_pcBody);
    return;
  }
  /* Coming back from Dual — restore the sheet header (title + hide ←). */
  var tEl = document.getElementById('pcTitle');
  if (tEl) tEl.textContent = 'Audio & Resolution';
  var bEl = document.getElementById('pcBack');
  if (bEl) bEl.style.display = 'none';

  /* ---- Audio languages ---- */
  var seenLang = {};
  var langList = [];
  for (var i = 0; i < _allSources.length; i++) {
    var s = _allSources[i];
    if (!s || !s.url) continue;
    var lang = _pcLangOf(s);
    if (!lang) lang = 'Original';
    if (!seenLang[lang]) { seenLang[lang] = []; langList.push(lang); }
    seenLang[lang].push(s);
  }
  langList.sort(function(a, b) {
    var ao = a === 'Original' ? 0 : 1, bo = b === 'Original' ? 0 : 1;
    if (ao !== bo) return ao - bo;
    return a.localeCompare(b);
  });
  _pcAddSection(_pcBody, 'Audio', langList.length + ' track' + (langList.length === 1 ? '' : 's'));
  var activeLang = active ? _pcLangOf(active) : null;
  langList.forEach(function(lang) {
    var list = seenLang[lang];
    var res = _pcSortedByBest(list.slice());
    var top = res[0];
    var sub = top ? (top._server || '') + (res.length > 1 ? ' +' + (res.length - 1) : '') : '';
    _pcAddRow(_pcBody, {
      on: activeLang && _pcLangEq(lang, activeLang),
      badgeHtml: '<span class="pc-lang-badge">' + lang + '</span>',
      txt: lang,
      sub: sub,
      onClick: function() { _pcPickLang(lang); }
    });
  });

  /* ---- Subtitles ---- */
  if (_allSubs && _allSubs.length) {
    _pcAddSection(_pcBody, 'Subtitles', _allSubs.length + ' track' + (_allSubs.length === 1 ? '' : 's'));
    _pcAddRow(_pcBody, {
      on: _pcSubUrl === null,
      badgeHtml: '<span class="pc-sub-badge">Off</span>',
      txt: 'Off',
      sub: '',
      onClick: function() { _pcSetSub(null); }
    });
    _allSubs.forEach(function(sub) {
      var label = sub.label || sub.lang_name || sub.lang || 'Subtitle';
      var name = String(label).substring(0, 34) + (String(label).length > 34 ? '…' : '');
      _pcAddRow(_pcBody, {
        on: _pcSubUrl !== null && sub.url === _pcSubUrl,
        badgeHtml: '<span class="pc-sub-badge">' + (sub.source === 'stremio' ? '★ ' : '') + (sub.lang || 'sub').toUpperCase() + '</span>',
        txt: name,
        sub: sub.source === 'stremio' ? 'stremio' : '',
        onClick: function() { _pcSetSub(sub); }
      });
    });
  } else if (!_pcSubFetching) {
    _pcAddSection(_pcBody, 'Subtitles', '');
    var note = _pcEl('div', 'pc-note', 'No subtitles bundled with these streams — searching…');
    _pcBody.appendChild(note);
  }

  /* ---- Resolutions ---- */
  var seenRes = {};
  var resList = [];
  for (var j = 0; j < _allSources.length; j++) {
    var src = _allSources[j];
    if (!src || !src.url) continue;
    /* Canonical label — language-stuffed qualities collapse to Auto. */
    var res = _pcCanonRes(src);
    if (!seenRes[res]) { seenRes[res] = []; resList.push(res); }
    seenRes[res].push(src);
  }
  resList.sort(function(a, b) { return _pcResRank(a) - _pcResRank(b); });
  _pcAddSection(_pcBody, 'Resolution', resList.length + ' qualit' + (resList.length === 1 ? 'y' : 'ies'));
  var activeRes = active ? _pcCanonRes(active) : null;
  resList.forEach(function(res) {
    var list = _pcSortedByBest(seenRes[res].slice());
    var top = list[0];
    var sub = top ? (top._server || '') : '';
    _pcAddRow(_pcBody, {
      /* Exact match — never compare by numeric value, because "Auto HLS",
         "Auto", "Hindi"… all parse to 0 and would light up together. */
      on: activeRes !== null && _pcResEq(activeRes, res),
      badgeHtml: '<span class="pc-badge">' + res + '</span>',
      txt: (res === 'Auto' ? 'Auto / Default' : res),
      sub: sub,
      onClick: function() { _pcPickRes(res); }
    });
  });
}

/* Compare two resolution labels for "same quality" purposes.
   Numeric labels compare numerically (1080p === 1080), anything else must be
   an exact (case-insensitive) string match so only ONE row lights up. */
function _pcResEq(a, b) {
  if (a === b) return true;
  var na = _pcResNum(a), nb = _pcResNum(b);
  if (na > 0 && nb > 0) return na === nb;
  return String(a || '').toLowerCase() === String(b || '').toLowerCase();
}

function _pcPickLang(lang) {
  _pcPrefLang = lang;
  var a = _pcActiveSrc();
  var wantRes = _pcPrefRes || (a ? _pcQOf(a) : null);
  var list = _pcCandidates(lang, wantRes);
  if (!list.length) list = _pcCandidates(lang, null);
  if (list.length) { _pcPrefRes = _pcQOf(list[0]); _pcPlaySource(list[0]); }
}
function _pcPickRes(res) {
  _pcPrefRes = res;
  var a = _pcActiveSrc();
  var wantLang = _pcPrefLang || (a ? _pcLangOf(a) : null);
  var list = _pcCandidates(wantLang, res);
  if (!list.length) list = _pcCandidates(null, res);
  if (list.length) { _pcPrefLang = _pcLangOf(list[0]); _pcPlaySource(list[0]); }
}
function _pcPlaySource(s) {
  if (!s || !s.url || !_pcVidEl) return;
  if (_pcDual) _pcExitDual();
  _activeSrcUrl = s.url;
  /* Explicit picks re-probe their URL and are always attempted first — a stale
     probe failure must not silently demote the user's chosen resolution. */
  if (window._pcInvalidateProbe) window._pcInvalidateProbe(s.url);
  window._pcUserPicked = true;
  _playHls(s.url, _pcVidEl);
  _pcRefreshSources();
}

/* ===== Dual stream ===== */
function _pcExitDual() {
  _pcDual = false;
  _dualMode = false;
  _stopSync();
  if (_audioHls) { try { _audioHls.destroy(); } catch (e) {} _audioHls = null; }
  if (typeof _audioEl !== 'undefined' && _audioEl) {
    try { _audioEl.pause(); _audioEl.removeAttribute('src'); } catch (e) {}
  }
  var v = _pcVidEl;
  if (v) {
    if (_pcVolBefore !== null) { v.volume = _pcVolBefore; v.muted = false; }
    else { v.muted = false; v.volume = 1; }
  }
  _pcDualVidUrl = ''; _pcDualAudUrl = '';
  if (_pcBody) _pcRenderSheet();
  _pcRefreshSources();
}
function _pcEnterDual() {
  if (_allSources.length < 1) return;
  _pcDual = true;
  var v = _pcVidEl;
  if (v) {
    _pcVolBefore = v.volume;
    v.volume = 0; v.muted = true;   /* external audio replaces the stream audio */
  }
  _dualMode = true;
  _startSync();
  _pcRenderSheet();
  /* start the audio leg automatically */
  var aud = _pcDualAudPick();
  if (aud && aud.url) _pcStartDualAudio(aud.url);
}
function _pcVideoOptions() {
  var out = [];
  for (var i = 0; i < _allSources.length; i++) {
    var s = _allSources[i];
    if (!s || !s.url) continue;
    var q = _pcQOf(s), lang = _pcLangOf(s);
    out.push({ url: s.url, label: (s._server || '?') + ' · ' + (q === '?' ? 'auto' : q) + (lang && lang !== 'Original' ? ' · ' + lang : ''), q: q });
  }
  return out;
}
function _pcDualAudPick() {
  /* An audio stream should ideally differ from the video stream and prefer a
     language the user asked for; otherwise any non-active stream. */
  var vidUrl = _pcDualVidUrl || (_pcActiveSrc() ? _pcActiveSrc().url : '');
  var prefer = _pcPrefLang;
  var out = [];
  for (var i = 0; i < _allSources.length; i++) {
    var s = _allSources[i];
    if (!s || !s.url || s.url === vidUrl) continue;
    if (prefer && _pcLangEq(_pcLangOf(s), prefer)) return s;
    out.push(s);
  }
  return _pcSortedByBest(out)[0] || null;
}
function _pcRenderDualControls(body) {
  var wrap = _pcEl('div', 'pc-dual-controls');
  var opts = _pcVideoOptions();
  var vSel = _pcEl('select');
  var selUrl = _pcDualVidUrl || (_pcActiveSrc() ? _pcActiveSrc().url : '');
  var vidPick = 0;
  for (var vi = 0; vi < opts.length; vi++) { if (opts[vi].url === selUrl) { vidPick = vi; break; } }
  opts.forEach(function(o, idx) {
    var opt = _pcEl('option', null, o.label);
    opt.value = idx;
    if (idx === vidPick) { opt.selected = true; _pcDualVidUrl = o.url; }
    vSel.appendChild(opt);
  });
  var vidVolIn = _pcEl('input'); vidVolIn.type = 'range'; vidVolIn.min = 0; vidVolIn.max = 100; vidVolIn.value = 0; vidVolIn.className = 'pc-vol';
  var vidVolVal = _pcEl('span', 'pc-vol-val', '0%');
  vidVolIn.addEventListener('input', function() { var vv = parseInt(vidVolIn.value) / 100; if (_pcVidEl) { _pcVidEl.volume = vv; _pcVidEl.muted = vv === 0; } vidVolVal.textContent = Math.round(vv * 100) + '%'; });

  var aud = _pcDualAudPick();
  var aSel = _pcEl('select');
  var audPick = 0;
  for (var ai = 0; ai < opts.length; ai++) { if (aud && opts[ai].url === aud.url) { audPick = ai; break; } }
  opts.forEach(function(o, idx) {
    var opt = _pcEl('option', null, o.label);
    opt.value = idx;
    if (idx === audPick) { opt.selected = true; _pcDualAudUrl = o.url; }
    aSel.appendChild(opt);
  });
  var audVolIn = _pcEl('input'); audVolIn.type = 'range'; audVolIn.min = 0; audVolIn.max = 100; audVolIn.value = 100; audVolIn.className = 'pc-vol';
  var audVolVal = _pcEl('span', 'pc-vol-val', '100%');
  audVolIn.addEventListener('input', function() { var av = parseInt(audVolIn.value) / 100; if (typeof _audioEl !== 'undefined' && _audioEl) { _audioEl.volume = av; _audioEl.muted = av === 0; } audVolVal.textContent = Math.round(av * 100) + '%'; });

  vSel.addEventListener('change', function() {
    var o = opts[parseInt(vSel.value)];
    if (!o) return;
    _pcDualVidUrl = o.url;
    _playHls(o.url, _pcVidEl);
  });
  aSel.addEventListener('change', function() {
    var o = opts[parseInt(aSel.value)];
    if (!o) return;
    _pcDualAudUrl = o.url;
    _pcStartDualAudio(o.url);
  });

  function dc(labelCls, labelTxt, el) {
    var row = _pcEl('div', 'pc-dc');
    row.appendChild(_pcEl('span', 'pc-dc-label ' + labelCls, labelTxt));
    row.appendChild(el);
    return row;
  }
  wrap.appendChild(dc('vid', 'Video', vSel));
  var volRow = _pcEl('div', 'pc-dc');
  volRow.appendChild(_pcEl('span', 'pc-dc-label vid', 'Vol'));
  volRow.appendChild(vidVolIn);
  volRow.appendChild(vidVolVal);
  wrap.appendChild(volRow);
  wrap.appendChild(dc('aud', 'Audio', aSel));
  var aVolRow = _pcEl('div', 'pc-dc');
  aVolRow.appendChild(_pcEl('span', 'pc-dc-label aud', 'Vol'));
  aVolRow.appendChild(audVolIn);
  aVolRow.appendChild(audVolVal);
  wrap.appendChild(aVolRow);

  var sync = _pcEl('div', 'pc-sync');
  function syncBtn(txt, delta) {
    var b = _pcEl('button', null, txt);
    b.addEventListener('click', function() {
      _audioOffset += delta;
      var v = _pcVidEl;
      if (typeof _audioEl !== 'undefined' && _audioEl && v) _audioEl.currentTime = v.currentTime + _audioOffset;
      var valEl = wrap.querySelector('.pc-sync-val');
      if (valEl) valEl.textContent = (_audioOffset > 0 ? '+' : '') + _audioOffset.toFixed(1) + 's';
    });
    return b;
  }
  var offEl = _pcEl('span', 'pc-sync-val', '0.0s');
  sync.appendChild(syncBtn('−.5s', -0.5));
  sync.appendChild(syncBtn('+.5s', 0.5));
  var rst = syncBtn('Reset', 0);
  rst.addEventListener('click', function() {
    _audioOffset = 0;
    var valEl = wrap.querySelector('.pc-sync-val');
    if (valEl) valEl.textContent = '0.0s';
    var v = _pcVidEl;
    if (typeof _audioEl !== 'undefined' && _audioEl && v) _audioEl.currentTime = v.currentTime;
  });
  sync.insertBefore(offEl, sync.children[1]);
  sync.appendChild(syncBtn('↺', 0));
  wrap.appendChild(sync);

  var titleEl = document.getElementById('pcTitle');
  if (titleEl) titleEl.textContent = 'Dual Stream';
  var backEl = document.getElementById('pcBack');
  if (backEl) backEl.style.display = '';
  body.appendChild(wrap);
}
function _pcStartDualAudio(url) {
  if (!url || !_pcVidEl) return;
  if (_audioHls) { try { _audioHls.destroy(); } catch (e) {} _audioHls = null; }
  if (typeof _audioEl === 'undefined' || !_audioEl) {
    _audioEl = document.createElement('audio');
    _audioEl.id = 'dualAudio';
    document.body.appendChild(_audioEl);
  }
  _audioEl.muted = false;
  _audioEl.volume = 1;
  var done = function() {
    var v = _pcVidEl;
    if (v) _audioEl.currentTime = v.currentTime + _audioOffset;
    _audioEl.play().catch(function() {});
  };
  if (url.indexOf('.m3u8') > -1 && window.Hls && window.Hls.isSupported()) {
    var h = new window.Hls({
      enableWorker: true, lowLatencyMode: false, maxBufferLength: 900, maxMaxBufferLength: 1800,
      backBufferLength: 60, highBufferWatchdogPeriod: 0.3, nudgeOffset: 0.1, maxSeekHole: 60,
      fragLoadingTimeOut: 30000, levelLoadingTimeOut: 15000, manifestLoadingTimeOut: 15000,
      fragLoadingMaxRetry: 5, levelLoadingMaxRetry: 2, manifestLoadingMaxRetry: 2,
      startLevel: -1, capLevelToPlayerSize: true, stretchShortVideoTrack: true,
      maxAudioFramesDrift: 4, startFragPrefetch: true, maxBufferSize: 209715200
    });
    h.loadSource(url);
    h.attachMedia(_audioEl);
    h.on(window.Hls.Events.MANIFEST_PARSED, function() { done(); });
    h.on(window.Hls.Events.ERROR, function(e, d) {
      if (d && d.fatal && d.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
        setTimeout(function() { if (h) h.startLoad(); }, 1500);
      }
    });
    _audioHls = h;
  } else {
    _audioEl.src = url;
    done();
  }
  _pcRefreshSources();
}

/* ===== Subtitles ===== */
function _pcTeardownSubs() {
  _pcSubGen++;   /* invalidates any in-flight subtitle fetch */
  _pcSubLoading = false;
  if (_pcSubTrack) {
    try {
      if (_pcSubTrack.tt) _pcSubTrack.tt.oncuechange = null;
      if (_pcSubTrack.el && _pcSubTrack.el.parentNode) _pcSubTrack.el.parentNode.removeChild(_pcSubTrack.el);
    } catch (e) {}
    _pcSubTrack = null;
  }
  if (_pcSubBlobUrl) { try { URL.revokeObjectURL(_pcSubBlobUrl); } catch (e) {} _pcSubBlobUrl = null; }
  _pcSubUrl = null;
  if (_pcSubOverlayEl && _pcSubOverlayEl.parentNode) {
    try { _pcSubOverlayEl.parentNode.removeChild(_pcSubOverlayEl); } catch (e) {}
  }
  _pcSubOverlayEl = null;
}
function _pcSubOverlay() {
  if (_pcSubOverlayEl && _pcSubOverlayEl.parentNode === _pcWrapEl) return _pcSubOverlayEl;
  _pcSubOverlayEl = _pcEl('div', 'pc-sub-overlay');
  if (_pcWrapEl) _pcWrapEl.appendChild(_pcSubOverlayEl);
  return _pcSubOverlayEl;
}
function _pcRenderCues(tt, overlay) {
  var lines = [], cues = tt ? tt.activeCues : null;
  if (cues) { for (var i = 0; i < cues.length; i++) lines.push(cues[i].text); }
  if (!overlay) return;
  overlay.textContent = lines.join('\n');
  overlay.classList.toggle('show', lines.length > 0);
}
function _pcFetchText(url) {
  /* credentials: omit — these subtitle CDNs answer Access-Control-Allow-Origin: *
     which browsers forbid combining with credentials. */
  return fetch(url, { mode: 'cors', credentials: 'omit', cache: 'no-store' }).then(function(r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.text();
  }).catch(function() {
    /* CORS/network blocked — retry through the browser service worker, never Django. */
    if (typeof _swActiveNow === 'function' && _swActiveNow() && typeof _makeProxyUrl === 'function') {
      var p = _makeProxyUrl(url);
      if (p && p !== url) {
        return fetch(p, { credentials: 'same-origin', cache: 'no-store' }).then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.text();
        });
      }
    }
    throw new Error('subtitle unreachable');
  });
}
/* SRT -> WebVTT. These bundled tracks are usually SRT even when named .vtt;
   <track> only parses WebVTT, so normalise timestamps and add the header. */
function _pcToVtt(text) {
  text = String(text || '');
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
  if (/^WEBVTT/i.test(text)) return text;
  text = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  var blocks = text.split(/\n{2,}/);
  var out = ['WEBVTT', ''];
  for (var i = 0; i < blocks.length; i++) {
    var b = blocks[i].trim();
    if (!b) continue;
    b = b.replace(/(\d{2}:\d{2}:\d{2}),(\d{3})/g, '$1.$2');
    var lines = b.split('\n');
    if (lines.length > 1 && /^\d+$/.test(lines[0].trim())) lines.shift();   /* SRT index line */
    b = lines.join('\n');
    if (/\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}/.test(b)) out.push(b, '');
  }
  return out.join('\n');
}
function _pcSetSub(sub) {
  var v = _pcVidEl;
  if (!v) return;
  _pcSubLoading = false;
  if (!sub || !sub.url) {
    _pcPrefSubUrl = null;
    _pcPrefSubMedia = null;
    _pcTeardownSubs();
    if (_pcOpen) _pcRenderSheet();
    return;
  }
  _pcPrefSubUrl = sub.url;
  _pcPrefSubMedia = _pcMediaKey();
  _pcTeardownSubs();
  var gen = _pcSubGen;
  var overlay = _pcSubOverlay();
  _pcSubLoading = true;
  /* Fetch + convert FIRST — only append the <track> once we have a local
     WebVTT blob, so an empty track can never error/leak into the DOM. */
  _pcFetchText(sub.url).then(function(text) {
    if (gen !== _pcSubGen) return;   /* superseded by a newer selection */
    var u = null;
    try {
      var vtt = _pcToVtt(text);
      var blob = new Blob([vtt], { type: 'text/vtt' });
      u = URL.createObjectURL(blob);
    } catch (e) { u = null; }
    _pcSubLoading = false;
    if (!u) { if (_pcOpen) _pcRenderSheet(); return; }
    _pcSubUrl = sub.url;
    _pcSubBlobUrl = u;
    var t = document.createElement('track');
    t.kind = 'subtitles';
    t.srclang = String(sub.lang || 'en').substring(0, 2);
    t.label = sub.label || sub.lang_name || 'Subtitle';
    v.appendChild(t);
    var tt = t.track;
    _pcSubTrack = { el: t, tt: tt, url: sub.url };
    if (tt) {
      tt.oncuechange = function() {
        if (_pcSubTrack && _pcSubTrack.el === t && _pcSubTrack.url === sub.url) _pcRenderCues(tt, overlay);
      };
      try { tt.mode = 'hidden'; } catch (e2) {}   /* hidden — we render cues ourselves, Netflix-style */
    }
    t.src = u;
    if (_pcOpen) _pcRenderSheet();   /* mark the new row active */
  }).catch(function() {
    /* Track unreachable/failed — reset the selection so the UI stays honest. */
    if (gen === _pcSubGen) { _pcSubLoading = false; _pcSubUrl = null; _pcPrefSubUrl = null; _pcPrefSubMedia = null; }
    if (_pcOpen) _pcRenderSheet();
  });
  if (_pcOpen) _pcRenderSheet();
}
function _pcEnsureSubs() {
  if (!_curMedia || !_curMedia.tmdbId) return;
  if ((!_allSubs || !_allSubs.length) && !_pcSubFetching && !_pcSubFallback) {
    _pcSubFallback = true;
    _pcSubFetching = true;
    var out = [];
    var done = function() {
      var seen = {};
      out.forEach(function(sub) { if (seen[sub.url]) return; seen[sub.url] = 1; _allSubs.push(sub); });
      _pcSubFetching = false;
      if (typeof _pcRefreshSources === 'function') _pcRefreshSources();
    };
    var mediaType = _curMedia.type === 'tv' ? 'tv' : 'movie';
    /* Subtitle tracks bundled with these streams (server endpoint used as a fallback
       only when the client-decrypted payloads carried none). */
    var u1 = '/ajax/videasy-sources/?tmdb_id=' + encodeURIComponent(_curMedia.tmdbId) + '&media_type=' + encodeURIComponent(mediaType);
    if (mediaType === 'tv') u1 += '&season_id=' + encodeURIComponent(_curMedia.season || '1') + '&episode_id=' + encodeURIComponent(_curMedia.episode || '1');
    var p1 = fetch(u1).then(function(r) { return r.json(); }).then(function(d) {
      (d.servers || []).forEach(function(s) {
        (s.subtitles || []).forEach(function(sub) {
          if (sub && sub.url) out.push({ url: sub.url, lang: String(sub.lang || sub.language || 'en').substring(0, 2), lang_name: sub.language || sub.lang || 'en', label: (sub.language || sub.lang || 'en') + ' subtitle', source: 'stream' });
        });
      });
    }).catch(function() {});
    var u2 = '/ajax/stremio-subtitles/?tmdb_id=' + encodeURIComponent(_curMedia.tmdbId) + '&type=' + encodeURIComponent(mediaType) + '&lang=all';
    if (mediaType === 'tv') u2 += '&season=' + encodeURIComponent(_curMedia.season || '1') + '&episode=' + encodeURIComponent(_curMedia.episode || '1');
    var p2 = fetch(u2).then(function(r) { return r.json(); }).then(function(d) {
      (d.subtitles || []).forEach(function(sub) {
        if (sub && sub.url) out.push({ url: sub.url, lang: String(sub.lang || 'en').substring(0, 2), lang_name: sub.lang_name || sub.lang || 'en', label: sub.label || sub.lang_name || sub.lang || 'subtitle', source: 'stremio' });
      });
    }).catch(function() {});
    Promise.all([p1, p2]).then(done);
  }
}

/* keep visibility refresh when picking rows (video 'pause'/'playing' handlers call _pcRefreshSources) */
window._pcOpenSheet = _pcOpenSheet;
window._pcClose = _pcClose;
window._pcPickLang = _pcPickLang;
window._pcPickRes = _pcPickRes;
window._pcPlaySource = _pcPlaySource;
window._pcExitDual = _pcExitDual;

/* Nonstop auto-advance: when the page switches to the next episode,
   drop Dual mode cleanly so the old audio stream can't ghost over it. */
window.addEventListener('nm:episode-change', function() {
  if ((typeof _pcDual !== 'undefined' && _pcDual) || (typeof _dualMode !== 'undefined' && _dualMode)) {
    _pcExitDual();
  }
});
