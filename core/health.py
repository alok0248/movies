"""Health watch: 5xx rate, disk space and gunicorn workers → email / Telegram.

The three failures that actually take this site down are cheap to detect:

* the 5xx rate climbing (a bad deploy, a dead database, a worker stuck),
* the disk filling (logs, sqlite, backups),
* the gunicorn workers disappearing (an OOM kill, a crash loop).

``check_health`` reads those three signals, plus the expiry date of the TLS
certificate nginx serves (``HEALTH_CERT_PATHS``), and ``dispatch`` sends one
alert when any of them crosses its threshold. Email goes to every active
``AlertRecipient`` row through the SMTP settings the site already uses, and
Telegram is optional (bot token + chat id).

A repeated alert is throttled by ``AlertSettings.repeat_minutes``, so a disk
that stays full does not mail every two minutes. The whole module is written so
that a failure inside it can never break a request or a command: every check
returns a finding instead of raising, and delivery errors are reported rather
than thrown.
"""

import logging
import shutil
import ssl
import subprocess
import time

from django.conf import settings as django_settings
from django.core.cache import cache
from django.db.models import Count
from django.utils import timezone

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ checks

def five_xx_rate(minutes=15):
    """(percentage, failed, total) over the last ``minutes`` of request log."""
    from .models import RequestLog
    from datetime import timedelta
    since = timezone.now() - timedelta(minutes=max(1, int(minutes or 15)))
    try:
        qs = RequestLog.objects.filter(created_at__gte=since)
        total = qs.count()
        failed = qs.filter(status_code__gte=500).count()
    except Exception:
        logger.debug('health: could not read the request log', exc_info=True)
        return 0.0, 0, 0
    pct = (100.0 * failed / total) if total else 0.0
    return pct, failed, total


def disk_usage(path='/'):
    """(percentage used, used bytes, total bytes)."""
    try:
        total, used, _free = shutil.disk_usage(path)
    except Exception:
        logger.debug('health: could not read disk usage', exc_info=True)
        return 0.0, 0, 0
    pct = (100.0 * used / total) if total else 0.0
    return pct, used, total


def gunicorn_workers():
    """How many gunicorn processes are alive (master included). -1 = unknown."""
    try:
        out = subprocess.run(
            ['pgrep', '-fc', 'gunicorn.*movie_portal'],
            capture_output=True, text=True, timeout=5,
        )
        text = (out.stdout or '').strip()
        return int(text) if text else 0
    except Exception:
        logger.debug('health: could not count gunicorn processes', exc_info=True)
        return -1


# -------------------------------------------------------------- certificate

# Days of warning before the certificate nginx serves expires. Let's Encrypt
# renews with 30 days left, so a healthy box never gets near this; a finding
# here means a renewal run failed or the deploy hook did not install the new
# chain into /etc/ssl/custom.
CERT_WARN_DAYS = 21


def cert_paths():
    """The certificate files to watch, from ``settings.HEALTH_CERT_PATHS``."""
    paths = getattr(django_settings, 'HEALTH_CERT_PATHS', None) or []
    return [path for path in paths if path]


