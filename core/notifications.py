"""App notifications: compose in the admin panel, read in the app.

Two halves live here so they can never drift:

* ``notifications_admin`` / ``notification_detail`` - the admin pages. Sending a
  message expands its audience (all / subscribed / unsubscribed / selected) into
  one ``AppNotificationRecipient`` row per user, which is what the delivered and
  read numbers are reported from.
* ``api_notifications`` / ``api_notifications_read`` - the app endpoints. They
  use the same Basic auth + app-header validation as every other app API, and
  they create the delivery row for users who signed up after the message was
  sent, so an old message still reaches them.
"""

import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.dateparse import parse_datetime

from .audit import log_action
from .models import (AndroidApp, AppNotification, AppNotificationRecipient,
                     SyncedUser)
from .views import _enforce_user_block, _validate_android_auth, is_staff_or_superuser


def _notification_audience_emails(audience, selected_emails=None):
    """Emails matching an audience, read from the users DB (SyncedUser).

    'selected' keeps only addresses that exist as synced users, so a typo in the
    compose box cannot create a recipient no device can ever read.
    """
    if audience == AppNotification.AUDIENCE_SELECTED:
        wanted = {e.strip().lower() for e in (selected_emails or []) if e.strip()}
        if not wanted:
            return []
        return sorted(set(SyncedUser.objects.filter(email__in=wanted)
                          .values_list('email', flat=True)))

    qs = SyncedUser.objects.all()
    if audience == AppNotification.AUDIENCE_SUBSCRIBED:
        qs = qs.filter(is_subscribed=True)
    elif audience == AppNotification.AUDIENCE_UNSUBSCRIBED:
        qs = qs.filter(is_subscribed=False)
    return list(qs.values_list('email', flat=True))


