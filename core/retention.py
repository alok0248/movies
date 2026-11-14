"""Retention cohorts and funnels, computed from rows the site already keeps.

Nothing new is recorded for this: the signup date is ``SyncedUser.created_at``,
activity is ``PlayHistory``, the install is ``ApkDownload`` and the plan change
is ``SubscriptionEvent``. That keeps the report cheap and makes it honest about
what the data can actually answer.

Retention is reported two ways, because they answer different questions:

* **exact day** — D7 means "was active on the 7th day after signing up", which
  is the standard cohort reading and the one that compares across weeks;
* **rolling** — "was active on day 7 or later", which is what a small cohort
  can show at all (a user who returns on day 9 still came back).

Days are calendar days in the site's timezone. Activity means a play, not a page
view: the question these cohorts answer is whether people watch, and page views
count a visit to a browse page the same as an evening of watching.
"""
from datetime import datetime, time, timedelta

from django.utils import timezone

DAY_OFFSETS = (1, 7, 30)
ROLLING_OFFSET = 7
MAX_WEEKS = 16


def user_db():
    """The alias that holds the user tables (the external DB when routed)."""
    from .models import DBRoutingConfig
    try:
        cfg = DBRoutingConfig.get_config()
        if cfg.use_external_db and cfg.external_db_ready:
            return 'external'
    except Exception:
        pass
    return 'default'


def _aliases():
    """Every alias that may hold a copy of a table we count (deduplicated)."""
    aliases = ['default']
    try:
        other = user_db()
        if other not in aliases:
            aliases.append(other)
    except Exception:
        pass
    return aliases


def _localdate(value):
    if value is None:
        return None
    if timezone.is_aware(value):
        return timezone.localtime(value).date()
    return value.date()


def _start_of_day(day):
    return timezone.make_aware(datetime.combine(day, time.min),
                               timezone.get_current_timezone())


def active_days(since=None):
    """``{user_id: {date, ...}}`` — the days each user played something.

    One pass over the play history; ``PlayHistory`` holds one row per
    user+title, so this is bounded by users x titles, not by watch events.
    """
    from .models import PlayHistory
    days = {}
    qs = PlayHistory.objects.using(user_db()).filter(user_id__isnull=False)
    if since is not None:
        qs = qs.filter(last_played_at__gte=since)
    for user_id, when in qs.values_list('user_id', 'last_played_at').iterator():
        day = _localdate(when)
        if day is not None:
            days.setdefault(user_id, set()).add(day)
    return days


def signups(since=None):
    """``{user_id: signup date}`` for accounts that exist in the user tables."""
    from .models import SyncedUser
    out = {}
    qs = SyncedUser.objects.using(user_db()).filter(user_id__isnull=False)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    for user_id, when in qs.values_list('user_id', 'created_at').iterator():
        day = _localdate(when)
        if day is not None:
            out.setdefault(user_id, day)
    return out


def _pct(part, whole):
    return round(part * 100.0 / whole, 1) if whole else 0.0


def cohorts(weeks=8):
    """One row per signup week: cohort size, D1/D7/D30 and rolling D7."""
    weeks = max(1, min(MAX_WEEKS, int(weeks or 8)))
    today = timezone.localdate()
    this_monday = today - timedelta(days=today.weekday())
    first_monday = this_monday - timedelta(days=7 * (weeks - 1))

    accounts = signups(_start_of_day(first_monday))
    active = active_days(_start_of_day(first_monday))

    rows = []
    totals = {'size': 0}
    for offset in DAY_OFFSETS:
        totals['d%d' % offset] = 0
    totals['rolling'] = 0

    for i in range(weeks):
        start = first_monday + timedelta(days=7 * i)
        end = start + timedelta(days=7)
        members = [uid for uid, day in accounts.items() if start <= day < end]
        row = {
            'week_start': start,
            'label': '%s – %s' % (start.strftime('%b %d'),
                                  (end - timedelta(days=1)).strftime('%b %d')),
            'partial': end > today,
            'size': len(members),
        }
        for step in DAY_OFFSETS:
            hits = 0
            for uid in members:
                wanted = accounts[uid] + timedelta(days=step)
                if wanted <= today and wanted in active.get(uid, ()):
                    hits += 1
            row['d%d' % step] = hits
            row['d%d_pct' % step] = _pct(hits, row['size'])
            totals['d%d' % step] += hits
        rolling = 0
        for uid in members:
            wanted = accounts[uid] + timedelta(days=ROLLING_OFFSET)
            if wanted <= today and any(d >= wanted for d in active.get(uid, ())):
                rolling += 1
        row['rolling'] = rolling
        row['rolling_pct'] = _pct(rolling, row['size'])
        totals['size'] += row['size']
        totals['rolling'] += rolling
        rows.append(row)

    for step in DAY_OFFSETS:
        totals['d%d_pct' % step] = _pct(totals['d%d' % step], totals['size'])
    totals['rolling_pct'] = _pct(totals['rolling'], totals['size'])

    rows.reverse()  # newest cohort first
    return rows, totals


