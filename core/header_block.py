"""Header (User-Agent) blocking for the public site.

An IP block answers one address; a header block answers the *program*, from
wherever it runs. A crawler that announces itself in its User-Agent — ClaudeBot,
GPTBot and the rest — is refused the moment it appears, even from an address
that has never been seen before, and even when it changes address for every
request. That is the advice the search engines and the AI vendors themselves
give for opting out of this kind of traffic, except that a rule here is enforced
rather than merely asked for.

The rule value is matched case-insensitively anywhere inside the header, so
``ClaudeBot`` covers ``ClaudeBot/1.0 (+https://claude.ai/...)`` and every later
version of it. A rule names any request header, defaulting to ``User-Agent``.

A rule can also be published in robots.txt (``robots_disallow``), which is the
polite half of the same decision: crawlers that honour robots.txt stay away, and
the ones that do not are read by the very same rule and answered 403.

Nothing here can lock the operator out or break the app: the middleware applies
header rules only to non-staff traffic, and never to the dashboard, the block
page's own sign-in/registration endpoints, or the app's ``/api/user/`` calls.

``BlockedHeader`` rows live on the default (sqlite) database, like RequestLog
and BlockedIP, so a rule is local to the portal and never touches the user DB.
"""

import logging
import re
from collections import Counter

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

BLOCKED_HEADER_STATUS = 403
DEFAULT_HEADER = 'User-Agent'
# Header names spelled as HTTP spells them, so a rule always reads the same in
# the UI whichever way the admin typed it.
CANONICAL_HEADERS = {
    'user-agent': 'User-Agent', 'referer': 'Referer', 'referrer': 'Referer',
    'accept': 'Accept', 'accept-language': 'Accept-Language',
    'accept-encoding': 'Accept-Encoding', 'cookie': 'Cookie',
    'authorization': 'Authorization', 'x-requested-with': 'X-Requested-With',
    'content-type': 'Content-Type',
}
# A rule has to be specific enough to mean something: 'a' would refuse the
# whole site. Anything shorter than this is rejected by normalize_value().
MIN_VALUE_LENGTH = 3
MAX_VALUE_LENGTH = 400
MAX_HEADER_NAME = 100
MAX_RULES = 500
MAX_ACTIVITY_ROWS = 20000
# Counting a rule's refusals means walking the retained request log once; with
# many rules that walk gets long, so only the newest few are counted and the
# rest are shown without a count.
MAX_COUNTED_RULES = 50
# The two headers every request log row keeps, so only those can be counted
# without a write on every single refused request. Maps the lowercased header
# name to the RequestLog field that holds it.
LOGGED_HEADER_FIELDS = {'user-agent': 'user_agent', 'referer': 'referer'}
# Common header names, offered as suggestions in the admin form.
COMMON_HEADERS = ('User-Agent', 'Referer', 'Accept-Language', 'X-Requested-With')

# Crawlers that announce themselves and that the operator most often wants gone.
# Offered as one-click suggestions; nothing is blocked until the admin clicks.
SUGGESTED_RULES = (
    ('ClaudeBot', 'Anthropic crawler'),
    ('GPTBot', 'OpenAI crawler'),
    ('CCBot', 'Common Crawl'),
    ('Google-Extended', 'Google AI training'),
    ('PerplexityBot', 'Perplexity crawler'),
    ('Bytespider', 'ByteDance crawler'),
    ('Amazonbot', 'Amazon crawler'),
    ('meta-externalagent', 'Meta crawler'),
    ('Applebot-Extended', 'Apple AI training'),
    ('Bingbot', 'Bing (also indexes for search)'),
)

# The rule list is cached per worker for a moment so a busy site does not read
# it from the database on every request. Only a non-empty list is cached: while
# there are no rules (the usual case) every request looks, so the very first
# rule takes effect everywhere at once instead of waiting out a stale empty
# list. Nothing is cached for long, because lifting a rule should stop the
# refusals promptly on every worker, not just the one the admin touched.
CACHE_KEY = 'blocked_headers_active'
CACHE_SECONDS = 15

_TOKEN_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{1,}$')