@login_required
@user_passes_test(is_staff_or_superuser)
def notifications_admin(request):
    """Compose a notification and see everything already sent."""
    if request.method == 'POST':
        title = (request.POST.get('title') or '').strip()
        body = (request.POST.get('body') or '').strip()
        link_url = (request.POST.get('link_url') or '').strip()
        audience = (request.POST.get('audience') or AppNotification.AUDIENCE_ALL).strip()
        app_id = request.POST.get('app') or ''
        selected = [e for e in re.split(r'[\s,;]+', request.POST.get('selected_emails') or '')
                    if e.strip()]

        if audience not in {a for a, _ in AppNotification.AUDIENCE_CHOICES}:
            audience = AppNotification.AUDIENCE_ALL

        if not title:
            messages.error(request, 'A title is required.')
        elif audience == AppNotification.AUDIENCE_SELECTED and not selected:
            messages.error(request, 'Pick at least one user for a selected audience.')
        else:
            app_obj = AndroidApp.objects.filter(id=app_id).first() if app_id else None
            note = AppNotification.objects.create(
                title=title[:200], body=body, link_url=link_url[:500],
                audience=audience, app=app_obj,
                created_by=getattr(request.user, 'username', '') or '',
                sent_at=timezone.now(),
            )
            emails = list(dict.fromkeys(_notification_audience_emails(audience, selected)))
            AppNotificationRecipient.objects.bulk_create(
                [AppNotificationRecipient(notification=note, email=e) for e in emails],
                ignore_conflicts=True)
            note.recipients_total = len(emails)
            note.save(update_fields=['recipients_total'])
            log_action(request, 'notification_send', title[:80],
                       'audience=%s recipients=%d' % (audience, len(emails)))
            messages.success(request, 'Sent to %d user%s.' % (
                len(emails), '' if len(emails) == 1 else 's'))
            # Come back to the same app and audience so a second message does
            # not silently fall back to "All apps" or to everyone.
            state = []
            if app_obj:
                state.append('app=%s' % app_obj.id)
            if audience:
                state.append('audience=%s' % audience)
            return redirect(reverse('notifications_admin') +
                            ('?' + '&'.join(state) if state else ''))

    notes = list(AppNotification.objects.select_related('app')[:60])
    total_users = SyncedUser.objects.count()
    subscribed = SyncedUser.objects.filter(is_subscribed=True).count()
    return render(request, 'core/admin_notifications.html', {
        'notes': notes,
        'apps': AndroidApp.objects.all().order_by('name'),
        'audiences': AppNotification.AUDIENCE_CHOICES,
        'stats': {
            'total_users': total_users,
            'subscribed': subscribed,
            'unsubscribed': total_users - subscribed,
            'sent': AppNotification.objects.count(),
            'delivered': AppNotificationRecipient.objects.exclude(delivered_at=None).count(),
            'read': AppNotificationRecipient.objects.exclude(read_at=None).count(),
        },
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def notification_detail(request, notification_id):
    """Delivered/read state of one notification, per user."""
    from django.core.paginator import Paginator

    note = get_object_or_404(AppNotification, id=notification_id)
    state = (request.GET.get('state') or 'all').lower()
    q = (request.GET.get('q') or '').strip()

    rows = note.recipients.all()
    if state == 'delivered':
        rows = rows.exclude(delivered_at=None)
    elif state == 'undelivered':
        rows = rows.filter(delivered_at=None)
    elif state == 'read':
        rows = rows.exclude(read_at=None)
    elif state == 'unread':
        rows = rows.filter(read_at=None)
    if q:
        rows = rows.filter(email__icontains=q)

    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="notification-%s.csv"' % note.id
        response.write('email,delivered_at,read_at,read_count\n')
        for row in note.recipients.all().iterator():
            response.write('%s,%s,%s,%s\n' % (
                row.email,
                row.delivered_at.strftime('%Y-%m-%d %H:%M:%S') if row.delivered_at else '',
                row.read_at.strftime('%Y-%m-%d %H:%M:%S') if row.read_at else '',
                row.read_count))
        return response

    return render(request, 'core/admin_notification_detail.html', {
        'note': note,
        'page': Paginator(rows, 100).get_page(request.GET.get('page')),
        'state': state,
        'q': q,
        'sent_total': note.recipients.count(),
        'delivered_total': note.recipients.exclude(delivered_at=None).count(),
        'read_total': note.recipients.exclude(read_at=None).count(),
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def notification_toggle(request, notification_id):
    """Switch one message off, or back on, for the app inbox.

    The app only returns active messages, so this is the stop switch for a
    message that went out with a mistake in it. Recipients and their read
    state are kept, so turning it back on restores the same record.
    """
    note = get_object_or_404(AppNotification, id=notification_id)
    if request.method != 'POST':
        return redirect('notifications_admin')

    note.is_active = not note.is_active
    note.save(update_fields=['is_active'])
    log_action(request, 'notification_toggle', '%s #%d' % (note.title[:60], note.id),
               'active=%s' % note.is_active)
    messages.success(request, '"%s" is now %s in the app.' % (
        note.title[:60], 'on' if note.is_active else 'off'))

    app_id = (request.POST.get('app') or '').strip()
    if app_id.isdigit() and request.POST.get('from') != 'composer':
        return redirect('android_app_dashboard_detail', app_id=int(app_id))
    # Fired from the Sent list: come back to it, keeping whatever it was
    # filtered by.
    state = ['app=%s' % app_id] if app_id.isdigit() else []
    audience = (request.POST.get('audience') or '').strip()
    if audience:
        state.append('audience=%s' % audience)
    return redirect(reverse('notifications_admin') +
                    ('?' + '&'.join(state) if state else ''))


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_user_search(request):
    """Typeahead behind the composer's user picker.

    Staff only, matches on address or display name, and answers the ten most
    recent matches — enough for a picker, and cheap on a large SyncedUser.
    """
    q = (request.GET.get('q') or '').strip()
    users = SyncedUser.objects.all()
    if q:
        users = users.filter(Q(email__icontains=q) | Q(display_name__icontains=q))
    users = users.order_by('-last_synced_at').values(
        'email', 'display_name', 'is_subscribed', 'block_mode')[:10]
    return JsonResponse({'results': [
        {'email': u['email'], 'name': u['display_name'],
         'subscribed': u['is_subscribed'], 'blocked': bool(u['block_mode'])}
        for u in users]}, headers={'Cache-Control': 'no-store'})


def _payload(note, recipient):
    return {
        'id': note.id,
        'title': note.title,
        'body': note.body,
        'link': note.link_url,
        'sentAt': note.sent_at.isoformat() if note.sent_at else '',
        'read': bool(recipient and recipient.read_at),
        'readAt': recipient.read_at.isoformat() if recipient and recipient.read_at else '',
    }


def api_notifications(request):
    """GET /api/notifications/?email=... — the caller's messages.

    Same Basic auth + app headers as every other app endpoint. Returning a
    message marks it delivered; opening it calls api_notifications_read.
    """
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    email = (request.GET.get('email') or '').strip().lower()
    if not email:
        return JsonResponse({'status': 'error', 'message': 'Email is required'}, status=400)

    block_err = _enforce_user_block(email, app_match)
    if block_err:
        return block_err

    device_id = (request.GET.get('deviceId') or '').strip()[:100]
    since = (request.GET.get('since') or '').strip()

    profile = SyncedUser.objects.filter(email__iexact=email).first()
    subscribed = bool(profile and profile.is_subscribed)

    match = Q(audience=AppNotification.AUDIENCE_ALL)
    match |= Q(audience=(AppNotification.AUDIENCE_SUBSCRIBED if subscribed
                         else AppNotification.AUDIENCE_UNSUBSCRIBED))
    match |= Q(audience=AppNotification.AUDIENCE_SELECTED, recipients__email=email)

    notes = AppNotification.objects.filter(match, is_active=True, sent_at__isnull=False)
    if since:
        try:
            notes = notes.filter(sent_at__gt=parse_datetime(since) or timezone.now())
        except (TypeError, ValueError):
            pass
    notes = notes.distinct().order_by('-sent_at')[:50]

    now = timezone.now()
    items, unread = [], 0
    for note in notes:
        recipient, _created = AppNotificationRecipient.objects.get_or_create(
            notification=note, email=email, defaults={'device_id': device_id})
        fields = []
        if recipient.delivered_at is None:
            recipient.delivered_at = now
            fields.append('delivered_at')
        if device_id and recipient.device_id != device_id:
            recipient.device_id = device_id
            fields.append('device_id')
        if fields:
            recipient.save(update_fields=fields)
        if not recipient.read_at:
            unread += 1
        items.append(_payload(note, recipient))

    return JsonResponse({'status': 'ok', 'unread': unread, 'notifications': items})


@csrf_exempt
def api_notifications_read(request):
    """POST /api/notifications/read/ {email, ids: [...]} — mark as read.

    CSRF exempt: the app authenticates with Basic auth and sends no cookies,
    so the CSRF check could only ever reject it (it did — a 403 that left
    every message unread on the site).
    """
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    email = (body.get('email') or '').strip().lower()
    ids = body.get('ids') or []
    if not email:
        return JsonResponse({'status': 'error', 'message': 'Email is required'}, status=400)
    if not isinstance(ids, list) or not ids:
        return JsonResponse({'status': 'error', 'message': 'ids must be a non-empty list'}, status=400)

    block_err = _enforce_user_block(email, app_match)
    if block_err:
        return block_err

    clean_ids = []
    for value in ids[:200]:
        try:
            clean_ids.append(int(value))
        except (TypeError, ValueError):
            continue

    now = timezone.now()
    updated = 0
    for row in AppNotificationRecipient.objects.filter(email=email,
                                                       notification_id__in=clean_ids):
        row.read_count += 1
        row.read_at = row.read_at or now
        row.delivered_at = row.delivered_at or now
        row.save(update_fields=['read_at', 'read_count', 'delivered_at'])
        updated += 1

    return JsonResponse({'status': 'ok', 'updated': updated, 'readAt': now.isoformat()})
