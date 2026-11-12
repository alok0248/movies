"""How long each device was online, with daily/weekly/monthly graphs.

The app sends a heartbeat every minute. ``record_device_heartbeat`` turns those
into per-device online seconds for the day; the report pages then show the
actual online time of every unique device and, per device, online vs offline
time for the day, the last twelve weeks and the last twelve months.

Only the immediate gap between two heartbeats counts (capped at the online
window), so a phone that was switched off overnight never inflates a total.
"""

import csv
import datetime

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from .models import (DeviceOnlineDaily, DeviceOnlineState, HeartbeatActivity,
                     UserHeartbeat)
from .views import is_staff_or_superuser

ONLINE_WINDOW = UserHeartbeat.ONLINE_WINDOW_SECONDS  # 180 s
DAY_SECONDS = 24 * 3600


# ----------------------------------------------------------------------
#  Recording
# ----------------------------------------------------------------------

def record_device_heartbeat(email, device_id, when=None):
    """Add the time since this device's previous heartbeat to today's total."""
    if not device_id or device_id in ('unknown', 'device_unknown'):
        return
    now = when or timezone.now()
    device_id = device_id[:100]

    state = DeviceOnlineState.objects.filter(device_id=device_id).first()
    if state is None:
        state = DeviceOnlineState.objects.create(
            device_id=device_id, email=(email or '')[:254], last_seen_at=now)
        counted = 0
    else:
        gap = (now - state.last_seen_at).total_seconds()
        # Cap at one window: time between heartbeats is presence, but a long
        # silence means the device was away, not online the whole time.
        counted = int(min(max(gap, 0), ONLINE_WINDOW)) if gap <= ONLINE_WINDOW * 2 else 0
        state.last_seen_at = now
        if email:
            state.email = email[:254]
        state.save(update_fields=['last_seen_at', 'email'])

    bucket, _created = DeviceOnlineDaily.objects.get_or_create(
        device_id=device_id, day=now.date(),
        defaults={'email': (email or '')[:254]})
    bucket.online_seconds = min(DAY_SECONDS, bucket.online_seconds + counted)
    bucket.heartbeat_count += 1
    if email:
        bucket.email = email[:254]
    bucket.save(update_fields=['online_seconds', 'heartbeat_count', 'email'])


def _offline_seconds(day, online_seconds):
    """Offline = the elapsed part of that day minus the online part."""
    today = timezone.localdate()
    if day == today:
        elapsed = int((timezone.localtime() -
                       timezone.localtime().replace(hour=0, minute=0, second=0,
                                                    microsecond=0)).total_seconds())
    elif day > today:
        elapsed = 0
    else:
        elapsed = DAY_SECONDS
    return max(0, elapsed - online_seconds)


