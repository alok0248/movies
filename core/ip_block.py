"""IP blocking for the public site.

The admin blocks an address straight from the Requests & Users page (or from
the Blocked IPs page). From then on every public request from that address is
answered with a sign-in / sign-up page instead of the site — and the request is
*still* written to RequestLog, because the blocking middleware sits inside
RequestLogMiddleware. That is what makes the Blocked IPs page possible: it can
show how many requests the address kept making after the block and exactly
which pages it asked for.

A blocked visitor is not sealed off: the block page shows the login and
registration forms, and signing in or registering lifts a normal block right
away — that is the way out a real visitor has, and the page warns that scraping
again will block the address permanently. A block becomes permanent when the
address is blocked again after such a lift, when an address that is already
signed in crosses the bot rate, or when the admin marks it permanent.

Nothing is left to the admin's eye alone either: a rolling per-address rate
watch blocks an address that asks for far more pages than a person could, which
is what a scraper does when it starts from a fresh address every time.

Safety: staff / superuser requests and the Django admin are never blocked, and
a blocked address is kept out of the visitor tables, so a mistyped block can
never lock the operator out of the dashboard.

``BlockedIP`` rows live on the default (sqlite) database, like RequestLog, so a
blocking rule is local to the portal and never touches the external user DB.
"""

import ipaddress
import logging
import time
from collections import Counter

from django.http import HttpResponseForbidden
from django.utils import timezone

logger = logging.getLogger(__name__)

# Status returned to a blocked address. Using a real 403 (rather than a silent
# redirect) also marks the rows in the request log, so the Blocked IPs page can
# show how many requests the address kept making after the block.
BLOCKED_STATUS = 403

# Endpoints a blocked visitor must still be able to reach, otherwise signing in
# (the way out of the block) would be impossible: the site's own auth calls, for
# both the website and the Android app.
AUTH_EXEMPT_PREFIXES = (
    '/login', '/logout',
    '/ajax/login', '/ajax/logout', '/ajax/register', '/ajax/verify',
    '/ajax/resend-verification', '/ajax/forgot-password', '/ajax/reset-password',
    '/api/user/login', '/api/user/register', '/api/user/verify-email',
    '/api/user/resend-verification', '/api/user/forgot-password',
    '/api/user/reset-password', '/api/user/verify-reset-otp',
)

# --- bot-rate watch -------------------------------------------------------
# A visitor asking for this many non-user-API requests inside the window looks
# like a scraper rather than a person, and is blocked as a bot. Tuned where a
# genuine visitor cannot reach it: 150 requests in 5 minutes is 30 a minute,
# sustained. The app's own sync endpoints are excluded because mobile carriers
# put many real devices behind one address — blocking one of those would take
# out thousands of genuine users.
BOT_WINDOW_SECONDS = 300
BOT_MAX_REQUESTS = 150
# Paths that never count towards the bot rate (the app's user-data polling).
BOT_RATE_SKIP_PREFIXES = (
    '/api/user/', '/ajax/page-activity', '/static/', '/media/',
)
# Keep the in-process watch bounded; oldest addresses are dropped first.
BOT_WATCH_MAX_IPS = 4000
_bot_watch = {}


def should_count_for_bot_rate(path):
    """False for chatty app endpoints and assets that are not scraping."""
    p = path or ''
    if not p or p.startswith(BOT_RATE_SKIP_PREFIXES):
        return False
    # The admin dashboard is the operator's own traffic, never a bot.
    return not (p.startswith('/admin') or p.startswith('/django-admin'))


