/* Developer-tools guard.
 *
 * The site is meant to be watched, not taken apart. This script notices the
 * usual ways a desktop browser opens its inspector (a docked panel shrinking
 * the viewport, or the console rendering an object we can watch) and puts a
 * blocking screen over the page until it is closed: media is paused, the page
 * cannot be clicked, and the visitor is asked to close the tools.
 *
 * What this can and cannot do: a web page cannot close the browser's developer
 * tools, and it cannot hide the HTML it was built from. This is a deterrent
 * for casual copying, not a guarantee - the server-side checks (the Cloudflare
 * challenge, the API gate) are what actually protect the content.
 */
(function () {
    'use strict';
    if (window.__devToolsGuard) { return; }
    window.__devToolsGuard = true;

    var OVERLAY_ID = '__devToolsGuard';
    var POLL_MS = 800;
    var GAP_PX = 190;
    var gateOpen = false;

    /* --- detection ------------------------------------------------------- */

    /* A docked inspector takes space from the page but not from the window, so
       outer minus inner grows by the size of the panel. The width check is
       skipped when the device pixel ratio shows zoom or a HiDPI screen, because
       zoom shrinks innerWidth the same way a side panel does. */
    function dockedPanel() {
        if (!window.outerWidth || !window.outerHeight) { return false; }
        if (window.outerHeight - window.innerHeight > GAP_PX) { return true; }
        var zoomed = Math.abs((window.devicePixelRatio || 1) - 1) > 0.05;
        return !zoomed && (window.outerWidth - window.innerWidth > GAP_PX);
    }

    /* The console re-renders logged objects when it opens, so a getter on one of
       their properties fires exactly then. It catches an undocked inspector too,
       which the size check cannot see. */
    var consoleSeen = false;
    var bait = null;
    try {
        bait = {};
        Object.defineProperty(bait, 'id', {
            get: function () { consoleSeen = true; return 'x'; },
            configurable: true,
        });
    } catch (e) { bait = null; }

    function baitConsole() {
        if (!bait) { return; }
        try { console.log(bait); } catch (e) {}
    }

    /* --- blocking screen ------------------------------------------------- */

    var CSS = ''
        + '#' + OVERLAY_ID + '{position:fixed;inset:0;z-index:2147483000;display:none;'
        + 'align-items:center;justify-content:center;padding:1.5rem;'
        + 'background:radial-gradient(900px 420px at 50% -10%,rgba(229,9,20,.22),transparent 60%),#08090d;'
        + 'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;color:#eef1f6;}'
        + '#' + OVERLAY_ID + '.is-open{display:flex;}'
        + '#' + OVERLAY_ID + ' .dg-card{max-width:440px;text-align:center;'
        + 'border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.04);'
        + 'border-radius:18px;padding:2rem 1.6rem;box-shadow:0 24px 70px rgba(0,0,0,.55);}'
        + '#' + OVERLAY_ID + ' .dg-mark{width:54px;height:54px;border-radius:50%;margin:0 auto 1rem;'
        + 'display:flex;align-items:center;justify-content:center;font-size:1.5rem;font-weight:800;'
        + 'background:rgba(229,9,20,.16);color:#ff5a63;}'
        + '#' + OVERLAY_ID + ' h2{font-size:1.2rem;margin:0 0 .55rem;}'
        + '#' + OVERLAY_ID + ' p{margin:0 0 .7rem;font-size:.9rem;line-height:1.6;color:#b6bcc7;}'
        + '#' + OVERLAY_ID + ' p.small{font-size:.78rem;color:#7f8794;margin:0;}'
        + 'html.__dg-locked{overflow:hidden!important;}';

    function injectStyle() {
        if (document.getElementById(OVERLAY_ID + '-style')) { return; }
        var style = document.createElement('style');
        style.id = OVERLAY_ID + '-style';
        style.textContent = CSS;
        (document.head || document.documentElement).appendChild(style);
    }

    function buildOverlay() {
        var overlay = document.createElement('div');
        overlay.id = OVERLAY_ID;
        overlay.setAttribute('role', 'alertdialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-labelledby', OVERLAY_ID + '-title');

        var card = document.createElement('div');
        card.className = 'dg-card';

        var mark = document.createElement('div');
        mark.className = 'dg-mark';
        mark.setAttribute('aria-hidden', 'true');
        mark.textContent = '!';

        var title = document.createElement('h2');
        title.id = OVERLAY_ID + '-title';
        title.textContent = 'Developer tools detected';

        var body = document.createElement('p');
        body.textContent = 'Close the browser\u2019s developer tools to keep browsing. '
            + 'This page stays blocked while they are open.';

        var small = document.createElement('p');
        small.className = 'small';
        small.textContent = 'Watching is fine, copying the site is not. Once the tools are '
            + 'closed this screen disappears by itself.';

        card.appendChild(mark);
        card.appendChild(title);
        card.appendChild(body);
        card.appendChild(small);
        overlay.appendChild(card);
        return overlay;
    }

    function pauseMedia() {
        var media = document.querySelectorAll('video,audio');
        for (var i = 0; i < media.length; i++) {
            try { if (!media[i].paused) { media[i].pause(); } } catch (e) {}
        }
        if (document.fullscreenElement && document.exitFullscreen) {
            try { document.exitFullscreen(); } catch (e) {}
        }
    }

    function showGate() {
        if (gateOpen) { return; }
        gateOpen = true;
        injectStyle();
        var overlay = document.getElementById(OVERLAY_ID);
        if (!overlay) {
            overlay = buildOverlay();
            (document.body || document.documentElement).appendChild(overlay);
        }
        overlay.classList.add('is-open');
        document.documentElement.classList.add('__dg-locked');
        pauseMedia();
        try {
            document.dispatchEvent(new CustomEvent('devtoolsguard:open'));
        } catch (e) {}
    }

    function hideGate() {
        if (!gateOpen) { return; }
        gateOpen = false;
        var overlay = document.getElementById(OVERLAY_ID);
        if (overlay) { overlay.classList.remove('is-open'); }
        document.documentElement.classList.remove('__dg-locked');
        try {
            document.dispatchEvent(new CustomEvent('devtoolsguard:close'));
        } catch (e) {}
    }

    /* --- loop ------------------------------------------------------------ */

    function check() {
        var seen = consoleSeen;
        consoleSeen = false;
        if (dockedPanel() || seen) {
            showGate();
            return;
        }
        if (gateOpen) { hideGate(); }
        /* Only log while the gate is shut: when the console opens it re-renders
           this object and the getter trips. A few entries, not a stream. */
        baitConsole();
    }

    function start() {
        try { baitConsole(); } catch (e) {}
        check();
        window.setInterval(check, POLL_MS);
        window.addEventListener('resize', function () { setTimeout(check, 120); });
        document.addEventListener('visibilitychange', check);
        window.addEventListener('focus', check);
        /* Media started while the gate is up must not play. */
        document.addEventListener('play', function (e) {
            if (gateOpen && e.target && e.target.pause) {
                try { e.target.pause(); } catch (err) {}
            }
        }, true);
        /* The usual ways in. F12 and the inspector shortcuts are refused, and
           so is saving or viewing the source from the keyboard. */
        document.addEventListener('keydown', function (e) {
            var key = (e.key || '').toLowerCase();
            var blocked = key === 'f12'
                || (e.ctrlKey && e.shiftKey && (key === 'i' || key === 'j' || key === 'c' || key === 'k' || key === 'm'))
                || ((e.ctrlKey || e.metaKey) && (key === 'u' || key === 's'));
            if (blocked) {
                e.preventDefault();
                e.stopPropagation();
            }
        }, true);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
