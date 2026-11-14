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
from collections import OrderedDict
from datetime import timedelta
from urllib.parse import quote

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
                     SyncedUser, UserMessage)
from .user_auth import authenticate_user
from .views import _enforce_user_block, _validate_android_auth, is_staff_or_superuser

# One message from the app is capped at this many characters, and a device (or
# account) can only write this many per hour — the message endpoint is reachable
# by every install, so a runaway build must not flood the admin page.
MAX_USER_MESSAGE_LEN = 2000
MAX_USER_MESSAGES_PER_HOUR = 30

# The conversation list is built from a recent slice of messages rather than one
# query per thread, so the page stays a couple of queries wide. THREAD_LIMIT caps
# what a single very chatty thread can pull in.
CONVERSATION_WINDOW = 600
CONVERSATIONS_PER_PAGE = 25
THREAD_LIMIT = 300


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
        'open_user_messages': UserMessage.objects.filter(replied_at=None).count(),
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


def _message_payload(row):
    """One user message as the app reads it, answer included."""
    return {
        'id': row.id,
        'kind': row.kind,
        'body': row.body,
        'sentAt': row.created_at.isoformat() if row.created_at else '',
        'notificationId': row.notification_id or 0,
        'notificationTitle': row.notification.title if row.notification else '',
        'reply': row.reply_body or '',
        'repliedAt': row.replied_at.isoformat() if row.replied_at else '',
    }


def _deliver_reply(row):
    """Send the admin's answer to a signed-in user's app inbox.

    Guests have no address to deliver to, so their answer waits in the thread the
    app fetches by device id. Delivering the notification here also means the
    answer arrives on the phone the way every other message does.
    """
    if not row.email:
        return False
    note = AppNotification.objects.create(
        title='Reply from support',
        body=row.reply_body,
        audience=AppNotification.AUDIENCE_SELECTED,
        app=row.app,
        created_by=row.replied_by,
        sent_at=timezone.now(),
    )
    AppNotificationRecipient.objects.bulk_create(
        [AppNotificationRecipient(notification=note, email=row.email,
                                  device_id=row.device_id)],
        ignore_conflicts=True)
    note.recipients_total = 1
    note.save(update_fields=['recipients_total'])
    return True


_NAME_SPLIT = re.compile(r'[^0-9A-Za-z]+')


def _initials(name):
    """Two letters for the conversation avatar."""
    parts = [part for part in _NAME_SPLIT.split(name or '') if part]
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def _conversation_key(row):
    """One thread per address, or per device for a guest.

    The key is what ``?thread=`` carries, so it repeats back through the form
    and the pager without another lookup.
    """
    if row.email:
        return '%s|%s' % (row.app_id or 0, row.email.strip().lower())
    return '%s|device:%s' % (row.app_id or 0, (row.device_id or '').strip())


def _group_conversations(rows):
    """Fold messages into one row per thread, in the order they arrive.

    ``rows`` is newest first, which is the order the rail wants: a thread sits
    where its latest message sits.
    """
    threads = OrderedDict()
    for row in rows:
        key = _conversation_key(row)
        thread = threads.get(key)
        if thread is None:
            thread = {
                'key': key,
                'app': row.app,
                'app_label': row.app.name if row.app else '',
                'display_user': row.display_user,
                'email': row.email,
                'device_id': row.device_id,
                'device_type': row.device_type or '',
                'is_registered': bool(row.email),
                'initials': _initials(row.display_user),
                'total': 0,
                'waiting': 0,
                'last_body': row.body,
                'last_kind': row.kind,
                'last_at': row.created_at,
                'last_id': row.id,
            }
            threads[key] = thread
        thread['total'] += 1
        if not row.replied_at:
            thread['waiting'] += 1
    return list(threads.values())


def _conversation_messages(thread):
    """Every message of one thread, oldest first (newest if it is very long)."""
    rows = UserMessage.objects.select_related('app', 'notification')
    rows = rows.filter(app=thread['app']) if thread['app'] else rows.filter(app__isnull=True)
    if thread['is_registered']:
        rows = rows.filter(email__iexact=thread['email'])
    else:
        rows = rows.filter(email='', device_id=thread['device_id'])
    recent = list(rows.order_by('-created_at', '-id')[:THREAD_LIMIT])
    recent.reverse()
    return recent


def _conversation_from_key(key):
    """Rebuild a thread named by a key, for links pointing outside the window."""
    app_part, _, identity = (key or '').partition('|')
    app_id = int(app_part) if app_part.isdigit() else 0
    rows = UserMessage.objects.select_related('app', 'notification')
    rows = rows.filter(app_id=app_id) if app_id else rows.filter(app__isnull=True)
    if identity.startswith('device:'):
        rows = rows.filter(email='', device_id=identity[len('device:'):])
    elif identity:
        rows = rows.filter(email__iexact=identity)
    else:
        return None
    recent = list(rows.order_by('-created_at', '-id')[:THREAD_LIMIT])
    if not recent:
        return None
    return _group_conversations(recent)[0]