def note_request(ip_address, now=None):
    """Count one request for this address; True when it crosses the bot limit.

    A small in-process rolling window per worker. It only has to be accurate
    enough to notice a scraper: once an address is blocked the block is in the
    database, so every worker enforces it immediately.
    """
    from collections import deque
    ip = normalize_ip(ip_address)
    if not ip:
        return False
    now = now if now is not None else time.monotonic()
    if len(_bot_watch) > BOT_WATCH_MAX_IPS:
        # Cheap eviction: drop the oldest inserted entries.
        for old in list(_bot_watch)[:BOT_WATCH_MAX_IPS // 4]:
            _bot_watch.pop(old, None)
    window = _bot_watch.get(ip)
    if window is None:
        window = _bot_watch[ip] = deque()
    cutoff = now - BOT_WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()
    window.append(now)
    if len(window) < BOT_MAX_REQUESTS:
        return False
    # Over the limit: forget this window so the check starts clean after the
    # block is lifted again.
    _bot_watch.pop(ip, None)
    return True


def reset_bot_watch(ip_address=None):
    """Clear the rolling window (all addresses, or just one)."""
    if ip_address is None:
        _bot_watch.clear()
        return
    _bot_watch.pop(normalize_ip(ip_address), None)

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


def client_ip_of(request):
    """The canonical client IP of a request, or '' — same first-hop rule as the
    request log (X-Forwarded-For first, then REMOTE_ADDR)."""
    try:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        raw = forwarded.split(',')[0] if forwarded else request.META.get('REMOTE_ADDR', '')
    except Exception:
        return ''
    return normalize_ip(raw)


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


def block_ip(ip_address, blocked_by='', reason='', permanent=False):
    """Block an address (or re-block one that was previously unblocked).

    Returns ``(row, created)``; reusing the row keeps the reason/history and
    resets ``blocked_at``, which is the cut-off the activity report counts from.
    Returns ``(None, False)`` when the address is not valid.

    A fresh block on an address that was already blocked before escalates:
    ``offense_count`` goes up, and if the previous block had been lifted by the
    visitor signing in — i.e. they were warned — the new block is permanent.
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
            is_permanent=bool(permanent),
            offense_count=1,
        )
        return row, True

    was_blocked = row.is_active
    if not was_blocked:
        # A new block on an address we have blocked before, not a repeat call
        # while it is already blocked.
        row.offense_count = max(1, (row.offense_count or 1) + 1)
        if row.lifted_by_signup:
            permanent = True
    row.is_active = True
    row.blocked_at = now
    row.blocked_by = (blocked_by or '')[:150]
    if reason:
        row.reason = reason[:300]
    row.unblocked_at = None
    row.unblocked_by = ''
    row.lifted_by_signup = False
    row.is_permanent = bool(row.is_permanent or permanent)
    row.save(update_fields=['is_active', 'blocked_at', 'blocked_by', 'reason',
                            'unblocked_at', 'unblocked_by', 'is_permanent',
                            'offense_count', 'lifted_by_signup'])
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
    reset_bot_watch(ip)
    return row


def lift_block_for_signup(ip_address):
    """Lift a non-permanent block because a visitor signed in or registered.

    This is the way out the block page offers. The row is marked so a later
    block on the same address is escalated to permanent (the warning the page
    shows). A permanent block is never lifted here. Returns the row or None.
    """
    ip = normalize_ip(ip_address)
    if not ip:
        return None
    from .models import BlockedIP
    row = BlockedIP.objects.filter(ip_address=ip, is_active=True).first()
    if row is None or row.is_permanent:
        return None
    row.is_active = False
    row.unblocked_at = timezone.now()
    row.unblocked_by = 'signup'
    row.lifted_by_signup = True
    row.offense_count = max(1, row.offense_count or 1)
    row.save(update_fields=['is_active', 'unblocked_at', 'unblocked_by',
                            'lifted_by_signup', 'offense_count'])
    reset_bot_watch(ip)
    logger.info('IPBlock: lifted the block on %s after a signup/login', ip)
    return row


def note_auth_success(request):
    """Lift a normal block because this request just signed in or registered.

    Called by the sign-in / registration views once they succeed, so a blocked
    visitor who follows the block page's way out is unblocked straight away.
    Returns the lifted row, or None when the address was not blocked (or the
    block is permanent, which is never lifted this way).
    """
    ip = client_ip_of(request)
    if not ip:
        return None
    row = blocked_ip_for(ip)
    if row is None:
        return None
    if row.is_permanent:
        logger.info('IPBlock: ignored a signup from permanently blocked %s', ip)
        return None
    return lift_block_for_signup(ip)


def auto_block_bot(ip_address, signed_in=False, requests=None):
    """Block an address that crossed the bot rate, as a bot.

    An address that crossed the limit while *signed in* is a repeat offender by
    definition — it already had the access the block page offers — so that block
    is permanent. An anonymous one gets the normal way out (register or sign in)
    plus the warning.
    """
    ip = normalize_ip(ip_address)
    if not ip:
        return None, False
    detail = ('%s requests inside %s minutes' % (requests or BOT_MAX_REQUESTS,
                                                BOT_WINDOW_SECONDS // 60))
    if signed_in:
        detail += ' while signed in'
    return block_ip(ip, 'auto:bot-detector',
                    'looks like a bot: ' + detail,
                    permanent=bool(signed_in))


def is_bot_block(row):
    """True when an auto-bot block (rather than a hand-made one) made this row."""
    return bool(row is not None and (row.blocked_by or '').startswith('auto:bot'))


def set_permanent(ip_address, permanent=True, by=''):
    """Make a block permanent (or allow signup to lift it again)."""
    ip = normalize_ip(ip_address)
    if not ip:
        return None
    from .models import BlockedIP
    row = BlockedIP.objects.filter(ip_address=ip, is_active=True).first()
    if row is None:
        return None
    row.is_permanent = bool(permanent)
    if not permanent:
        row.lifted_by_signup = False
    row.save(update_fields=['is_permanent', 'lifted_by_signup'])
    logger.info('IPBlock: %s marked %s by %s', ip,
                'permanent' if permanent else 'temporary', by or 'admin')
    return row


def blocked_response(request, ip_address='', row=None, header_rule=None):
    """The 403 page a blocked request receives.

    A blocked visitor sees one thing only: the sign-in form and the registration
    form, with a short note saying the address is blocked and why. Signing in or
    registering is the way out — either one lifts a normal block immediately —
    and the page carries the warning that scraping again means a permanent
    block. The auth entry points the forms post to stay reachable
    (see ``is_auth_entry``).

    When the refusal came from a header rule instead (``header_rule``), the page
    names the header and the value that matched and offers no way in: a rule
    names a program, so signing in would not change the answer.
    """
    ip = normalize_ip(ip_address) or client_ip_of(request)
    if row is None:
        row = blocked_ip_for(ip)
    permanent = bool(row is not None and row.is_permanent)
    reason = (row.reason if row is not None else '') or ''
    auto_bot = is_bot_block(row)
    rule = header_rule or None
    if rule is not None:
        reason = rule.get('reason') or ''
    try:
        user_agent = (request.headers.get('User-Agent', '') or '')[:300]
    except Exception:
        user_agent = ''
    try:
        from django.shortcuts import render
        response = render(request, 'core/ip_blocked.html', {
            'blocked_ip': ip,
            'permanent': permanent,
            'reason': reason[:300],
            'auto_bot': auto_bot,
            'header_rule': rule,
            'header_name': (rule or {}).get('header_name', ''),
            'header_value': (rule or {}).get('value', ''),
            'request_user_agent': user_agent,
        }, status=BLOCKED_STATUS)
    except Exception:
        logger.warning('IPBlock: falling back to the plain block page', exc_info=True)
        response = HttpResponseForbidden(
            '<!doctype html><meta name="robots" content="noindex">'
            '<title>403 - Access blocked</title>'
            '<p>This IP address is blocked. Sign in or register to continue.'
            ' Scraping again will block this address permanently.</p>'
        )
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
            # Escalation state: a permanent block (or one the visitor was
            # warned about and repeated anyway) and how often this address has
            # been blocked at all.
            'is_permanent': bool(row.is_permanent),
            'offense_count': row.offense_count or 1,
            'lifted_by_signup': bool(row.lifted_by_signup),
            'is_bot': is_bot_block(row),
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
        'permanent': sum(1 for s in rows if s['is_permanent'] and s['is_active']),
        'bots': sum(1 for s in rows if s['is_bot'] and s['is_active']),
        'repeat': sum(1 for s in rows if (s['offense_count'] or 1) > 1),
    }
    return rows, totals