def cert_not_after(path):
    """``notAfter`` of the first certificate in ``path``; None when unreadable."""
    try:
        out = subprocess.run(
            ['openssl', 'x509', '-in', path, '-noout', '-enddate'],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        logger.debug('health: could not read %s with openssl', path, exc_info=True)
        return None
    line = (out.stdout or '').strip()
    if out.returncode != 0 or not line.startswith('notAfter='):
        logger.debug('health: %s is not a readable certificate: %s',
                     path, (out.stderr or '').strip()[:200])
        return None
    return line.split('=', 1)[1].strip()


def cert_days_until(stamp):
    """Whole days from now until an openssl date stamp; None when unparseable."""
    try:
        expires = ssl.cert_time_to_seconds(stamp)
    except ValueError:
        logger.debug('health: could not parse the certificate date %r', stamp)
        return None
    return int((expires - time.time()) // 86400)


def cert_days_left(path):
    """Whole days until the certificate in ``path`` expires; None when unreadable."""
    stamp = cert_not_after(path)
    return None if stamp is None else cert_days_until(stamp)


def soonest_expiry(paths=None):
    """The soonest-expiring of ``paths`` as ``(days_left, notAfter, path)``.

    Unreadable files are skipped, so one unreadable path cannot hide the others;
    ``(None, None, None)`` when none of them can be read.
    """
    soonest = (None, None, None)
    for path in (cert_paths() if paths is None else paths):
        stamp = cert_not_after(path)
        days = None if stamp is None else cert_days_until(stamp)
        if days is not None and (soonest[0] is None or days < soonest[0]):
            soonest = (days, stamp, path)
    return soonest


def run_checks(record=True):
    """Every enabled check as ``{key, title, ok, detail}``, in display order.

    A check that cannot read its signal reports ``ok=None`` (unknown) rather
    than a false alarm.
    """
    from .models import AlertSettings
    conf = AlertSettings.get_settings()
    findings = []

    if getattr(conf, 'check_5xx', True):
        threshold = float(getattr(conf, 'five_xx_threshold', 2.0) or 0)
        window = int(getattr(conf, 'five_xx_window_minutes', 15) or 15)
        pct, failed, total = five_xx_rate(window)
        findings.append({
            'key': '5xx',
            'title': 'Server errors',
            'ok': pct <= threshold,
            'detail': '%d of %d requests failed with a 5xx in the last %d minutes '
                      '(%.1f%%, threshold %.1f%%)' % (failed, total, window, pct, threshold),
        })

    if getattr(conf, 'check_disk', True):
        threshold = int(getattr(conf, 'disk_threshold', 85) or 85)
        pct, used, total = disk_usage()
        findings.append({
            'key': 'disk',
            'title': 'Disk space',
            'ok': pct < threshold,
            'detail': '%.1f%% used (threshold %d%%)' % (pct, threshold),
        })

    if getattr(conf, 'check_workers', True):
        minimum = int(getattr(conf, 'min_workers', 2) or 1)
        count = gunicorn_workers()
        findings.append({
            'key': 'workers',
            'title': 'Gunicorn workers',
            'ok': None if count < 0 else count >= minimum,
            'detail': ('could not count them' if count < 0
                       else '%d running (minimum %d)' % (count, minimum)),
        })

    paths = cert_paths()
    if paths:
        days, stamp, path = soonest_expiry(paths)
        if days is None:
            findings.append({
                'key': 'cert',
                'title': 'TLS certificate',
                'ok': None,
                'detail': 'no readable certificate at %s' % ', '.join(paths),
            })
        else:
            when = ('expires in %d days' % days if days >= 0
                    else 'expired %d days ago' % -days)
            findings.append({
                'key': 'cert',
                'title': 'TLS certificate',
                'ok': days >= CERT_WARN_DAYS,
                'detail': '%s %s (%s, warn at %d days)' % (path, when, stamp, CERT_WARN_DAYS),
            })

    if record:
        try:
            record_incidents(findings)
        except Exception:
            logger.debug('health: could not record incidents', exc_info=True)

    return findings


def record_incidents(findings=None):
    """Open, update or close one incident per failing check.

    Called at the end of every ``run_checks`` (the cron command, the
    "Run check now" button and the sidebar badge all go through it), so the
    incident record reflects what the checks actually saw, whether or not an
    email was allowed to go out. Never raises: a sick database must not stop
    the checks from reporting.
    """
    from .models import AlertIncident
    now = timezone.now()
    failing = {f['key']: f for f in (findings if findings is not None else run_checks(record=False))
               if f.get('ok') is False}

    for inc in AlertIncident.objects.filter(resolved_at__isnull=True):
        finding = failing.get(inc.check_key)
        if finding is None:
            inc.resolved_at = now
            inc.save(update_fields=['resolved_at'])
            continue
        inc.last_seen_at = now
        inc.occurrences = (inc.occurrences or 1) + 1
        if finding.get('detail'):
            inc.detail = finding['detail']
        inc.save(update_fields=['last_seen_at', 'occurrences', 'detail'])

    for key, finding in failing.items():
        if AlertIncident.objects.filter(check_key=key, resolved_at__isnull=True).exists():
            continue
        AlertIncident.objects.create(
            check_key=key,
            title=(finding.get('title') or key)[:120],
            detail=finding.get('detail') or '',
            opened_at=now,
            last_seen_at=now,
        )


def in_quiet_hours(conf=None):
    """True when now is inside the configured quiet window (24h HH:MM)."""
    from .models import AlertSettings
    conf = conf or AlertSettings.get_settings()
    start = (getattr(conf, 'quiet_start', '') or '').strip()
    end = (getattr(conf, 'quiet_end', '') or '').strip()
    if not start or not end:
        return False

    def minutes(value):
        try:
            hour, minute = value.split(':')
            hour, minute = int(hour), int(minute)
        except (ValueError, AttributeError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    begin, finish = minutes(start), minutes(end)
    if begin is None or finish is None:
        return False
    local = timezone.localtime()
    current = local.hour * 60 + local.minute
    if begin <= finish:
        return begin <= current < finish
    return current >= begin or current < finish   # window crosses midnight


def problems(findings=None):
    return [f for f in (findings if findings is not None else run_checks())
            if f.get('ok') is False]


# --------------------------------------------------------------- delivery

def _apply_mail_settings():
    """Use the site's own SMTP settings when sending outside a request."""
    try:
        from .models import SiteSettings
        conf = SiteSettings.get_settings()
        if conf and conf.email_host_user:
            django_settings.EMAIL_HOST = conf.email_host or 'smtp.gmail.com'
            django_settings.EMAIL_PORT = conf.email_port or 587
            django_settings.EMAIL_HOST_USER = conf.email_host_user
            django_settings.EMAIL_HOST_PASSWORD = conf.email_host_password or ''
            django_settings.EMAIL_USE_TLS = bool(conf.email_use_tls)
            django_settings.DEFAULT_FROM_EMAIL = conf.email_host_user
            return True
    except Exception:
        logger.debug('health: could not apply the site mail settings', exc_info=True)
    return False


def recipients():
    from .models import AlertRecipient
    try:
        return list(AlertRecipient.objects.filter(is_active=True))
    except Exception:
        return []


def send_email(subject, body, keys=None):
    """Email the active recipients routed to ``keys``. Returns (sent_count, error).

    ``keys`` is the set of failing check keys; a recipient with no routing
    configured hears about everything, and one with routing gets only the
    checks it asked for.
    """
    rows = recipients()
    if keys:
        rows = [r for r in rows if r.wants(keys)]
    addrs = [r.email for r in rows]
    if not addrs:
        return 0, 'no alert recipients configured for these checks'
    _apply_mail_settings()
    try:
        from django.core.mail import send_mail
        send_mail(subject, body, django_settings.DEFAULT_FROM_EMAIL, addrs, fail_silently=False)
        return len(addrs), ''
    except Exception as exc:
        logger.warning('health: could not send the alert email', exc_info=True)
        return 0, str(exc)[:300]


def send_telegram(text):
    """Send one message to the configured chat. Returns (ok, error)."""
    from .models import AlertSettings
    conf = AlertSettings.get_settings()
    token = (getattr(conf, 'telegram_bot_token', '') or '').strip()
    chat = (getattr(conf, 'telegram_chat_id', '') or '').strip()
    if not token or not chat:
        return False, 'Telegram bot token or chat id is missing'
    try:
        import requests
        resp = requests.post(
            'https://api.telegram.org/bot%s/sendMessage' % token,
            json={'chat_id': chat, 'text': text, 'disable_web_page_preview': True},
            timeout=10,
        )
        if resp.status_code >= 400:
            return False, 'Telegram answered %s: %s' % (resp.status_code, resp.text[:200])
        return True, ''
    except Exception as exc:
        logger.warning('health: could not send the Telegram message', exc_info=True)
        return False, str(exc)[:300]


def compose(findings):
    """The subject and body shared by both channels."""
    bad = [f for f in findings if f.get('ok') is False]
    subjects = ', '.join(f['title'] for f in bad) or 'all checks passed'
    lines = ['newmovies health check — %s' % timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
             '', 'Problem: %s' % subjects, '']
    for f in findings:
        mark = {True: 'OK', False: 'PROBLEM', None: 'unknown'}[f.get('ok')]
        lines.append('[%s] %s — %s' % (mark, f['title'], f['detail']))
    return subjects, '\n'.join(lines)


def dispatch(force=False):
    """Run the checks and alert when something is wrong.

    Returns ``{findings, sent_email, sent_telegram, errors, throttled}``.
    ``force`` ignores the repeat window and sends even when everything is fine
    (used by the "send a test alert" button).
    """
    from .models import AlertSettings
    conf = AlertSettings.get_settings()
    findings = run_checks()
    result = {'findings': findings, 'sent_email': 0, 'sent_telegram': False,
              'errors': [], 'throttled': False, 'quiet': False}

    if not getattr(conf, 'enabled', True):
        result['errors'].append('health alerts are switched off')
        return result

    bad = [f for f in findings if f.get('ok') is False]
    if not bad and not force:
        return result

    # Quiet hours hold delivery back, not detection: the incident is already
    # recorded by run_checks, so the problem is visible in the morning even
    # though no message went out overnight.
    if not force and in_quiet_hours(conf):
        result['quiet'] = True
        return result

    if not force:
        window = int(getattr(conf, 'repeat_minutes', 30) or 30) * 60
        key = 'healthalert:sent:' + ','.join(sorted(f['key'] for f in bad))
        try:
            if cache.get(key):
                result['throttled'] = True
                return result
            cache.set(key, 1, window)
        except Exception:
            pass

    subject, body = compose(findings)
    if force and not bad:
        subject = 'test alert — ' + subject
    subject = '[newmovies] %s' % subject

    if getattr(conf, 'notify_email', True):
        count, error = send_email(subject, body, keys=[f['key'] for f in bad] or None)
        result['sent_email'] = count
        if error:
            result['errors'].append('email: ' + error)
    if getattr(conf, 'notify_telegram', False):
        ok, error = send_telegram(subject + '\n\n' + body)
        result['sent_telegram'] = ok
        if error:
            result['errors'].append('telegram: ' + error)

    return result


# ------------------------------------------------------------------
# Data-health rows (the admin data-health page and the sidebar badge).
# These were added after the monitoring checks above; both live here so a
# check is defined once.
# ------------------------------------------------------------------


def check_rows():
    """``(slug, label, count)`` for every check, in page order."""
    from .models import (AndroidAppDevice, AndroidAppDeviceVisit, AndroidAppLog,
                         ErrorLog, SyncedUser, UserHeartbeat)

    now = timezone.now()
    today = timezone.localdate()
    day_ago = now - timezone.timedelta(hours=24)
    month_ago = now - timezone.timedelta(days=90)

    shared = (SyncedUser.objects.exclude(device_id='').values('device_id').order_by()
              .annotate(n=Count('id')).filter(n__gt=1).count())

    return [
        ('expired', 'Expired but still subscribed',
         SyncedUser.objects.filter(is_subscribed=True, valid_until__lt=today).count()),
        ('unverified', 'Accounts not marked verified',
         SyncedUser.objects.filter(is_email_verified=False).count()),
        ('nodevice', 'Accounts with no device id',
         SyncedUser.objects.filter(device_id='').count()),
        ('shared', 'Device ids shared by two accounts', shared),
        ('unlinked', 'Install records with no account link',
         AndroidAppDevice.objects.filter(account_key='').count()),
        ('guests', 'Guest heartbeat rows',
         UserHeartbeat.objects.filter(email__startswith='guest_').count()),
        ('visits', 'Device visits older than 90 days',
         AndroidAppDeviceVisit.objects.filter(visited_at__lt=month_ago).count()),
        ('logs', 'App logs older than 90 days',
         AndroidAppLog.objects.filter(timestamp__lt=month_ago).count()),
        ('errors', 'Errors captured in the last 24 hours',
         ErrorLog.objects.filter(created_at__gte=day_ago).count()),
    ]


ACK_WINDOW = timezone.timedelta(hours=24)


def acknowledged_slugs(now=None):
    """Slugs dismissed in the last 24 hours, with when they were dismissed."""
    from .models import HealthCheckAck

    now = now or timezone.now()
    return dict(HealthCheckAck.objects.filter(acknowledged_at__gte=now - ACK_WINDOW)
                .values_list('slug', 'acknowledged_at'))


def open_check_rows(now=None):
    """``check_rows()`` minus whatever is already dismissed."""
    dismissed = acknowledged_slugs(now)
    return [row for row in check_rows() if row[2] and row[0] not in dismissed]