def normalize_header_name(value):
    """A clean header name; anything empty or unusable falls back to User-Agent.

    Known names are spelled the way HTTP spells them, so 'user-agent' and
    'User-Agent' cannot end up as two rules that look like one.
    """
    name = re.sub(r'[^A-Za-z0-9-]', '', (value or '').strip())[:MAX_HEADER_NAME]
    if not name:
        return DEFAULT_HEADER
    return CANONICAL_HEADERS.get(name.lower(), name)


def normalize_value(value):
    """The stored form of a rule value: single-spaced, trimmed, length-capped.

    Returns '' for a value that cannot be a rule — empty, too short to be
    specific, or carrying a control character (a newline in a header is an
    injection attempt, not a rule).
    """
    raw = (value or '').strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return ''
    text = ' '.join(raw.split())
    if len(text) < MIN_VALUE_LENGTH:
        return ''
    return text[:MAX_VALUE_LENGTH]


def _cache_clear():
    try:
        cache.delete(CACHE_KEY)
    except Exception:
        logger.debug('HeaderBlock: could not clear the rule cache', exc_info=True)


def active_rules():
    """The active rules as plain dicts, cached for a moment to spare each request.

    A dict list (not model instances) so the cache stays cheap, and the cache is
    cleared the instant a rule is added, lifted or published, so an admin change
    takes effect on the very next request.
    """
    from .models import BlockedHeader
    try:
        cached = cache.get(CACHE_KEY)
    except Exception:
        cached = None
    if cached:
        return cached
    try:
        rules = list(BlockedHeader.objects.filter(is_active=True)
                     .order_by('-blocked_at')[:MAX_RULES]
                     .values('id', 'header_name', 'value', 'reason',
                             'robots_disallow'))
    except Exception:
        logger.debug('HeaderBlock: could not load the rules', exc_info=True)
        return []
    for rule in rules:
        rule['needle'] = rule['value'].lower()
        rule['header_key'] = (rule['header_name'] or DEFAULT_HEADER).lower()
    if rules:
        try:
            cache.set(CACHE_KEY, rules, CACHE_SECONDS)
        except Exception:
            pass
    return rules


def header_rules_for_request(request):
    """The active rules this request matches, newest first (usually empty)."""
    rules = active_rules()
    if not rules:
        return []
    try:
        headers = request.headers
    except Exception:
        return []
    matched = []
    for rule in rules:
        try:
            raw = headers.get(rule['header_key'], '') or ''
        except Exception:
            continue
        if rule['needle'] and rule['needle'] in raw.lower():
            matched.append(rule)
    return matched


def rule_for_request(request):
    """The first (newest) active rule this request matches, or None."""
    matched = header_rules_for_request(request)
    return matched[0] if matched else None


def normalize_path(request):
    """True when header rules may apply to this path at all.

    The app's own account endpoints are left alone: one rule that happens to
    match a phone client's User-Agent must never be able to take the mobile app
    down for every user behind it.
    """
    try:
        path = getattr(request, 'path', '') or ''
    except Exception:
        return True
    return not path.startswith('/api/user/')


def rule_for_value(raw, header_name=DEFAULT_HEADER):
    """The active rule a header value would match, or None.

    Used for data the portal already holds — the User-Agent recorded on a
    blocked-IP row, for instance — so the pages can show that a value is already
    covered by a rule instead of offering to block it twice.
    """
    if not raw:
        return None
    key = (header_name or DEFAULT_HEADER).lower()
    lower = str(raw).lower()
    for rule in active_rules():
        if rule['header_key'] != key:
            continue
        if rule['needle'] and rule['needle'] in lower:
            return rule
    return None


def active_blocked_headers():
    """{id: row} for every active rule (for UI badges)."""
    try:
        from .models import BlockedHeader
        return {row.id: row for row in BlockedHeader.objects.filter(is_active=True)}
    except Exception:
        logger.debug('HeaderBlock: could not load the active rules', exc_info=True)
        return {}