def _fmt_duration(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return '%dh %02dm' % (hours, minutes)
    if minutes:
        return '%dm %02ds' % (minutes, secs)
    return '%ds' % secs


# ----------------------------------------------------------------------
#  Data for the graphs
# ----------------------------------------------------------------------

def _daily_series(device_id, days=30):
    today = timezone.localdate()
    start = today - datetime.timedelta(days=days - 1)
    rows = {row.day: row for row in DeviceOnlineDaily.objects
            .filter(device_id=device_id, day__gte=start)}
    series = []
    for offset in range(days):
        day = start + datetime.timedelta(days=offset)
        row = rows.get(day)
        online = row.online_seconds if row else 0
        offline = _offline_seconds(day, online)
        series.append({
            'label': day.strftime('%d %b'),
            'key': day.isoformat(),
            'online': online,
            'offline': offline,
            'online_text': _fmt_duration(online),
            'offline_text': _fmt_duration(offline),
        })
    return series


def _weekly_series(device_id, weeks=12):
    today = timezone.localdate()
    this_monday = today - datetime.timedelta(days=today.weekday())
    start = this_monday - datetime.timedelta(weeks=weeks - 1)
    rows = DeviceOnlineDaily.objects.filter(device_id=device_id, day__gte=start)
    buckets = {}
    for row in rows:
        monday = row.day - datetime.timedelta(days=row.day.weekday())
        buckets[monday] = buckets.get(monday, 0) + row.online_seconds
    series = []
    for index in range(weeks):
        monday = start + datetime.timedelta(weeks=index)
        online = buckets.get(monday, 0)
        elapsed_days = min(7, (today - monday).days + 1)
        offline = max(0, elapsed_days * DAY_SECONDS - online)
        series.append({
            'label': monday.strftime('%d %b'),
            'key': monday.isoformat(),
            'online': online,
            'offline': offline,
            'online_text': _fmt_duration(online),
            'offline_text': _fmt_duration(offline),
        })
    return series


def _monthly_series(device_id, months=12):
    today = timezone.localdate()
    start_month = (today.replace(day=1) - datetime.timedelta(days=31 * (months - 1))).replace(day=1)
    rows = DeviceOnlineDaily.objects.filter(device_id=device_id, day__gte=start_month)
    buckets = {}
    for row in rows:
        key = (row.day.year, row.day.month)
        buckets[key] = buckets.get(key, 0) + row.online_seconds

    series = []
    year, month = start_month.year, start_month.month
    for _index in range(months):
        online = buckets.get((year, month), 0)
        days_in_month = 0
        for offset in range(31):
            try:
                day = datetime.date(year, month, 1) + datetime.timedelta(days=offset)
            except ValueError:
                break
            if day.month != month:
                break
            if day > today:
                break
            days_in_month += 1
        offline = max(0, days_in_month * DAY_SECONDS - online)
        series.append({
            'label': datetime.date(year, month, 1).strftime('%b %Y'),
            'key': '%04d-%02d' % (year, month),
            'online': online,
            'offline': offline,
            'online_text': _fmt_duration(online),
            'offline_text': _fmt_duration(offline),
        })
        month += 1
        if month > 12:
            month = 1
            year += 1
    return series


def _scale(series):
    """Largest bar value, used to size the bars as percentages."""
    return max([max(item['online'], item['offline']) for item in series] or [1]) or 1


# ----------------------------------------------------------------------
#  Admin pages
# ----------------------------------------------------------------------

@login_required
@user_passes_test(is_staff_or_superuser)
def device_activity(request):
    """Every unique device with its actual online time, and who is online now."""
    search = (request.GET.get('q') or '').strip()
    states = DeviceOnlineState.objects.all().order_by('-last_seen_at')
    if search:
        states = states.filter(email__icontains=search) | states.filter(device_id__icontains=search)

    today = timezone.localdate()
    today_rows = {row.device_id: row for row in
                  DeviceOnlineDaily.objects.filter(day=today)}
    week_start = today - datetime.timedelta(days=6)
    week_totals = dict(DeviceOnlineDaily.objects.filter(day__gte=week_start)
                       .values_list('device_id')
                       .annotate(total=Sum('online_seconds'))
                       .values_list('device_id', 'total'))
    month_start = today - datetime.timedelta(days=29)
    month_totals = dict(DeviceOnlineDaily.objects.filter(day__gte=month_start)
                        .values_list('device_id')
                        .annotate(total=Sum('online_seconds'))
                        .values_list('device_id', 'total'))

    live = {hb.device_id: hb for hb in
            UserHeartbeat.objects.exclude(device_id='')}

    devices = []
    now = timezone.now()
    for state in states[:500]:
        today_row = today_rows.get(state.device_id)
        online_today = today_row.online_seconds if today_row else 0
        heartbeat = live.get(state.device_id)
        age = (now - state.last_seen_at).total_seconds() if state.last_seen_at else 99999
        devices.append({
            'device_id': state.device_id,
            'email': state.email,
            'last_seen_at': state.last_seen_at,
            'is_online': age <= ONLINE_WINDOW,
            'online_today': online_today,
            'online_today_text': _fmt_duration(online_today),
            'offline_today_text': _fmt_duration(_offline_seconds(today, online_today)),
            'online_week_text': _fmt_duration(week_totals.get(state.device_id, 0)),
            'online_month_text': _fmt_duration(month_totals.get(state.device_id, 0)),
            'activity': heartbeat.activity_payload() if heartbeat else None,
        })

    devices.sort(key=lambda item: (not item['is_online'], -item['online_today']))
    totals = DeviceOnlineDaily.objects.filter(day=today).aggregate(total=Sum('online_seconds'))
    return render(request, 'core/admin_device_activity.html', {
        'devices': devices,
        'search': search,
        'device_count': len(devices),
        'online_now': sum(1 for item in devices if item['is_online']),
        'online_today_text': _fmt_duration(totals.get('total') or 0),
    })


def _device_or_404(device_id):
    return get_object_or_404(DeviceOnlineState, device_id=device_id)


@login_required
@user_passes_test(is_staff_or_superuser)
def device_activity_detail(request, device_id):
    """One device: online vs offline for the day, week and month views."""
    state = _device_or_404(device_id)
    daily = _daily_series(device_id, 30)
    weekly = _weekly_series(device_id, 12)
    monthly = _monthly_series(device_id, 12)
    heartbeat = UserHeartbeat.objects.filter(device_id=device_id).first()

    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="device-%s.csv"' % device_id
        writer = csv.writer(response)
        writer.writerow(['day', 'online_seconds', 'offline_seconds', 'heartbeats'])
        for row in DeviceOnlineDaily.objects.filter(device_id=device_id).order_by('day'):
            writer.writerow([row.day, row.online_seconds,
                             _offline_seconds(row.day, row.online_seconds),
                             row.heartbeat_count])
        return response

    return render(request, 'core/admin_device_activity_detail.html', {
        'state': state,
        'daily': daily, 'daily_scale': _scale(daily),
        'weekly': weekly, 'weekly_scale': _scale(weekly),
        'monthly': monthly, 'monthly_scale': _scale(monthly),
        'heartbeat': heartbeat,
        'is_online': (timezone.now() - state.last_seen_at).total_seconds() <= ONLINE_WINDOW,
        'today_online': daily[-1]['online_text'] if daily else '0s',
        'today_offline': daily[-1]['offline_text'] if daily else '0s',
        'recent_activity': HeartbeatActivity.objects.filter(device_id=device_id)[:25],
        'fmt': _fmt_duration(None),
        'today_total': _fmt_duration(daily[-1]['online'] if daily else 0),
    })
