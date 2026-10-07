"""IP blocking for the public site.

The admin blocks an address straight from the Requests & Users page (or from
the Blocked IPs page). From then on every public request from that address
answers 403 — but the request is *still* written to RequestLog, because the
blocking middleware sits inside RequestLogMiddleware. That is what makes the
Blocked IPs page possible: it can show how many requests the address kept
making after the block and exactly which pages it asked for.

Safety: staff / superuser requests and the Django admin are never blocked, and
a blocked address is kept out of the visitor tables, so a mistyped block can
never lock the operator out of the dashboard.

``BlockedIP`` rows live on the default (sqlite) database, like RequestLog, so a
blocking rule is local to the portal and never touches the external user DB.
"""

import ipaddress
import logging
from collections import Counter

from django.http import HttpResponseForbidden
from django.utils import timezone

logger = logging.getLogger(__name__)

# Status returned to a blocked address. Using a real 403 (rather than a silent
# redirect) also marks the rows in the request log, so the Blocked IPs page can
# show how many requests the address kept making after the block.
BLOCKED_STATUS = 403

# Endpoints a blocked visitor must still be able to reach, otherwise signing in
# (the way out of the block) would be impossible: the site's own auth calls.
AUTH_EXEMPT_PREFIXES = (
    '/login', '/logout',
    '/ajax/login', '/ajax/logout', '/ajax/register', '/ajax/verify',
    '/ajax/resend-verification', '/ajax/forgot-password', '/ajax/reset-password',
)

# The auth form lives in the site's home page modal, so the home page stays
# reachable when it is being opened for signing in or registering.
AUTH_MODAL_PARAMS = ('login_required', 'register')


def is_auth_entry(request):
    """True for the site's login/register entry points a blocked visitor needs."""
    try:
        path = getattr(request, 'path', '') or ''
        if path.startswith(AUTH_EXEMPT_PREFIXES):
            return True
        if path in ('/', ''):
            return any(param in request.GET for param in AUTH_MODAL_PARAMS)
    except Exception:
        return False
    return False

# Guard rails so a page render can never blow up on a long block list.
MAX_REPORT_ROWS = 500
MAX_ACTIVITY_ROWS = 20000
MAX_PATHS_PER_IP = 200


def normalize_ip(value):
    """Return a canonical IP string, or '' when the value is not a valid one.

    Only literal IPv4 / IPv6 addresses are accepted — no hostnames, no CIDR
    ranges — so a block always means exactly one address.
    """
    raw = (value or '').strip()
    if not raw:
        return ''
    # An X-Forwarded-For header can arrive as a comma-separated chain; take the
    # first hop, the same address the request log records.
    raw = raw.split(',')[0].strip()
    if raw.startswith('[') and raw.endswith(']'):
        raw = raw[1:-1]
    try:
        return str(ipaddress.ip_address(raw))[:64]
    except (ValueError, TypeError):
        return ''


def active_blocked_ips():
    """{ip: BlockedIP} for every currently blocked address (for UI badges)."""
    try:
        from .models import BlockedIP
        return {row.ip_address: row for row in BlockedIP.objects.filter(is_active=True)}
    except Exception:
        logger.debug('BlockedIP: could not load active blocks', exc_info=True)
        return {}


def blocked_ip_for(ip_address):
    """The active BlockedIP row for this address, or None."""
    ip = normalize_ip(ip_address)
    if not ip:
        return None
    try:
        from .models import BlockedIP
        return BlockedIP.objects.filter(ip_address=ip, is_active=True).first()
    except Exception:
        logger.debug('BlockedIP: lookup failed for %s', ip, exc_info=True)
        return None


def is_ip_blocked(ip_address):
    """True when this address is currently blocked. Never raises."""
    return blocked_ip_for(ip_address) is not None


