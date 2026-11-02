/* ============================================================
 * Typing confirmation for destructive actions
 * A wipe or a delete cannot be undone, so those controls open a
 * dialog that asks the admin to type a word first. Use it where
 * confirm() used to sit:
 *   <form  onsubmit="return confirmTyped(event, {what: 'this user'})">
 *   <a     onclick="return confirmTyped(event, {what: 'all logs'})">
 *   confirmTyped(null, {what: 'the data', onConfirm: function(){ ... }})
 * The dialog only replays the original action once the word matches.
 * ============================================================ */
(function () {
    if (window.confirmTyped) { return; }

    var CSS = [
        '.ct-overlay{position:fixed;inset:0;z-index:3000;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(2,6,23,.74)}',
        '.ct-card{width:100%;max-width:470px;background:#111a2e;border:1px solid rgba(239,68,68,.45);border-radius:16px;padding:22px 22px 18px;box-shadow:0 30px 80px rgba(0,0,0,.62);color:#e2e8f0}',
        '.ct-head{display:flex;align-items:center;gap:12px;margin-bottom:12px}',
        '.ct-icon{flex:0 0 auto;width:44px;height:44px;border-radius:13px;display:flex;align-items:center;justify-content:center;font-size:1.12rem;background:linear-gradient(135deg,#ef4444,#b91c1c);color:#fff}',
        '.ct-eyebrow{font-size:.68rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#f87171}',
        '.ct-title{font-size:1rem;font-weight:700;margin:.1rem 0 0;line-height:1.35}',
        '.ct-text{font-size:.86rem;line-height:1.62;color:#a9b6c9;margin:0 0 .4rem}',
        '.ct-text b{color:#fca5a5;font-weight:600}',
        '.ct-detail{font-size:.78rem;line-height:1.6;color:#8b98ab;margin:.3rem 0 .9rem}',
        '.ct-label{display:block;font-size:.74rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#9fb0c7;margin:0 0 .45rem}',
        '.ct-label code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.08em;color:#fca5a5}',
        '.ct-input{width:100%;box-sizing:border-box;border-radius:10px;border:1px solid rgba(148,163,184,.35);background:rgba(2,6,23,.55);color:#e2e8f0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.92rem;letter-spacing:.1em;padding:.62rem .75rem;outline:none}',
        '.ct-input:focus{border-color:#ef4444;box-shadow:0 0 0 3px rgba(239,68,68,.18)}',
        '.ct-hint{font-size:.74rem;color:#8b98ab;margin:.45rem 0 0;min-height:1rem}',
        '.ct-actions{display:flex;gap:.5rem;justify-content:flex-end;margin-top:1rem}',
        '.ct-btn{border:0;border-radius:10px;padding:.58rem 1rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:inherit}',
        '.ct-btn-cancel{background:rgba(148,163,184,.16);color:#cbd5e1}',
        '.ct-btn-cancel:hover{background:rgba(148,163,184,.26)}',
        '.ct-btn-danger{background:#dc2626;color:#fff}',
        '.ct-btn-danger:hover{background:#b91c1c}',
        '.ct-btn-danger:disabled{opacity:.45;cursor:not-allowed}',
        'html[data-theme="light"] .ct-card{background:#fff;border-color:#fca5a5;color:#111827;box-shadow:0 30px 80px rgba(15,23,42,.25)}',
        'html[data-theme="light"] .ct-text,html[data-theme="light"] .ct-detail{color:#4b5563}',
        'html[data-theme="light"] .ct-label{color:#475569}',
        'html[data-theme="light"] .ct-input{background:#fff;color:#111827;border-color:#cbd5e1}',
        'html[data-theme="light"] .ct-btn-cancel{background:#e2e8f0;color:#334155}'
    ].join('');

    function injectStyles() {
        if (document.getElementById('ctStyles')) { return; }
        var s = document.createElement('style');
        s.id = 'ctStyles';
        s.textContent = CSS;
        document.head.appendChild(s);
    }

    window.confirmTyped = function (evt, opts) {
        if (evt) { evt.preventDefault(); evt.stopPropagation(); }
        if (document.getElementById('ctOverlay')) { return false; }
        injectStyles();

        var o = opts || {};
        var trigger = (evt && evt.currentTarget) || o.trigger || null;
        var submitter = (evt && evt.submitter) || null;
        var word = String(o.word || 'DELETE').toUpperCase();
        var what = o.what || 'this item';

        var wrap = document.createElement('div');
        wrap.className = 'ct-overlay';
        wrap.id = 'ctOverlay';
        wrap.innerHTML =
            '<div class="ct-card" role="dialog" aria-modal="true" aria-labelledby="ctTitle">' +
                '<div class="ct-head">' +
                    '<div class="ct-icon"><i class="fas fa-triangle-exclamation"></i></div>' +
                    '<div>' +
                        '<div class="ct-eyebrow">Permanent action</div>' +
                        '<h2 class="ct-title" id="ctTitle">' + (o.title || 'This cannot be undone') + '</h2>' +
                    '</div>' +
                '</div>' +
                '<p class="ct-text">You are about to remove <b>' + what + '</b>.</p>' +
                (o.detail ? '<p class="ct-detail">' + o.detail + '</p>' : '') +
                '<label class="ct-label" for="ctWord">Type <code>' + word + '</code> to confirm</label>' +
                '<input class="ct-input" id="ctWord" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="' + word + '">' +
                '<p class="ct-hint" id="ctHint"></p>' +
                '<div class="ct-actions">' +
                    '<button type="button" class="ct-btn ct-btn-cancel" id="ctCancel">Cancel</button>' +
                    '<button type="button" class="ct-btn ct-btn-danger" id="ctGo" disabled>Continue</button>' +
                '</div>' +
            '</div>';
        document.body.appendChild(wrap);

        var input = wrap.querySelector('#ctWord');
        var go = wrap.querySelector('#ctGo');
        var hint = wrap.querySelector('#ctHint');
        var cancel = wrap.querySelector('#ctCancel');

        function close() { if (wrap.parentNode) { wrap.parentNode.removeChild(wrap); } }
        function typed() { return input.value.trim().toUpperCase() === word; }
        function refresh() {
            var ok = typed();
            go.disabled = !ok;
            hint.textContent = (input.value && !ok) ? 'That does not match yet.' : '';
        }
        function proceed() {
            if (!typed()) { refresh(); return; }
            close();
            if (typeof o.onConfirm === 'function') { o.onConfirm(); return; }
            if (trigger && trigger.tagName === 'FORM') {
                if (submitter && submitter.name) {
                    var hidden = document.createElement('input');
                    hidden.type = 'hidden';
                    hidden.name = submitter.name;
                    hidden.value = submitter.value || '';
                    trigger.appendChild(hidden);
                }
                trigger.submit();
                return;
            }
            var href = (trigger && (trigger.getAttribute('href') || trigger.href)) || o.href || '';
            if (href && href !== '#') { window.location.href = href; }
        }

        input.addEventListener('input', refresh);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); if (!go.disabled) { proceed(); } }
        });
        wrap.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { e.preventDefault(); close(); }
        });
        wrap.addEventListener('click', function (e) { if (e.target === wrap) { close(); } });
        cancel.addEventListener('click', close);
        go.addEventListener('click', proceed);
        setTimeout(function () { input.focus(); }, 30);
        return false;
    };
})();