def block_header(value, header_name=DEFAULT_HEADER, blocked_by='', reason='',
                 robots_disallow=False):
    """Add (or switch back on) a header rule. Returns ``(row, created)``.

    Returns ``(None, False)`` when the value cannot be a rule, so the admin gets
    an explanation instead of a rule that matches everything.
    """
    name = normalize_header_name(header_name)
    text = normalize_value(value)
    if not text:
        return None, False
    from .models import BlockedHeader
    row = BlockedHeader.objects.filter(header_name=name, value=text).first()
    now = timezone.now()
    if row is None:
        row = BlockedHeader.objects.create(
            header_name=name, value=text, reason=(reason or '')[:300],
            blocked_by=(blocked_by or '')[:150], blocked_at=now, is_active=True,
            robots_disallow=bool(robots_disallow),
        )
        _cache_clear()
        logger.info('HeaderBlock: blocked %s matching %r', name, text)
        return row, True
    row.is_active = True
    row.blocked_at = now
    row.blocked_by = (blocked_by or '')[:150]
    if reason:
        row.reason = reason[:300]
    row.unblocked_at = None
    row.unblocked_by = ''
    if robots_disallow:
        row.robots_disallow = True
    row.save(update_fields=['is_active', 'blocked_at', 'blocked_by', 'reason',
                            'unblocked_at', 'unblocked_by', 'robots_disallow'])
    _cache_clear()
    return row, False


def unblock_header(rule_id, unblocked_by=''):
    """Switch a rule off (its row and history stay). Returns the row or None."""
    try:
        from .models import BlockedHeader
        row = BlockedHeader.objects.filter(id=rule_id, is_active=True).first()
    except Exception:
        return None
    if row is None:
        return None
    row.is_active = False
    row.unblocked_at = timezone.now()
    row.unblocked_by = (unblocked_by or '')[:150]
    row.save(update_fields=['is_active', 'unblocked_at', 'unblocked_by'])
    _cache_clear()
    logger.info('HeaderBlock: lifted the rule on %s matching %r', row.header_name, row.value)
    return row


def delete_header_rule(rule_id):
    """Remove a rule outright (for a typo nobody needs in the history)."""
    try:
        from .models import BlockedHeader
        deleted, _ = BlockedHeader.objects.filter(id=rule_id).delete()
    except Exception:
        return 0
    if deleted:
        _cache_clear()
    return deleted


def set_robots_disallow(rule_id, disallow=True, by=''):
    """Publish (or stop publishing) a rule in robots.txt. Returns the row or None."""
    try:
        from .models import BlockedHeader
        row = BlockedHeader.objects.filter(id=rule_id).first()
    except Exception:
        return None
    if row is None:
        return None
    row.robots_disallow = bool(disallow)
    row.save(update_fields=['robots_disallow'])
    _cache_clear()
    logger.info('HeaderBlock: %s published %s: %r in robots.txt',
                by or 'admin', 'is' if disallow else 'is no longer', row.value)
    return row


def _robots_token(value):
    """The ``User-agent:`` token robots.txt needs, or '' when there is not one.

    A robots.txt line names an exact agent token, so a rule value is cut at its
    first space, slash or semicolon: ``ClaudeBot/1.0 (+https://...)`` publishes
    as ``ClaudeBot``. Values that do not look like a token (a long regex, a
    sentence) are skipped rather than published broken.
    """
    text = (value or '').strip()
    if not text:
        return ''
    token = re.split(r'[\s/;,()]', text, 1)[0]
    if len(token) < MIN_VALUE_LENGTH or not _TOKEN_RE.match(token):
        return ''
    # What follows the token says whether it is an agent name or the first word
    # of a sentence: 'ClaudeBot', 'ClaudeBot/1.0' and 'ClaudeBot (comment)' are
    # agent names, 'long sentence of prose' is not and is not published.
    rest = text[len(token):].lstrip()
    if rest and rest[0] not in '/(;':
        return ''
    return token


def robots_disallow_lines():
    """robots.txt lines for the User-Agent rules the operator published."""
    lines = []
    seen = set()
    for rule in active_rules():
        if not rule['robots_disallow']:
            continue
        if rule['header_key'] != 'user-agent':
            continue
        token = _robots_token(rule['value'])
        if not token or token.lower() in seen:
            continue
        seen.add(token.lower())
        lines.append('User-agent: ' + token)
        lines.append('Disallow: /')
        lines.append('')
    return lines