@login_required
@user_passes_test(is_staff_or_superuser)
def user_messages_admin(request):
    """Read what users wrote from the app and answer each one.

    Messages are grouped into one conversation per user (or per guest device) so
    the page reads as a chat console: the rail lists conversations, the pane shows
    the selected thread and the composer answers either the message the admin
    pointed at or whatever is still waiting. The list filters by app, by user kind
    (registered or guest) and by state (waiting or answered), with a free-text
    search over the address, device id and both bodies. Answering stamps the reply
    on the row and, for a signed-in sender, also sends it to their app inbox.
    """
    from django.core.paginator import Paginator

    if request.method == 'POST':
        row = get_object_or_404(UserMessage, id=request.POST.get('message_id') or 0)
        reply = (request.POST.get('reply') or '').strip()
        if not reply:
            messages.error(request, 'A reply cannot be empty.')
        elif len(reply) > MAX_USER_MESSAGE_LEN:
            messages.error(request, 'Keep the reply under %d characters.'
                           % MAX_USER_MESSAGE_LEN)
        else:
            row.reply_body = reply
            row.replied_at = timezone.now()
            row.replied_by = getattr(request.user, 'username', '') or ''
            row.is_read = True
            row.save(update_fields=['reply_body', 'replied_at', 'replied_by',
                                    'is_read'])
            delivered = _deliver_reply(row)
            log_action(request, 'user_message_reply', 'message #%d' % row.id,
                       'to=%s delivered=%s' % (row.email or row.device_id, delivered))
            messages.success(request, 'Replied to %s. %s' % (
                row.display_user,
                'Delivered to the app inbox.' if delivered
                else 'The app shows it in their message thread.'))
        return redirect(reverse('user_messages_admin') + _message_filters(request))

    rows = UserMessage.objects.select_related('app', 'notification')
    app_id = (request.GET.get('app') or '').strip()
    if app_id.isdigit():
        rows = rows.filter(app_id=int(app_id))

    user_kind = (request.GET.get('user') or 'all').lower()
    if user_kind == 'registered':
        rows = rows.exclude(email='')
    elif user_kind == 'guest':
        rows = rows.filter(email='')

    state = (request.GET.get('state') or 'open').lower()
    if state == 'open':
        rows = rows.filter(replied_at=None)
    elif state == 'answered':
        rows = rows.exclude(replied_at=None)

    q = (request.GET.get('q') or '').strip()
    if q:
        rows = rows.filter(Q(body__icontains=q) | Q(reply_body__icontains=q) |
                           Q(email__icontains=q) | Q(device_id__icontains=q))

    window = list(rows.order_by('-created_at', '-id')[:CONVERSATION_WINDOW])
    conversations = _group_conversations(window)
    by_key = {conv['key']: conv for conv in conversations}

    active_key = (request.GET.get('thread') or '').strip()
    active = by_key.get(active_key) if active_key else None
    if active is None and active_key:
        active = _conversation_from_key(active_key)
        if active is not None:
            conversations.insert(0, active)
            by_key[active['key']] = active
            active_key = active['key']
    if active is None:
        # Nothing picked yet: an unanswered thread is the one worth reading.
        active = next((conv for conv in conversations if conv['waiting']), None)
    if active is None and conversations:
        active = conversations[0]

    thread_messages = []
    reply_target = None
    if active is not None:
        active_key = active['key']
        thread_messages = _conversation_messages(active)
        # The window may have missed older messages of this thread, and the count
        # on the row is read from what the pane actually shows.
        active['total'] = len(thread_messages) or active['total']
        active['waiting'] = sum(1 for row in thread_messages if not row.replied_at)
        for row in reversed(thread_messages):
            if not row.replied_at:
                reply_target = row
                break
        if reply_target is None and thread_messages:
            reply_target = thread_messages[-1]

    page = Paginator(conversations, CONVERSATIONS_PER_PAGE).get_page(
        request.GET.get('page'))
    shown = list(page.object_list)
    if active is not None and active not in shown:
        # Keep the open thread reachable from the rail on every page.
        shown.insert(0, active)
        page.object_list = shown

    filter_qs = ''.join('&%s=%s' % (key, quote(value)) for key, value in (
        ('app', app_id),
        ('user', user_kind if user_kind != 'all' else ''),
        ('q', q),
    ) if value)

    return render(request, 'core/admin_user_messages.html', {
        'page': page,
        'conversations': conversations,
        'active': active,
        'active_key': active_key,
        'thread_messages': thread_messages,
        'reply_target': reply_target,
        'filter_qs': filter_qs,
        'thread_qs': filter_qs + '&state=' + quote(state),
        'apps': AndroidApp.objects.all().order_by('name'),
        'user_kind': user_kind,
        'state': state,
        'q': q,
        'app_id': app_id,
        'stats': {
            'total': UserMessage.objects.count(),
            'open': UserMessage.objects.filter(replied_at=None).count(),
            'registered': UserMessage.objects.exclude(email='').count(),
            'guest': UserMessage.objects.filter(email='').count(),
            'threads': (
                UserMessage.objects.exclude(email='').values('app_id', 'email')
                .distinct().count()
                + UserMessage.objects.filter(email='').values('app_id', 'device_id')
                .distinct().count()),
        },
    })


