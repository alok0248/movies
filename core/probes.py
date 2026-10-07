"""Traffic that asks for pages this site does not have — and the scrapers behind it.

The request log already records the status of every request, so a page the site
does not have is simply a row answered 404. On top of that, some paths are never
part of a real visit (``/.env``, ``/wp-login.php``, framework files, exploit
probes): those are matched by marker even when the response was not a 404.

This module turns that raw log into an answer to "which IP is trying to reach
pages that are not on the website": one row per address with how many such pages
it asked for and exactly which ones, plus the signals that make it look like a
scraper (no user agent at all, a command-line/crawler client, probe paths, or a
request rate no human produces).

The Blocked IPs page and the ``block_scrapers`` management command both build on
this report, so the check can be looked at by hand or run periodically.
"""

import logging
from collections import Counter
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# A page the site does not have answers 404; that is the main "not on the
# website" signal, and it is what the request log records.
NOT_FOUND_STATUS = 404

# Paths that real users never browse to: exploit probes and framework files.
# Kept identical to the active-users list in core/views.py so the Probes page and
# the Active Users "bot" column agree on what a scanner looks like.
PROBE_PATH_MARKERS = (
    '/index.php', '/wp-', '/.env', '/.git', '/vendor/', '/phpunit',
    '/xmlrpc.php', '/admin.php', '/config.php', '/cgi-bin/', '/shell',
    '/actuator', '/docker', '/credentials', '/.aws', '/.ssh', '/boaform',
    '/hudson', '/solr/', '/jenkins', '/telescope', '/containers/json',
    '/sdk/', '/userportal/', '/_ignition', '/.vscode', '/.idea', '/.svn',
    '/.htaccess', '/.htpasswd', '/.ds_store', '/server-status', '/phpinfo',
    '/info.php', '/test.php', '/db.php', '/owa/', '/autodiscover', '/cgi',
    '/gponform', '/.dockerenv', '/wp-content',
)

# Signatures of scanners, crawlers and command-line clients that hammer the
# site without ever being a real visitor.
SCRAPER_UA_MARKERS = (
    'bot', 'crawler', 'spider', 'scrapy', 'curl', 'wget', 'python-requests',
    'python-urllib', 'go-http-client', 'java/', 'okhttp', 'libwww', 'axios',
    'headlesschrome', 'phantomjs', 'masscan', 'zgrab', 'nmap', 'nuclei',
    'l9explore', 'expanse', 'censys', 'shodan', 'internetmeasurement',
)

# Pages per hour above which an address looks like a scraper rather than a
# person reading the site.
SCRAPER_HITS_PER_HOUR = 60

MAX_REPORT_ROWS = 400
MAX_ACTIVITY_ROWS = 20000
MAX_PATHS_PER_IP = 60
MAX_TOP_PATHS = 40


def is_probe_path(path):
    """True when a path is one only a scanner would ever ask for."""
    p = (path or '').lower()
    if not p:
        return False
    return any(marker in p for marker in PROBE_PATH_MARKERS)


def is_scraper_agent(user_agent):
    """True when the client announces itself as a crawler or a script."""
    ua = (user_agent or '').strip().lower()
    if not ua:
        return False
    return any(marker in ua for marker in SCRAPER_UA_MARKERS)


def signals_for(has_probe_paths, user_agent, requests, hours):
    """The reasons an address looks automated, as short human labels."""
    signals = []
    ua = (user_agent or '').strip()
    if not ua:
        signals.append('no user agent')
    elif is_scraper_agent(ua):
        signals.append('scraper client')
    if has_probe_paths:
        signals.append('probe paths')
    try:
        if hours and (requests / float(hours)) >= SCRAPER_HITS_PER_HOUR:
            signals.append('high rate')
    except Exception:
        pass
    return signals