def blocked_header_report(include_inactive=False, search=''):
    """Rules with the requests they refused: ``(rows, totals)``.

    Counts come from the retained request log (the same rows the Requests &
    Users page shows): for each logged request, every rule whose header it
    carries and that answered a 403 is one refusal. A rule on a header the log
    does not keep cannot be counted that way and is shown without a number.
    """
    from .models import BlockedHeader, RequestLog

    qs = BlockedHeader.objects.all()
    if not include_inactive:
        qs = qs.filter(is_active=True)
    needle = (search or '').strip()
    if needle:
        from django.db.models import Q
        qs = qs.filter(Q(value__icontains=needle) | Q(header_name__icontains=needle))
    rules = list(qs.order_by('-is_active', '-blocked_at')[:MAX_RULES])

    rows = []
    counted = []
    for index, rule in enumerate(rules):
        header_key = (rule.header_name or DEFAULT_HEADER).lower()
        log_field = LOGGED_HEADER_FIELDS.get(header_key, '')
        counted_here = index < MAX_COUNTED_RULES and bool(log_field)
        summary = {
            'obj': rule,
            'id': rule.id,
            'header_name': rule.header_name or DEFAULT_HEADER,
            'header_key': header_key,
            'value': rule.value,
            'reason': rule.reason,
            'blocked_by': rule.blocked_by,
            'blocked_at': rule.blocked_at,
            'unblocked_at': rule.unblocked_at,
            'unblocked_by': rule.unblocked_by,
            'is_active': rule.is_active,
            'robots_disallow': rule.robots_disallow,
            'robots_token': _robots_token(rule.value),
            'refused': 0,
            'last_refused': None,
            'first_refused': None,
            'ips': Counter(),
            'paths': Counter(),
            'counted': counted_here,
            'log_field': log_field,
        }
        rows.append(summary)
        if counted_here:
            counted.append(summary)

    if counted:
        rules_by_field = {}
        for summary in counted:
            rules_by_field.setdefault(summary['log_field'], []).append(summary)
        activity = (RequestLog.objects
                    .filter(status_code=BLOCKED_HEADER_STATUS)
                    .values('user_agent', 'referer', 'client_ip', 'path', 'created_at')
                    .order_by('-created_at')[:MAX_ACTIVITY_ROWS])
        for rec in activity:
            when = rec['created_at']
            for field, summaries in rules_by_field.items():
                raw = (rec[field] or '')
                if not raw:
                    continue
                lower = raw.lower()
                for summary in summaries:
                    needle_lower = summary['value'].lower()
                    if not needle_lower or needle_lower not in lower:
                        continue
                    if when < summary['blocked_at']:
                        continue
                    if summary['unblocked_at'] and when > summary['unblocked_at']:
                        continue
                    summary['refused'] += 1
                    if summary['first_refused'] is None or when < summary['first_refused']:
                        summary['first_refused'] = when
                    if summary['last_refused'] is None or when > summary['last_refused']:
                        summary['last_refused'] = when
                    if rec['client_ip']:
                        summary['ips'][rec['client_ip']] += 1
                    if rec['path']:
                        summary['paths'][rec['path']] += 1

    for summary in rows:
        summary['ip_total'] = len(summary['ips'])
        summary['path_total'] = len(summary['paths'])
        summary['top_paths'] = [p for p, _ in summary['paths'].most_common(3)]
        summary['top_ips'] = [i for i, _ in summary['ips'].most_common(3)]

    totals = {
        'rules': sum(1 for s in rows if s['is_active']),
        'off': sum(1 for s in rows if not s['is_active']),
        'shown': len(rows),
        'refused': sum(s['refused'] for s in rows),
        'published': sum(1 for s in rows if s['is_active'] and s['robots_token']
                         and s['robots_disallow']),
        'uncounted': sum(1 for s in rows if not s['counted']),
    }
    return rows, totals