def _message_filters(request):
    """Keep the current filters on the redirect back to the list."""
    state = []
    for key in ('app', 'user', 'state', 'q', 'page', 'thread'):
        value = (request.POST.get(key) or '').strip()
        if value:
            state.append('%s=%s' % (key, quote(value)))
    return '?' + '&'.join(state) if state else ''


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

    email, auth_err = authenticate_user(request, email, app_match, endpoint='api/notifications')
    if auth_err:
        return auth_err

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

    email, auth_err = authenticate_user(request, email, app_match, endpoint='api/notifications/read')
    if auth_err:
        return auth_err
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


@csrf_exempt
def api_user_messages(request):
    """GET/POST /api/messages/ — the app's half of the conversation.

    POST stores one message: a reply to a notification (``notificationId`` set)
    or a suggestion typed from the inbox. GET returns the caller's own messages
    with the admin's answers, so the thread survives a reinstall and a guest
    device that never signed in can still be answered. Auth is the same Basic +
    app header pair every other app endpoint uses.
    """
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    if request.method == 'GET':
        email = (request.GET.get('email') or '').strip().lower()
        device_id = (request.GET.get('deviceId') or '').strip()[:100]
        if email:
            # A signed-in reading is always scoped to the credential, never to
            # the address the caller typed.
            email, auth_err = authenticate_user(request, email, app_match, endpoint='api/messages')
            if auth_err:
                return auth_err
        rows = UserMessage.objects.filter(app=app_match).select_related('notification')
        if email:
            # Messages written before signing in belong to this device too.
            rows = rows.filter(Q(email=email) | Q(email='', device_id=device_id))
        elif device_id:
            rows = rows.filter(device_id=device_id, email='')
        else:
            return JsonResponse({'status': 'error',
                                 'message': 'email or deviceId is required'}, status=400)
        return JsonResponse({'status': 'ok',
                             'messages': [_message_payload(row) for row in rows[:100]]},
                            headers={'Cache-Control': 'no-store'})

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'GET or POST required'},
                            status=405)

    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    text = (body.get('body') or '').strip()
    email = (body.get('email') or '').strip().lower()
    device_id = (body.get('deviceId') or '').strip()[:100]
    device_type = (body.get('deviceType') or '').strip()[:10]
    kind = (body.get('kind') or UserMessage.KIND_SUGGESTION).strip()
    if kind not in {value for value, _ in UserMessage.KIND_CHOICES}:
        kind = UserMessage.KIND_SUGGESTION

    if not text:
        return JsonResponse({'status': 'error', 'message': 'A message is required'}, status=400)
    if not email and not device_id:
        return JsonResponse({'status': 'error', 'message': 'email or deviceId is required'},
                            status=400)

    if email:
        email, auth_err = authenticate_user(request, email, app_match, endpoint='api/messages')
        if auth_err:
            return auth_err
        block_err = _enforce_user_block(email, app_match)
        if block_err:
            return block_err

    hour_ago = timezone.now() - timedelta(hours=1)
    recent = UserMessage.objects.filter(created_at__gte=hour_ago)
    recent = recent.filter(email=email) if email else recent.filter(device_id=device_id)
    if recent.count() >= MAX_USER_MESSAGES_PER_HOUR:
        return JsonResponse({'status': 'error',
                             'message': 'Too many messages, try again later'}, status=429)

    try:
        notification_id = int(body.get('notificationId') or 0)
    except (TypeError, ValueError):
        notification_id = 0
    notification = None
    if notification_id:
        notification = AppNotification.objects.filter(id=notification_id,
                                                      app=app_match).first()

    row = UserMessage.objects.create(
        app=app_match, notification=notification, email=email,
        device_id=device_id, device_type=device_type, kind=kind,
        body=text[:MAX_USER_MESSAGE_LEN],
    )
    return JsonResponse({'status': 'ok', 'message': _message_payload(row)},
                        headers={'Cache-Control': 'no-store'})