def block_ip(ip_address, blocked_by='', reason=''):
    """Block an address (or re-block one that was previously unblocked).

    Returns ``(row, created)``; reusing the row keeps the reason/history and
    resets ``blocked_at``, which is the cut-off the activity report counts from.
    Returns ``(None, False)`` when the address is not valid.
    """
    ip = normalize_ip(ip_address)
    if not ip:
        return None, False
    from .models import BlockedIP
    row = BlockedIP.objects.filter(ip_address=ip).first()
    now = timezone.now()
    if row is None:
        row = BlockedIP.objects.create(
            ip_address=ip,
            reason=(reason or '')[:300],
            blocked_by=(blocked_by or '')[:150],
            blocked_at=now,
            is_active=True,
        )
        return row, True
    row.is_active = True
    row.blocked_at = now
    row.blocked_by = (blocked_by or '')[:150]
    if reason:
        row.reason = reason[:300]
    row.unblocked_at = None
    row.unblocked_by = ''
    row.save(update_fields=['is_active', 'blocked_at', 'blocked_by', 'reason',
                            'unblocked_at', 'unblocked_by'])
    return row, False


def unblock_ip(ip_address, unblocked_by=''):
    """Lift the block on an address. Returns the row, or None if not blocked."""
    ip = normalize_ip(ip_address)
    if not ip:
        return None
    from .models import BlockedIP
    row = BlockedIP.objects.filter(ip_address=ip, is_active=True).first()
    if row is None:
        return None
    row.is_active = False
    row.unblocked_at = timezone.now()
    row.unblocked_by = (unblocked_by or '')[:150]
    row.save(update_fields=['is_active', 'unblocked_at', 'unblocked_by'])
    return row


def blocked_response(ip_address=''):
    """The 403 page a blocked address receives.

    It is not a dead end: a blocked visitor is asked to register or sign in, and
    the sign-in/register entry points stay reachable (see ``is_auth_entry``).
    """
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        '<title>403 &mdash; Access blocked</title></head>'
        '<body style="margin:0;min-height:100vh;display:flex;align-items:center;'
        'justify-content:center;background:#0b1020;color:#e2e8f0;'
        'font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif">'
        '<div style="text-align:center;padding:36px 24px;max-width:560px">'
        '<div style="width:64px;height:64px;margin:0 auto 16px;border-radius:18px;'
        'background:linear-gradient(135deg,#ef4444,#f59e0b);display:flex;'
        'align-items:center;justify-content:center;font-size:1.7rem">&#128737;</div>'
        '<div style="font-size:3rem;font-weight:800;letter-spacing:-.03em;'
        'background:linear-gradient(135deg,#f87171,#fbbf24);'
        '-webkit-background-clip:text;background-clip:text;color:transparent">403</div>'
        '<h1 style="font-size:1.15rem;margin:.6rem 0 .5rem">Access blocked</h1>'
        '<p style="color:#94a3b8;font-size:.9rem;line-height:1.65;margin:0 0 1.4rem">'
        'This address was blocked for automated traffic &mdash; for example, repeatedly '
        'asking for pages that do not exist on this site.</p>'
        '<p style="color:#cbd5e1;font-size:.9rem;line-height:1.65;margin:0 0 1.2rem">'
        'If you are a real visitor, create a free account or sign in to continue.</p>'
        '<div style="display:flex;gap:.6rem;justify-content:center;flex-wrap:wrap">'
        '<a href="/?register=true" style="display:inline-block;padding:.6rem 1.2rem;'
        'border-radius:10px;font-weight:600;font-size:.9rem;text-decoration:none;'
        'color:#fff;background:linear-gradient(135deg,#6366f1,#22d3ee)">Create a free account</a>'
        '<a href="/?login_required=true" style="display:inline-block;padding:.6rem 1.2rem;'
        'border-radius:10px;font-weight:600;font-size:.9rem;text-decoration:none;'
        'color:#e2e8f0;border:1px solid rgba(148,163,184,.45)">Sign in</a>'
        '</div>'
        '<p style="color:#64748b;font-size:.78rem;margin:1.4rem 0 0;line-height:1.6">'
        'Already signed in? Reload this page.</p>'
        '</div></body></html>'
    )
    response = HttpResponseForbidden(body)
    response['Cache-Control'] = 'no-store'
    return response


