"""Per-device chat, watch history and the "login as" preview.

The full user list (``admin-dashboard/android-apps/<id>/users-full/``) has one
row per account/device and two actions open from a row:

* ``user_chat_panel`` - everything that has passed between that device and the
  admin: the messages it wrote from the app inbox, the answers it got, the
  notifications delivered to it, and the titles it watched.
* ``login_as_preview`` - a separate browser session that renders the website as
  that user. The session key travels in the URL (``/preview/<key>/...``) and
  ``PreviewSessionMiddleware`` swaps ``request.session``/``request.user`` for
  the prefixed request only, so the admin's own session in the other tab is
  never touched, and the link works in an incognito window too.

Sending an answer reuses the app inbox, exactly like the messages console: a
signed-in user gets an ``AppNotification`` (what their app shows as a message),
and when the device wrote from the inbox its row is stamped as well, so its
thread shows the answer inline.
"""

from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, set_script_prefix
from django.utils import timezone

from .audit import log_action
from .models import (AndroidApp, AppNotification, AppNotificationRecipient,
                     PlayHistory, SyncedUser, UserCloudData, UserHeartbeat,
                     UserMessage)
from .notifications import MAX_USER_MESSAGE_LEN, _deliver_reply
from .preview_middleware import session_store as _session_store
from .views import is_staff_or_superuser

# How much of one conversation the popup carries: the thread oldest-first, the
# notifications delivered to the device, and the watch history beside it.
THREAD_LIMIT = 200
NOTIFICATION_LIMIT = 40
TIMELINE_LIMIT = 120
WATCH_LIMIT = 20

# A preview session is a real sign-in for the chosen user, so it is short-lived
# and only ever created by a staff member from the user list.
PREVIEW_SESSION_SECONDS = 4 * 60 * 60


def _thread_rows(app_obj, email, device_id):
    """The messages of one thread: everything this account/device exchanged.

    Mirrors the app's own filter (``api_user_messages``): a signed-in account
    also owns the messages its device wrote before signing in.
    """
    rows = UserMessage.objects.filter(app=app_obj).select_related('notification')
    email = (email or '').strip()
    device_id = (device_id or '').strip()
    if email and device_id:
        rows = rows.filter(Q(email__iexact=email) |
                           Q(email='', device_id=device_id))
    elif email:
        rows = rows.filter(email__iexact=email)
    elif device_id:
        rows = rows.filter(email='', device_id=device_id)
    else:
        return rows.none()
    # Unsliced: callers filter it further (a waiting message) before reading.
    return rows.order_by('-created_at', '-id')


def _delivered_notifications(email, device_id):
    """What the admin already pushed to this device, read state included."""
    if not email and not device_id:
        return []
    rows = AppNotificationRecipient.objects.select_related('notification')
    if email and device_id:
        rows = rows.filter(Q(email__iexact=email) |
                           Q(email='', device_id=device_id))
    elif email:
        rows = rows.filter(email__iexact=email)
    else:
        rows = rows.filter(email='', device_id=device_id)
    return list(rows.order_by('-notification__sent_at', '-notification__created_at')
                [:NOTIFICATION_LIMIT])


def _timeline(app_obj, email, device_id):
    """The conversation in reading order, admin side and user side together."""
    entries = []
    rows = list(_thread_rows(app_obj, email, device_id)[:THREAD_LIMIT])
    rows.reverse()  # oldest first
    for row in rows:
        entries.append({
            'side': 'in',
            'who': row.display_user,
            'title': ('reply to "%s"' % row.notification.title
                      if row.notification and row.notification.title else ''),
            'body': row.body,
            'at': row.created_at,
        })
        if row.replied_at:
            entries.append({
                'side': 'out',
                'who': row.replied_by or 'Admin',
                'title': 'answered',
                'body': row.reply_body,
                'at': row.replied_at,
            })
    for recipient in _delivered_notifications(email, device_id):
        note = recipient.notification
        entries.append({
            'side': 'note',
            'who': 'Admin',
            'title': ('read' if recipient.read_at else 'notification'),
            'body': (('%s\n%s' % (note.title, note.body)).strip()
                     if note else ''),
            'at': recipient.delivered_at or note.sent_at or note.created_at,
        })
    entries.sort(key=lambda e: e['at'] or timezone.now())
    return entries[-TIMELINE_LIMIT:]


def _pct(done, total):
    try:
        if not total:
            return 0
        return max(0, min(100, int(done * 100 / total)))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0