def first_play_stats(days=30):
    """What happened on each user's first play — the first impression.

    "Never finished a first play" is the number to watch: it counts people who
    opened a title and did not reach the end of it, which no amount of
    marketing spend fixes, it only enlarges.
    """
    from django.db.models import Min
    from .models import PlayHistory
    alias = user_db()
    base = PlayHistory.objects.using(alias).filter(user_id__isnull=False)

    firsts = {}
    for user_id, when in (base.values('user_id')
                          .annotate(first=Min('last_played_at'))
                          .values_list('user_id', 'first')):
        if when is not None:
            firsts[user_id] = when

    # Pull only the first-play rows: one query, bounded by the number of users.
    details = {}
    wanted = set(firsts.values())
    if wanted:
        rows = (base.filter(last_played_at__in=wanted)
                .values_list('user_id', 'last_played_at', 'completed',
                             'duration_seconds', 'title'))
        for user_id, when, completed, watched, title in rows:
            key = (user_id, when)
            if key in details or when not in wanted:
                continue
            if firsts.get(user_id) != when:
                continue
            details[key] = (completed, watched or 0, title or '')

    players = len(firsts)
    finished = sum(1 for d in details.values() if d[0])
    started_only = sum(1 for d in details.values() if not d[1])

    titles = {}
    for completed, watched, title in details.values():
        if title:
            titles[title] = titles.get(title, 0) + 1
    top_first = sorted(titles.items(), key=lambda item: (-item[1], item[0]))[:5]

    since = timezone.now() - timedelta(days=days)
    recent = base.filter(last_played_at__gte=since)
    recent_plays = recent.count()
    recent_completed = recent.filter(completed=True).count()
    recent_users = recent.values('user_id').distinct().count()

    return {
        'players': players,
        'finished_first': finished,
        'finished_first_pct': _pct(finished, players),
        'never_finished_first': players - finished,
        'started_only': started_only,
        'started_only_pct': _pct(started_only, players),
        'top_first': top_first,
        'days': days,
        'recent_plays': recent_plays,
        'recent_completed': recent_completed,
        'recent_completed_pct': _pct(recent_completed, recent_plays),
        'recent_users': recent_users,
    }


def install_funnel():
    """Downloads → app opened → app reached the API.

    Every step comes from ``ApkDownload`` itself, so the funnel does not guess
    which download became which account. The account count is reported beside
    it, not inside it, for the same reason.
    """
    from .models import ApkDownload, SyncedUser
    downloads = opened = reached_api = 0
    for alias in _aliases():
        qs = ApkDownload.objects.using(alias)
        downloads += qs.count()
        opened_qs = qs.filter(opened_app=True)
        opened += opened_qs.count()
        reached_api += opened_qs.exclude(device_user_id='').count()

    accounts = 0
    for alias in _aliases():
        try:
            accounts += SyncedUser.objects.using(alias).count()
        except Exception:
            continue

    steps = [
        {'label': 'APK downloads', 'value': downloads, 'pct': 100.0 if downloads else 0.0},
        {'label': 'App opened', 'value': opened, 'pct': _pct(opened, downloads)},
        {'label': 'Reached the API', 'value': reached_api, 'pct': _pct(reached_api, downloads)},
    ]
    return {'steps': steps, 'accounts': accounts,
            'opened_pct': _pct(opened, downloads)}


def subscription_funnel():
    """Accounts → watched once → ever subscribed → subscribed today."""
    from .models import SubscriptionEvent, SyncedUser

    played = set(active_days().keys())
    subscribed_emails = set()
    for alias in _aliases():
        try:
            subscribed_emails |= set(
                SubscriptionEvent.objects.using(alias)
                .filter(is_subscribed=True)
                .values_list('email', flat=True))
        except Exception:
            continue

    accounts = played_accounts = ever = current = 0
    seen = set()
    for alias in _aliases():
        try:
            rows = (SyncedUser.objects.using(alias)
                    .values_list('id', 'user_id', 'email', 'is_subscribed'))
        except Exception:
            continue
        for row_id, user_id, email, is_subscribed in rows:
            key = (alias, row_id)
            if key in seen:
                continue
            seen.add(key)
            accounts += 1
            if user_id in played:
                played_accounts += 1
            if is_subscribed or (email or '').lower() in subscribed_emails:
                ever += 1
            if is_subscribed:
                current += 1

    steps = [
        {'label': 'Accounts', 'value': accounts, 'pct': 100.0 if accounts else 0.0},
        {'label': 'Watched something', 'value': played_accounts,
         'pct': _pct(played_accounts, accounts)},
        {'label': 'Ever subscribed', 'value': ever, 'pct': _pct(ever, accounts)},
        {'label': 'Subscribed today', 'value': current, 'pct': _pct(current, accounts)},
    ]
    return {'steps': steps}


def build_report(weeks=8, days=30):
    """Everything the cohorts page renders, from one place."""
    cohort_rows, cohort_totals = cohorts(weeks)
    return {
        'weeks': max(1, min(MAX_WEEKS, int(weeks or 8))),
        'cohorts': cohort_rows,
        'cohort_totals': cohort_totals,
        'first_play': first_play_stats(days=days),
        'install': install_funnel(),
        'subscriptions': subscription_funnel(),
        'day_offsets': DAY_OFFSETS,
        'rolling_offset': ROLLING_OFFSET,
        'user_db': user_db(),
        'generated_at': timezone.now(),
    }
