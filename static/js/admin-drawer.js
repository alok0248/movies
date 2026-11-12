/* ============================================================
 * Right-hand detail drawer for admin lists
 * A row marked with data-drawer="/admin-dashboard/.../peek/<id>/"
 * opens its detail in a side panel instead of a full page load, so the
 * list keeps its filters, its page and its scroll position. The fragment
 * is ordinary server-rendered HTML, and every drawer offers a link to the
 * full page for anything the summary cannot do.
 *
 * The panel is a modal dialog, the same shape as the IP-geo card: a
 * role="dialog" named by its visible title, dismissed by Escape, by the
 * backdrop or by its Close button. On top of that it takes focus when it
 * opens, keeps Tab inside it, and hands focus back to the row that opened
 * it, so the whole interaction works from the keyboard.
 *
 * A row that holds links or buttons of its own is never turned into a control
 * itself: no role="button" around controls nested in a button, and no tab stop
 * spent on every one of hundreds of rows. Such a row carries a real button of
 * its own (data-drawer-open) that opens the same panel, and focus comes back to
 * that button when the panel closes.
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
        '.adw-x:focus-visible,.adw-btn:focus-visible,.adw-panel:focus-visible{outline:2px solid #60a5fa;outline-offset:2px}',
        'html[data-theme="light"] .adw-x:focus-visible,html[data-theme="light"] .adw-btn:focus-visible,html[data-theme="light"] .adw-panel:focus-visible{outline-color:#1d4ed8}',
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
    var opener = null; /* the row that opened the drawer, so focus can go back */

    /* aria-modal is a promise that the rest of the page is out of reach, and a
       focus trap alone does not keep that promise: a screen reader reads the
       list behind the panel and its virtual cursor walks there. While a dialog
       is up, every body child other than that dialog is inert and hidden from
       assistive tech, and put back untouched when it closes.

       Two dialogs can stack: the IP-geo card opens from a row inside the
       drawer. Only the topmost one stays live, and the marks are recomputed
       from the open list, so closing it hands the page back to the drawer
       rather than to the list underneath. */
    var openDialogs = [];   /* dialogs currently up, last one on top */
    var inertMarks = [];    /* {el, aria}: the aria-hidden each element had before */

    function markInert(el) {
        for (var i = 0; i < inertMarks.length; i++) {
            if (inertMarks[i].el === el) { return; }
        }
        inertMarks.push({ el: el, aria: el.getAttribute('aria-hidden') });
        el.setAttribute('inert', '');
        el.setAttribute('aria-hidden', 'true');
    }

    function unmarkInert(el) {
        for (var i = inertMarks.length - 1; i >= 0; i--) {
            if (inertMarks[i].el !== el) { continue; }
            if (inertMarks[i].aria === null) { el.removeAttribute('aria-hidden'); }
            else { el.setAttribute('aria-hidden', inertMarks[i].aria); }
            el.removeAttribute('inert');
            inertMarks.splice(i, 1);
        }
    }

    function syncInert() {
        /* A dialog can be torn down without telling us; it is not a live one. */
        for (var i = 0; i < openDialogs.length;) {
            if (document.documentElement.contains(openDialogs[i])) { i++; }
            else { openDialogs.splice(i, 1); }
        }
        var top = openDialogs.length ? openDialogs[openDialogs.length - 1] : null;
        var kids = document.body.children;
        for (var k = 0; k < kids.length; k++) {
            var el = kids[k];
            var tag = el.tagName;
            if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK') { continue; }
            if (!top) { unmarkInert(el); }                    /* nothing open: page is live */
            else if (el === top || el.contains(top)) { unmarkInert(el); }
            else { markInert(el); }
        }
    }

    var pageInert = {
        claim: function (node) {
            if (!node || openDialogs.indexOf(node) !== -1) { return; }
            openDialogs.push(node);
            syncInert();
        },
        release: function (node) {
            var i = openDialogs.indexOf(node);
            if (i !== -1) { openDialogs.splice(i, 1); }
            syncInert();
        },
        isTop: function (node) {
            return !openDialogs.length || openDialogs[openDialogs.length - 1] === node;
        }
    };
    window.adminModalInert = pageInert;

    function injectStyles() {
        if (document.getElementById('adwStyles')) { return; }
        var s = document.createElement('style');
        s.id = 'adwStyles';
        s.textContent = CSS;
        document.head.appendChild(s);
    }

    function close() {
        var wrap = document.getElementById('adwWrap');
        if (wrap && wrap.parentNode) {
            wrap.parentNode.removeChild(wrap);
            pageInert.release(wrap);
        }
        if (escHandler) {
            document.removeEventListener('keydown', escHandler);
            escHandler = null;
        }
        var back = opener;
        opener = null;
        if (back && document.contains(back) && typeof back.focus === 'function') {
            back.focus();
        }
    }

    /* Everything Tab may land on inside the panel, skipping hidden controls. */
    function panelFocusables(panel) {
        var nodes = panel.querySelectorAll(
            'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
            'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])');
        var out = [];
        for (var i = 0; i < nodes.length; i++) {
            if (nodes[i].offsetWidth || nodes[i].offsetHeight || nodes[i].getClientRects().length) {
                out.push(nodes[i]);
            }
        }
        return out;
    }

    /* aria-modal promises the rest of the page is inert, so Tab wraps here. */
    function trapTab(event, panel) {
        var items = panelFocusables(panel);
        if (!items.length) {
            event.preventDefault();
            panel.focus();
            return;
        }
        var first = items[0];
        var last = items[items.length - 1];
        var active = document.activeElement;
        var inside = panel.contains(active);
        if (event.shiftKey) {
            if (!inside || active === first || active === panel) {
                event.preventDefault();
                last.focus();
            }
        } else if (!inside || active === last) {
            event.preventDefault();
            first.focus();
        }
    }

    function open(url, title, trigger) {
        if (!url) { return; }
        var restoreTo = trigger || document.activeElement;
        injectStyles();
        close();
        opener = restoreTo;

        var wrap = document.createElement('div');
        wrap.className = 'adw-wrap';
        wrap.id = 'adwWrap';
        wrap.innerHTML =
            '<div class="adw-backdrop" data-adw-close></div>' +
            '<aside class="adw-panel" role="dialog" aria-modal="true" aria-labelledby="adwTitle" tabindex="-1">' +
                '<header class="adw-head">' +
                    '<div class="adw-title" id="adwTitle">' + (title || 'Detail') + '</div>' +
                    '<button type="button" class="adw-x" data-adw-close aria-label="Close">&times;</button>' +
                '</header>' +
                '<div class="adw-body"><div class="adw-loading">Loading...</div></div>' +
            '</aside>';
        document.body.appendChild(wrap);
        pageInert.claim(wrap);

        var panel = wrap.querySelector('.adw-panel');
        var body = wrap.querySelector('.adw-body');
        escHandler = function (e) {
            /* With the IP-geo card open on top, Escape and Tab belong to it: it
               claims the key, and it is not "top" any more once it is closed. */
            if (e.defaultPrevented || !pageInert.isTop(wrap)) { return; }
            if (e.key === 'Escape') { close(); return; }
            if (e.key === 'Tab') { trapTab(e, panel); }
        };
        document.addEventListener('keydown', escHandler);
        wrap.addEventListener('click', function (e) {
            if (e.target.closest('[data-adw-close]')) { close(); }
        });
        wrap.querySelector('.adw-x').focus();

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
        open(row.getAttribute('data-drawer'), row.getAttribute('data-drawer-title') || 'Detail', row);
    });

    /* The keyboard path into a row that carries controls of its own: a real
       button inside the row, marked data-drawer-open. Being a button, Enter and
       Space need no special handling, and it is what focus returns to. */
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-drawer-open]');
        if (!btn) { return; }
        var row = btn.closest('[data-drawer]');
        if (!row) { return; }
        e.preventDefault();
        open(row.getAttribute('data-drawer'), row.getAttribute('data-drawer-title') || 'Detail', btn);
    });

    /* The controls a row may hold in its own right. If any of these is inside a
       row, the row stays a plain container: no role on a box that contains
       buttons, and no tab stop on each row of a long list. */
    var OWN_CONTROLS =
        'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), ' +
        'select:not([disabled]), textarea:not([disabled]), summary, label, form, ' +
        '[contenteditable="true"], [tabindex]:not([tabindex="-1"]), [data-no-drawer]';

    /* A row is a mouse target by nature. When nothing inside it is a control,
       the row itself can be one: a tab stop, Enter or Space opening the same
       dialog, and the dialog announced. A row that does hold its own links or
       buttons is left as it is, and opens from the real button inside it
       (data-drawer-open). */
    function prepareTriggers() {
        var rows = document.querySelectorAll('[data-drawer]');
        for (var i = 0; i < rows.length; i++) {
            var el = rows[i];
            if (el.querySelector(OWN_CONTROLS)) { continue; }
            if (!el.hasAttribute('tabindex')) { el.setAttribute('tabindex', '0'); }
            if (el.tagName !== 'TR' && !el.hasAttribute('role')) { el.setAttribute('role', 'button'); }
            el.setAttribute('aria-haspopup', 'dialog');
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', prepareTriggers);
    } else {
        prepareTriggers();
    }

    /* Enter or Space on a focused row that is its own control. */
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') { return; }
        var el = e.target;
        if (!el || !el.hasAttribute || !el.hasAttribute('data-drawer')) { return; }
        e.preventDefault();
        open(el.getAttribute('data-drawer'), el.getAttribute('data-drawer-title') || 'Detail', el);
    });
})();