def _watch_history(user):
    """The titles this account watched, newest first.

    ``PlayHistory`` is the watch history the app syncs; when an account has
    none yet, the cloud blob's own list is shown so the popup is still useful.
    """
    rows = []
    if not user.user_id:
        return rows
    for play in (PlayHistory.objects.filter(user_id=user.user_id)
                 .order_by('-last_played_at')[:WATCH_LIMIT]):
        total = play.total_duration_seconds or 0
        watched = play.duration_seconds or 0
        rows.append({
            'title': play.title or 'Untitled',
            'kind': 'TV' if play.media_type == 'tv' else 'Movie',
            'episode': ('S%sE%s' % (play.season_number, play.episode_number)
                        if play.season_number and play.episode_number else ''),
            'episode_title': play.episode_title or '',
            'at': play.last_played_at,
            'completed': play.completed,
            'pct': _pct(watched, total),
            'watched_min': int(watched // 60),
            'total_min': int(total // 60),
            'from_cloud': False,
        })
    if rows:
        return rows
    cloud = UserCloudData.objects.filter(user_id=user.user_id).first()
    for item in ((cloud.watch_history or []) if cloud else []):
        if not isinstance(item, dict):
            continue
        title = item.get('title') or item.get('name') or ''
        if not title:
            continue
        episode = ''
        if item.get('isTv'):
            season = item.get('lastSeasonNumber', item.get('season', -1))
            number = item.get('lastEpisodeNumber', item.get('episode', -1))
            try:
                season, number = int(season), int(number)
            except (TypeError, ValueError):
                season = number = -1
            if season >= 0 and number >= 0:
                episode = 'S%sE%s' % (season, number)
        rows.append({
            'title': title,
            'kind': 'TV' if item.get('isTv') else 'Movie',
            'episode': episode,
            'episode_title': '',
            'at': None,
            'completed': False,
            'pct': 0,
            'watched_min': 0,
            'total_min': 0,
            'from_cloud': True,
        })
        if len(rows) >= WATCH_LIMIT:
            break
    return rows


def _device_activity(email, device_id):
    """What the device is doing right now, from its last heartbeat."""
    if not email and not device_id:
        return None
    rows = UserHeartbeat.objects.all()
    rows = (rows.filter(email__iexact=email) if email
            else rows.filter(device_id=device_id, email=''))
    hb = rows.order_by('-last_seen_at').first()
    if hb is None:
        return None
    return {
        'online': hb.is_online,
        'seen': hb.last_seen_at,
        'screen': hb.current_screen or '',
        'title': hb.media_title or '',
        'playing': hb.is_playing,
        'pct': _pct(hb.playback_position_ms, hb.duration_ms),
        'actions': [str(a) for a in (hb.recent_actions or []) if str(a).strip()][:6],
    }


def _send_message(app_obj, user, text, request):
    """Deliver one admin message. Returns ``(sent, error)``."""
    who = getattr(request.user, 'username', '') or ''
    waiting = (_thread_rows(app_obj, user.email, user.device_id)
               .filter(replied_at=None).order_by('-created_at', '-id').first())
    if waiting is not None:
        # The device asked something: the answer rides on its own row so the
        # app's thread shows it inline, and (for an address) the inbox gets it.
        waiting.reply_body = text
        waiting.replied_at = timezone.now()
        waiting.replied_by = who
        waiting.is_read = True
        waiting.save(update_fields=['reply_body', 'replied_at', 'replied_by',
                                    'is_read'])
        delivered = _deliver_reply(waiting)
        log_action(request, 'user_message_reply', 'message #%d' % waiting.id,
                   'to=%s delivered=%s from=user_list'
                   % (waiting.email or waiting.device_id, delivered))
        return ('Reply sent to %s.%s' % (
            user.display_name or waiting.display_user,
            ' Delivered to the app inbox.' if delivered
            else ' The app shows it in its message thread.'), '')
    if not user.email:
        return '', ('This device never wrote from the app inbox, so there is '
                    'no thread to answer.')
    note = AppNotification.objects.create(
        title='Message from support', body=text,
        audience=AppNotification.AUDIENCE_SELECTED, app=app_obj,
        created_by=who, sent_at=timezone.now())
    AppNotificationRecipient.objects.create(
        notification=note, email=user.email, device_id=user.device_id or '')
    note.recipients_total = 1
    note.save(update_fields=['recipients_total'])
    log_action(request, 'user_message_send', 'user=%s' % (user.email or user.device_id),
               'notification=%d' % note.id)
    return 'Message sent to %s.' % user.email, ''


def _is_ajax(request):
    return (request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.GET.get('partial') == '1')


@login_required
@user_passes_test(is_staff_or_superuser)
def user_chat_panel(request, app_id, user_id):
    """The popup behind the row's Message button: chat and watch history.

    GET renders the panel, POST sends one message and renders it again with the
    message in place (the popup posts with fetch, so the admin never leaves the
    list).
    """
    app_obj = get_object_or_404(AndroidApp, id=app_id)
    user = get_object_or_404(SyncedUser, id=user_id)
    email = (user.email or '').strip()
    device_id = (user.device_id or '').strip()

    sent = ''
    error = ''
    if request.method == 'POST':
        text = (request.POST.get('body') or '').strip()
        if not text:
            error = 'Write a message first.'
        elif len(text) > MAX_USER_MESSAGE_LEN:
            error = 'Keep the message under %d characters.' % MAX_USER_MESSAGE_LEN
        else:
            sent, error = _send_message(app_obj, user, text, request)
        if not _is_ajax(request):
            if sent:
                django_messages.success(request, sent)
            if error:
                django_messages.error(request, error)
            return redirect(reverse('android_app_users_full', args=[app_obj.id])
                            + '?tab=accounts')

    return render(request, 'core/_user_chat_panel.html', {
        'app': app_obj,
        'user': user,
        'email': email,
        'device_id': device_id,
        'timeline': _timeline(app_obj, email, device_id),
        'watch': _watch_history(user),
        'activity': _device_activity(email, device_id),
        'max_len': MAX_USER_MESSAGE_LEN,
        'sent': sent,
        'error': error,
        # A guest device can only be answered inside a thread it started.
        'can_send': bool(email) or _thread_rows(app_obj, email, device_id).exists(),
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def login_as_preview(request, app_id, user_id):
    """Mint a separate session that opens the website as this user.

    Nothing is written to the admin's own session: the new session's key is
    returned as a URL and ``PreviewSessionMiddleware`` only honours it for
    requests under ``/preview/<key>/``. It is refused for administrator
    accounts, so a preview can never be a shortcut to more rights.
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    app_obj = get_object_or_404(AndroidApp, id=app_id)
    user = get_object_or_404(SyncedUser, id=user_id)

    if not user.user_id:
        return JsonResponse({'ok': False, 'error': (
            'This user has no website account — they have only used the app, '
            'so there is nothing to sign in to.')}, status=400)
    UserModel = get_user_model()
    target = UserModel.objects.filter(pk=user.user_id).first()
    if target is None:
        return JsonResponse({'ok': False, 'error': 'That website account no longer exists.'},
                            status=400)
    if target.is_staff or target.is_superuser:
        return JsonResponse({'ok': False, 'error': (
            'That account is an administrator. Login as is for customer '
            'accounts only.')}, status=400)
    if not target.is_active:
        return JsonResponse({'ok': False, 'error': 'That website account is not active.'},
                            status=400)

    session = _session_store()()
    session['_auth_user_id'] = str(target.pk)
    session['_auth_user_backend'] = (settings.AUTHENTICATION_BACKENDS[0]
                                     if settings.AUTHENTICATION_BACKENDS
                                     else 'django.contrib.auth.backends.ModelBackend')
    session['_auth_user_hash'] = target.get_session_auth_hash()
    session['preview_by'] = getattr(request.user, 'username', '') or ''
    session['preview_email'] = user.email or getattr(target, 'email', '') or ''
    session['preview_device'] = user.device_id or ''
    session['preview_return'] = (reverse('android_app_users_full', args=[app_obj.id])
                                 + '?tab=accounts')
    session.set_expiry(PREVIEW_SESSION_SECONDS)
    session.create()

    log_action(request, 'user_login_as', 'user #%d' % user.id,
               'email=%s session=%s' % (user.email or user.device_id,
                                        session.session_key[:8]))
    return JsonResponse({'ok': True, 'url': '/preview/%s/' % session.session_key,
                         'email': session['preview_email'], 'error': ''})


def preview_exit(request):
    """Close the preview session and hand the window back to the admin list.

    Staff-only cannot be required here: inside the preview ``request.user`` is
    the customer. What is required is the preview marker the middleware only
    ever finds on a session minted by ``login_as_preview``.
    """
    if not request.session.get('preview_by'):
        return HttpResponse('Not a preview session.', status=403)
    back = request.session.get('preview_return') or reverse('admin_dashboard')
    request.session.flush()
    set_script_prefix('/')
    return redirect(back)
