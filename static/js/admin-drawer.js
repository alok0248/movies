/* ============================================================
 * Right-hand detail drawer for admin lists
 * A row marked with data-drawer="/admin-dashboard/.../peek/<id>/"
 * opens its detail in a side panel instead of a full page load, so the
 * list keeps its filters, its page and its scroll position. The fragment
 * is ordinary server-rendered HTML, and every drawer offers a link to the
 * full page for anything the summary cannot do.
 * ============================================================ */
(function () {
    if (window.openAdminDrawer) { return; }

    var CSS = [
        '.adw-wrap{position:fixed;inset:0;z-index:3100}',
        '.adw-backdrop{position:absolute;inset:0;background:rgba(2,6,23,.55);animation:adwFade .18s ease}',
        '.adw-panel{position:absolute;top:0;right:0;height:100%;width:480px;max-width:94vw;display:flex;flex-direction:column;background:#0f172a;border-left:1px solid rgba(148,163,184,.22);box-shadow:-30px 0 70px rgba(0,0,0,.55);animation:adwIn .22s ease}',
        '.adw-head{display:flex;align-items:center;gap:.75rem;padding:16px 18px;border-bottom:1px solid rgba(148,163,184,.16);flex:0 0 auto}',
        '.adw-title{font-size:.95rem;font-weight:700;color:#e2e8f0;line-height:1.3}',
        '.adw-x{margin-left:auto;border:0;background:rgba(148,163,184,.16);color:#cbd5e1;width:32px;height:32px;border-radius:9px;font-size:1.15rem;line-height:1;cursor:pointer;font-family:inherit}',
        '.adw-x:hover{background:rgba(148,163,184,.3)}',
        '.adw-body{padding:16px 18px 28px;overflow-y:auto;flex:1 1 auto;color:#cbd5e1;font-size:.85rem}',
        '.adw-loading,.adw-error{color:#94a3b8;font-size:.85rem;padding:8px 0}',
        '.adw-error{color:#fca5a5}',
        '.adw-body h4{margin:0 0 .5rem;font-size:.95rem;color:#e2e8f0;font-weight:700}',
        '.adw-section{margin:0 0 1.1rem}',
        '.adw-section-title{font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:#94a3b8;font-weight:700;margin:0 0 .5rem}',
        '.adw-kv{display:grid;grid-template-columns:132px 1fr;gap:.3rem .6rem;font-size:.82rem}',
        '.adw-kv dt{color:#8b98ab;font-weight:600}',
        '.adw-kv dd{margin:0;color:#e2e8f0;word-break:break-word}',
        '.adw-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem}',
        '.adw-chip{display:inline-block;padding:.12rem .45rem;border-radius:999px;font-size:.7rem;font-weight:700;background:rgba(148,163,184,.18);color:#e2e8f0;margin-right:.3rem}',
        '.adw-chip.on{background:rgba(239,68,68,.85);color:#fff}',
        '.adw-chip.ok{background:rgba(34,197,94,.85);color:#fff}',
        '.adw-chip.info{background:rgba(99,102,241,.85);color:#fff}',
        '.adw-actions{display:flex;flex-wrap:wrap;gap:.45rem;margin-top:.2rem}',
        '.adw-btn{display:inline-flex;align-items:center;gap:.35rem;border:1px solid rgba(148,163,184,.3);background:rgba(148,163,184,.12);color:#e2e8f0;border-radius:9px;padding:.42rem .7rem;font-size:.79rem;font-weight:600;text-decoration:none;cursor:pointer;font-family:inherit}',
        '.adw-btn:hover{background:rgba(148,163,184,.22);color:#fff}',
        '.adw-btn.danger{border-color:rgba(239,68,68,.45);background:rgba(239,68,68,.16);color:#fca5a5}',
        '.adw-btn.primary{border-color:rgba(99,102,241,.5);background:rgba(99,102,241,.22);color:#c7d2fe}',
        '.adw-list{margin:0;padding:0;list-style:none}',
        '.adw-list li{padding:.4rem 0;border-bottom:1px solid rgba(148,163,184,.12);font-size:.8rem}',
        '.adw-list li:last-child{border-bottom:0}',
        '.adw-empty{color:#94a3b8;font-size:.8rem}',
        '@keyframes adwIn{from{transform:translateX(24px);opacity:.4}to{transform:translateX(0);opacity:1}}',
        '@keyframes adwFade{from{opacity:0}to{opacity:1}}',
        'html[data-theme="light"] .adw-panel{background:#fff;border-left-color:#e2e8f0}',
        'html[data-theme="light"] .adw-head{border-bottom-color:#e2e8f0}',
        'html[data-theme="light"] .adw-title,html[data-theme="light"] .adw-body h4,html[data-theme="light"] .adw-kv dd{color:#111827}',
        'html[data-theme="light"] .adw-body{color:#334155}',
        'html[data-theme="light"] .adw-btn{color:#111827;border-color:#cbd5e1;background:#f1f5f9}',
        'html[data-theme="light"] .adw-x{background:#e2e8f0;color:#334155}'
    ].join('');

    var escHandler = null;

    function injectStyles() {
        if (document.getElementById('adwStyles')) { return; }
        var s = document.createElement('style');
        s.id = 'adwStyles';
        s.textContent = CSS;
        document.head.appendChild(s);
    }

    function close() {
        var wrap = document.getElementById('adwWrap');
        if (wrap && wrap.parentNode) { wrap.parentNode.removeChild(wrap); }
        if (escHandler) {
            document.removeEventListener('keydown', escHandler);
            escHandler = null;
        }
    }

    function open(url, title) {
        if (!url) { return; }
        injectStyles();
        close();

        var wrap = document.createElement('div');
        wrap.className = 'adw-wrap';
        wrap.id = 'adwWrap';
        wrap.innerHTML =
            '<div class="adw-backdrop" data-adw-close></div>' +
            '<aside class="adw-panel" role="dialog" aria-modal="true" aria-label="' + (title || 'Detail') + '">' +
                '<header class="adw-head">' +
                    '<div class="adw-title">' + (title || 'Detail') + '</div>' +
                    '<button type="button" class="adw-x" data-adw-close aria-label="Close">&times;</button>' +
                '</header>' +
                '<div class="adw-body"><div class="adw-loading">Loading…</div></div>' +
            '</aside>';
        document.body.appendChild(wrap);

        var body = wrap.querySelector('.adw-body');
        escHandler = function (e) { if (e.key === 'Escape') { close(); } };
        document.addEventListener('keydown', escHandler);
        wrap.addEventListener('click', function (e) {
            if (e.target.closest('[data-adw-close]')) { close(); }
        });

        fetch(url, {
            headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'text/html' },
            credentials: 'same-origin'
        }).then(function (r) {
            if (!r.ok) { throw new Error('HTTP ' + r.status); }
            return r.text();
        }).then(function (html) {
            body.innerHTML = html;
        }).catch(function () {
            body.innerHTML = '<div class="adw-error">Could not load this detail. ' +
                'Sign in again if the session expired, or open the full page.</div>';
        });
    }

    window.openAdminDrawer = open;
    window.closeAdminDrawer = close;

    /* Rows carry data-drawer; clicks on their own links and buttons keep working. */
    document.addEventListener('click', function (e) {
        var row = e.target.closest('[data-drawer]');
        if (!row) { return; }
        if (e.target.closest('a,button,input,select,textarea,label,form,[data-no-drawer]')) { return; }
        if (e.ctrlKey || e.metaKey || e.shiftKey) { return; }
        e.preventDefault();
        open(row.getAttribute('data-drawer'), row.getAttribute('data-drawer-title') || 'Detail');
    });
})();