def blocked_ip_report(include_unblocked=False, search=''):
    """Activity summary for the block list: ``(rows, totals)``.

    For every blocked address this counts the requests it has made overall and
    the requests that arrived inside its blocked window (``blocked_at`` to
    ``unblocked_at``, or up to now while the block is active — those are the
    ones that answered 403), and the pages it kept asking for. Counts come from
    the retained request log (newest ``request_log.MAX_ROWS`` requests), which
    is also what the Request Log page shows.
    """
    from .models import BlockedIP, RequestLog

    qs = BlockedIP.objects.all()
    if not include_unblocked:
        qs = qs.filter(is_active=True)
    needle = (search or '').strip()
    if needle:
        qs = qs.filter(ip_address__icontains=needle)
    blocked = list(qs.order_by('-is_active', '-blocked_at')[:MAX_REPORT_ROWS])

    rows = []
    by_ip = {}
    for row in blocked:
        by_ip[row.ip_address] = row
        rows.append({
            'obj': row,
            'ip': row.ip_address,
            'is_active': row.is_active,
            'reason': row.reason,
            'blocked_by': row.blocked_by,
            'blocked_at': row.blocked_at,
            'unblocked_at': row.unblocked_at,
            'unblocked_by': row.unblocked_by,
            'total': 0,
            'after': 0,
            'after_403': 0,
            'paths': {},
            'first_seen': None,
            'last_seen': None,
            'last_after': None,
            'user_agent': '',
            'usernames': Counter(),
        })

    if blocked:
        summaries = {s['ip']: s for s in rows}
        activity = (RequestLog.objects
                    .filter(client_ip__in=list(by_ip.keys()))
                    .values('client_ip', 'path', 'status_code', 'created_at',
                            'username', 'user_agent')[:MAX_ACTIVITY_ROWS])
        for rec in activity:
            summary = summaries.get(rec['client_ip'])
            if summary is None:
                continue
            when = rec['created_at']
            summary['total'] += 1
            if summary['first_seen'] is None or when < summary['first_seen']:
                summary['first_seen'] = when
            if summary['last_seen'] is None or when > summary['last_seen']:
                summary['last_seen'] = when
                if rec['user_agent']:
                    summary['user_agent'] = rec['user_agent']
            row = summary['obj']
            # While the block is active the window is open-ended, so "after"
            # keeps growing; once unblocked it is frozen at unblocked_at.
            if when >= row.blocked_at and (row.unblocked_at is None or when <= row.unblocked_at):
                summary['after'] += 1
                if rec['status_code'] == BLOCKED_STATUS:
                    summary['after_403'] += 1
                if summary['last_after'] is None or when > summary['last_after']:
                    summary['last_after'] = when
                path = rec['path'] or '/'
                entry = summary['paths'].get(path)
                if entry is None:
                    entry = summary['paths'][path] = {'path': path, 'count': 0, 'last': None}
                entry['count'] += 1
                if entry['last'] is None or when > entry['last']:
                    entry['last'] = when
                if rec['username']:
                    summary['usernames'][rec['username']] += 1

    for summary in rows:
        paths = sorted(summary['paths'].values(),
                       key=lambda p: (-p['count'], p['path']))
        summary['paths'] = paths[:MAX_PATHS_PER_IP]
        summary['path_total'] = len(paths)
        summary['top_users'] = [u for u, _ in summary['usernames'].most_common(3)]

    totals = {
        'blocked': sum(1 for s in rows if s['is_active']),
        'shown': len(rows),
        'after': sum(s['after'] for s in rows),
        'active_after': sum(1 for s in rows if s['is_active'] and s['after']),
        'served_403': sum(s['after_403'] for s in rows),
        'requests': sum(s['total'] for s in rows),
        'unblocked': sum(1 for s in rows if not s['is_active']),
    }
    return rows, totals