def probe_report(hours=24, min_hits=1, hide_blocked=False, search=''):
    """Which addresses asked for pages the site does not have.

    Returns ``(rows, totals, top_paths)``. One row per IP with its unknown-page
    hits (404s and probe paths), the paths it asked for, its request rate over
    the window and the bot signals; plus the most requested unknown paths overall.
    Counts come from the retained request log (newest ``request_log.MAX_ROWS``
    requests), which is the same window the Request Log page shows.
    """
    from .models import RequestLog
    from .ip_block import active_blocked_ips

    try:
        hours = max(1, min(int(hours or 24), 24 * 30))
    except (TypeError, ValueError):
        hours = 24
    try:
        min_hits = max(1, int(min_hits or 1))
    except (TypeError, ValueError):
        min_hits = 1
    needle = (search or '').strip().lower()
    since = timezone.now() - timedelta(hours=hours)

    try:
        records = list(RequestLog.objects
                       .filter(created_at__gte=since)
                       .values('client_ip', 'path', 'status_code', 'created_at',
                               'user_agent', 'method')[:MAX_ACTIVITY_ROWS])
    except Exception:
        logger.debug('probes: request-log query failed', exc_info=True)
        records = []

    blocked = active_blocked_ips()
    by_ip = {}
    path_stats = {}

    for rec in records:
        ip = (rec['client_ip'] or '').strip()
        path = rec['path'] or '/'
        ua = rec['user_agent'] or ''
        probe_path = is_probe_path(path)
        unknown = rec['status_code'] == NOT_FOUND_STATUS or probe_path
        when = rec['created_at']

        if unknown:
            stat = path_stats.get(path)
            if stat is None:
                stat = path_stats[path] = {'path': path, 'count': 0, 'ips': set(),
                                           'last': None, 'probe': probe_path}
            stat['count'] += 1
            stat['last'] = when if stat['last'] is None or when > stat['last'] else stat['last']
            if ip:
                stat['ips'].add(ip)

        if not ip:
            # No address to attribute (e.g. a local health probe) — the path
            # stats above still record the attempt.
            continue

        entry = by_ip.get(ip)
        if entry is None:
            entry = by_ip[ip] = {
                'ip': ip, 'requests': 0, 'unknown': 0, 'probes': 0, 'not_found': 0,
                'paths': {}, 'first_seen': None, 'last_seen': None, 'user_agent': '',
                'methods': Counter(), 'probe_path_hits': 0,
            }
        entry['requests'] += 1
        entry['methods'][(rec['method'] or '').upper()] += 1
        if entry['first_seen'] is None or when < entry['first_seen']:
            entry['first_seen'] = when
        if entry['last_seen'] is None or when > entry['last_seen']:
            entry['last_seen'] = when
            if ua:
                entry['user_agent'] = ua
        if not unknown:
            continue
        entry['unknown'] += 1
        if probe_path:
            entry['probes'] += 1
            entry['probe_path_hits'] += 1
        if rec['status_code'] == NOT_FOUND_STATUS:
            entry['not_found'] += 1
        item = entry['paths'].get(path)
        if item is None:
            item = entry['paths'][path] = {'path': path, 'count': 0, 'last': None,
                                           'status': rec['status_code']}
        item['count'] += 1
        item['status'] = rec['status_code']
        item['last'] = when if item['last'] is None or when > item['last'] else item['last']

    rows = []
    for entry in by_ip.values():
        if entry['unknown'] < min_hits:
            continue
        if needle and needle not in entry['ip'].lower():
            continue
        is_blocked = entry['ip'] in blocked
        if hide_blocked and is_blocked:
            continue
        paths = sorted(entry['paths'].values(), key=lambda p: (-p['count'], p['path']))
        entry['paths'] = paths[:MAX_PATHS_PER_IP]
        entry['path_total'] = len(paths)
        entry['blocked'] = is_blocked
        entry['per_hour'] = round(entry['requests'] / float(hours), 1)
        entry['signals'] = signals_for(entry['probe_path_hits'], entry['user_agent'],
                                       entry['requests'], hours)
        rows.append(entry)

    rows.sort(key=lambda r: (-r['unknown'], -(r['last_seen'].timestamp() if r['last_seen'] else 0)))

    totals = {
        'ips': len(rows),
        'unknown': sum(r['unknown'] for r in rows),
        'probes': sum(r['probes'] for r in rows),
        'not_found': sum(r['not_found'] for r in rows),
        'blocked': sum(1 for r in rows if r['blocked']),
        'requests': sum(r['requests'] for r in rows),
        'distinct_paths': len(path_stats),
        'suspicious': sum(1 for r in rows if r['signals'] and not r['blocked']),
        'hours': hours,
    }

    top_paths = sorted(path_stats.values(), key=lambda p: (-p['count'], p['path']))[:MAX_TOP_PATHS]
    for stat in top_paths:
        stat['ips'] = len(stat['ips'])
    return rows, totals, top_paths


def suspicious_ips(hours=24, min_hits=20, include_blocked=False):
    """Addresses worth blocking: plenty of unknown-page requests and looking automated.

    Returns the report rows that hit at least ``min_hits`` unknown pages and carry
    at least one bot signal. Used by the ``block_scrapers`` management command, so
    the decision to block is reviewable (and dry-run by default).
    """
    rows, _, _ = probe_report(hours=hours, min_hits=min_hits)
    picked = []
    for row in rows:
        if row['blocked'] and not include_blocked:
            continue
        if not row['signals']:
            continue
        picked.append(row)
    return picked
