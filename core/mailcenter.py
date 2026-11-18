"""Mail centre: compose, outbox, inbox and devices in one admin page.

Why this exists
---------------
``EmailSendLog`` used to be a write-only audit trail: a send either worked or
left a ``failed`` row with an error string nobody read, so when the SMTP box
started refusing connections the only symptom was "mail is not going out".
This module makes that same table an outbox. A campaign writes one row per
recipient with ``status='pending'`` and a shared ``batch`` key *before* the
first attempt, every attempt updates its own row, and a retry re-runs only the
rows that never left. Nothing about the message is lost, because the body is
stored too.

Around the outbox the page carries what mail alone cannot show: the app's
in-app notifications (fetched/opened tracked per user), mail and chat arriving
from ``UserMessage``, and the device list — so an admin can see who is reachable
on which build, and send to everyone or only the people they ticked.

Honesty about statuses: an SMTP box can only tell us a message was accepted, so
``sent`` is the real ceiling for plain mail. ``read`` is set when the 1x1
tracking pixel in the HTML body is fetched, and ``delivered`` is set by the app
when it fetches an in-app message. Nothing here invents a delivery receipt.
"""

import re
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.mail import EmailMessage
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .audit import log_action
from .models import (AccountDevice, AndroidApp, AppNotification,
                     AppNotificationRecipient, EmailAddress, EmailSendLog,
                     SyncedUser, UserMessage)

# One compose request sends at most this many mails. A campaign larger than
# that is written in full as ``pending`` and the rest is drained by "retry
# pending", so a slow SMTP box cannot hold a request open for minutes.
MAX_SEND_PER_REQUEST = 150

# Recipient ceiling for one campaign. Above this an admin should filter the
# audience instead of carting thousands of rows through a form.
MAX_CAMPAIGN_RECIPIENTS = 5000

AUDIENCES = [
    ('all', 'All users'),
    ('subscribed', 'Subscribed users'),
    ('unsubscribed', 'Unsubscribed users'),
    ('selected', 'Selected users'),
]

STATUS_LABELS = dict(EmailSendLog.STATUS_CHOICES)

# 1x1 transparent GIF, the smallest thing a mail client will still fetch.
_TRACKING_GIF = bytes.fromhex(
    '47494638396101000100800000ffffff00000021f90401000000002c'
    '00000000010001000002024401003b')


def _parse_emails(text):
    """Pull addresses out of free text (spaces, commas, semicolons, newlines)."""
    found = re.split(r'[\s,;]+', text or '')
    out = []
    for value in found:
        value = value.strip().strip('<>').strip()
        if '@' in value and '.' in value.split('@')[-1]:
            out.append(value)
    return out


def _dedupe(values):
    seen = set()
    out = []
    for value in values:
        key = (value or '').strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(value.strip())
    return out


def _audience_emails(audience, selected):
    """Recipients for one audience, email side.

    ``selected`` accepts any address an admin typed (mail can go to someone who
    never installed the app); the app side restricts to synced users on its own.
    """
    if audience == 'selected':
        return _dedupe(selected)
    qs = SyncedUser.objects.exclude(email='')
    if audience == 'subscribed':
        qs = qs.filter(is_subscribed=True)
    elif audience == 'unsubscribed':
        qs = qs.filter(is_subscribed=False)
    return _dedupe(list(qs.values_list('email', flat=True)))


def _pick_address(purpose='notification'):
    """The SMTP box a campaign sends through, by the same chain as every other
    mail: purpose default, purpose any, any default, any active."""
    addr = EmailAddress.objects.filter(purpose=purpose, is_active=True,
                                       is_default=True).first()
    if not addr:
        addr = EmailAddress.objects.filter(purpose=purpose, is_active=True).first()
    if not addr:
        addr = EmailAddress.objects.filter(is_active=True, is_default=True).first()
    if not addr:
        addr = EmailAddress.objects.filter(is_active=True).first()
    return addr


def _from_header(addr):
    display = (addr.display_name or '').strip()
    if not display:
        try:
            from .models import SiteSettings
            display = getattr(SiteSettings.get_settings(), 'brand_name', '') or ''
        except Exception:
            display = ''
    user_addr = addr.smtp_username or addr.email
    return '%s <%s>' % (display, user_addr) if display else user_addr


def _html_body(text, track_url):
    """The HTML half of a campaign: readable paragraphs plus the read pixel."""
    paragraphs = ''.join(
        '<p style="margin:0 0 14px">%s</p>' % line.replace('&', '&amp;')
        .replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
        for line in (text or '').strip().split('\n\n') if line.strip())
    pixel = ('<img src="%s" width="1" height="1" alt="" '
             'style="display:block;border:0">' % track_url) if track_url else ''
    return ('<!doctype html><html><body style="margin:0;padding:22px;'
            'font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
            'color:#1b1f25;background:#ffffff">'
            '<div style="max-width:560px;margin:0 auto">%s</div>%s</body></html>'
            % (paragraphs, pixel))


def _attempt(row, addr, request):
    """One send. Updates the row in place and never raises."""
    if addr is None:
        row.status = EmailSendLog.STATUS_FAILED
        row.error_message = 'No active SMTP address is configured.'
        row.save(update_fields=['status', 'error_message'])
        return False
    try:
        backend = addr.get_backend()
        mail = EmailMessage(subject=row.subject, body=row.body or '',
                            from_email=_from_header(addr), to=[row.recipient],
                            connection=backend)
        if row.track_token:
            track_url = request.build_absolute_uri(
                reverse('mail_track', args=[row.track_token]))
            mail.attach_alternative(_html_body(row.body, track_url), 'text/html')
        mail.send(fail_silently=False)
    except Exception as exc:  # SMTP, DNS, auth, TLS — all land here
        row.status = EmailSendLog.STATUS_FAILED
        row.error_message = str(exc)[:2000]
        row.save(update_fields=['status', 'error_message'])
        return False
    row.status = EmailSendLog.STATUS_SENT
    row.error_message = ''
    row.address = addr
    row.save(update_fields=['status', 'error_message', 'address'])
    return True


def _send_batch(batch, request, limit=MAX_SEND_PER_REQUEST):
    """Try every pending or failed row of a batch. Returns (sent, failed)."""
    addr = _pick_address('notification')
    rows = list(EmailSendLog.objects.filter(
        batch=batch, status__in=[EmailSendLog.STATUS_PENDING,
                                 EmailSendLog.STATUS_FAILED]).order_by('id')[:limit])
    sent = failed = 0
    for row in rows:
        if _attempt(row, addr, request):
            sent += 1
        else:
            failed += 1
    return sent, failed


def _email_campaigns():
    """Campaign rows for the outbox, one per batch, with per-status counts."""
    return list(
        EmailSendLog.objects.filter(batch__gt='')
        .values('batch', 'audience')
        .annotate(
            subject=Max('subject'),
            created=Max('created_at'),
            total=Count('id'),
            pending=Count('id', filter=Q(status=EmailSendLog.STATUS_PENDING)),
            sent=Count('id', filter=Q(status=EmailSendLog.STATUS_SENT)),
            delivered=Count('id', filter=Q(status=EmailSendLog.STATUS_DELIVERED)),
            read=Count('id', filter=Q(status=EmailSendLog.STATUS_READ)),
            failed=Count('id', filter=Q(status=EmailSendLog.STATUS_FAILED)),
        )
        .order_by('-created'))


def _inapp_campaigns():
    """In-app messages as outbox rows: pending = not fetched by a device yet."""
    notes = (AppNotification.objects
             .annotate(
                 total=Count('recipients', distinct=True),
                 delivered=Count('recipients', distinct=True,
                                 filter=~Q(recipients__delivered_at=None)),
                 read=Count('recipients', distinct=True,
                            filter=~Q(recipients__read_at=None)))
             .order_by('-created_at')[:200])
    rows = []
    for note in notes:
        total = note.total or note.recipients_total or 0
        rows.append({
            'kind': 'inapp',
            'key': 'note:%d' % note.id,
            'subject': note.title,
            'audience': note.get_audience_display(),
            'created': note.sent_at or note.created_at,
            'total': total,
            'pending': max(0, total - note.delivered),
            'sent': note.delivered,
            'delivered': note.delivered,
            'read': note.read,
            'failed': 0,
            'url': reverse('notification_detail', args=[note.id]),
            'is_active': note.is_active,
        })
    return rows


def _outbox_rows(status='all', q='', kind='all'):
    rows = []
    if kind in ('all', 'email'):
        for campaign in _email_campaigns():
            campaign = dict(campaign)
            campaign['kind'] = 'email'
            campaign['key'] = campaign['batch']
            campaign['url'] = reverse('mail_campaign_detail',
                                      args=[campaign['batch']])
            rows.append(campaign)
    if kind in ('all', 'inapp'):
        rows.extend(_inapp_campaigns())

    if q:
        needle = q.lower()
        rows = [row for row in rows if needle in (row['subject'] or '').lower()]
    if status != 'all':
        rows = [row for row in rows if (row.get(status) or 0) > 0]
    rows.sort(key=lambda row: row['created'] or timezone.now(), reverse=True)
    return rows


def _stats():
    base = EmailSendLog.objects.filter(batch__gt='')
    inapp = AppNotification.objects.count()
    return {
        'pending': base.filter(status=EmailSendLog.STATUS_PENDING).count(),
        'sent': base.filter(status=EmailSendLog.STATUS_SENT).count(),
        'delivered': base.exclude(delivered_at=None).count(),
        'read': base.exclude(read_at=None).count(),
        'failed': base.filter(status=EmailSendLog.STATUS_FAILED).count(),
        'campaigns': base.values('batch').distinct().count(),
        'inapp': inapp,
        'inbox_open': UserMessage.objects.filter(replied_at=None).count(),
        'inbox_total': UserMessage.objects.count(),
        'devices': AccountDevice.objects.count(),
        'users': SyncedUser.objects.exclude(email='').count(),
    }


def _device_rows(q='', version=''):
    """Devices with the build they run and the account behind them.

    ``AccountDevice`` is the per-device record; a ``SyncedUser`` whose device id
    has no such row yet still counts, so a fresh install is never invisible.
    """
    rows = []
    seen = set()
    for dev in AccountDevice.objects.order_by('-last_seen_at')[:2000]:
        key = ((dev.email or '').lower(), dev.device_id or '')
        seen.add(key)
        rows.append({
            'email': dev.email, 'device_id': dev.device_id,
            'device_model': dev.device_model or '',
            'app_version': dev.app_version or '',
            'last_seen': dev.last_seen_at, 'source': 'account device',
        })
    for user in SyncedUser.objects.order_by('-last_synced_at')[:2000]:
        key = ((user.email or '').lower(), user.device_id or '')
        if key in seen:
            continue
        rows.append({
            'email': user.email, 'device_id': user.device_id or '',
            'device_model': user.device_model or '',
            'app_version': user.app_version or '',
            'last_seen': user.last_synced_at, 'source': 'account',
        })
    if q:
        needle = q.lower()
        rows = [row for row in rows
                if needle in (row['email'] or '').lower()
                or needle in (row['device_id'] or '').lower()
                or needle in (row['device_model'] or '').lower()]
    if version:
        rows = [row for row in rows if version in (row['app_version'] or '')]
    return rows


@login_required
@user_passes_test(lambda u: u.is_staff or u.is_superuser)
def mail_center(request):
    """The page: outbox, compose, inbox and devices under one roof."""
    tab = (request.GET.get('tab') or 'outbox').lower()
    if tab not in ('outbox', 'compose', 'inbox', 'devices'):
        tab = 'outbox'

    context = {
        'tab': tab,
        'stats': _stats(),
        'statuses': EmailSendLog.STATUS_CHOICES,
        'audiences': AUDIENCES,
        'apps': AndroidApp.objects.all().order_by('name'),
        'address': _pick_address('notification'),
    }

    if tab == 'compose':
        context['to_prefill'] = ', '.join(_parse_emails(request.GET.get('to')))
        context['audience'] = (request.GET.get('audience') or 'selected').lower()
        context['subject'] = request.GET.get('subject') or ''

    elif tab == 'inbox':
        state = (request.GET.get('state') or 'all').lower()
        user_kind = (request.GET.get('user') or 'all').lower()
        kind = (request.GET.get('kind') or 'all').lower()
        q = (request.GET.get('q') or '').strip()
        rows = UserMessage.objects.select_related('app', 'notification')
        if state == 'open':
            rows = rows.filter(replied_at=None)
        elif state == 'answered':
            rows = rows.exclude(replied_at=None)
        if user_kind == 'registered':
            rows = rows.exclude(email='')
        elif user_kind == 'guest':
            rows = rows.filter(email='')
        if kind in (UserMessage.KIND_SUGGESTION, UserMessage.KIND_REPLY):
            rows = rows.filter(kind=kind)
        if q:
            rows = rows.filter(Q(body__icontains=q) | Q(reply_body__icontains=q)
                               | Q(email__icontains=q) | Q(device_id__icontains=q))
        context.update({
            'rows': Paginator(rows.order_by('-created_at', '-id'), 40)
                    .get_page(request.GET.get('page')),
            'state': state, 'user_kind': user_kind, 'kind': kind, 'q': q,
        })

    elif tab == 'devices':
        q = (request.GET.get('q') or '').strip()
        version = (request.GET.get('version') or '').strip()
        context.update({
            'rows': Paginator(_device_rows(q, version), 100)
                    .get_page(request.GET.get('page')),
            'q': q, 'version': version,
            'versions': sorted({row['app_version'] for row in _device_rows()
                                if row['app_version']}, reverse=True)[:40],
        })

    else:
        status = (request.GET.get('status') or 'all').lower()
        if status not in dict(EmailSendLog.STATUS_CHOICES):
            status = 'all'
        q = (request.GET.get('q') or '').strip()
        kind = (request.GET.get('kind') or 'all').lower()
        rows = _outbox_rows(status, q, kind)
        context.update({
            'rows': Paginator(rows, 20).get_page(request.GET.get('page')),
            'status': status, 'q': q, 'kind': kind,
            # Transactional sends (verification, resets, test mail) have no
            # batch; they still belong on the page, just not in a campaign row.
            'singles': EmailSendLog.objects.filter(batch='')
                       .select_related('address').order_by('-created_at')[:25],
        })

    return render(request, 'core/admin_mail_center.html', context)


@login_required
@user_passes_test(lambda u: u.is_staff or u.is_superuser)
def mail_send(request):
    """Compose handler: write the batch as pending, then attempt it."""
    if request.method != 'POST':
        return redirect('mail_center')

    audience = (request.POST.get('audience') or 'all').lower()
    if audience not in dict(AUDIENCES):
        audience = 'all'
    subject = (request.POST.get('subject') or '').strip()[:500]
    body = (request.POST.get('body') or '').strip()
    link_url = (request.POST.get('link_url') or '').strip()[:500]
    send_email = request.POST.get('send_email') == '1'
    send_inapp = request.POST.get('send_inapp') == '1'
    selected = _parse_emails(request.POST.get('selected_emails'))
    app_obj = None
    app_id = (request.POST.get('app') or '').strip()
    if app_id.isdigit():
        app_obj = AndroidApp.objects.filter(id=int(app_id)).first()

    if not subject:
        messages.error(request, 'A subject is required.')
        return redirect(reverse('mail_center') + '?tab=compose')
    if not (send_email or send_inapp):
        messages.error(request, 'Pick at least one channel: email, in-app, or both.')
        return redirect(reverse('mail_center') + '?tab=compose')
    if audience == 'selected' and not selected:
        messages.error(request, 'Pick at least one recipient for a selected audience.')
        return redirect(reverse('mail_center') + '?tab=compose')
    if not body:
        messages.error(request, 'A message body is required.')
        return redirect(reverse('mail_center') + '?tab=compose')

    emails = _audience_emails(audience, selected)
    if not emails:
        messages.error(request, 'That audience has no addresses to send to.')
        return redirect(reverse('mail_center') + '?tab=compose')
    if len(emails) > MAX_CAMPAIGN_RECIPIENTS:
        messages.error(request, 'That is %d recipients; the limit for one send is %d.'
                       % (len(emails), MAX_CAMPAIGN_RECIPIENTS))
        return redirect(reverse('mail_center') + '?tab=compose')

    batch = uuid.uuid4().hex[:16]
    created_by = getattr(request.user, 'username', '') or ''
    sent = failed = 0
    if send_email:
        EmailSendLog.objects.bulk_create([
            EmailSendLog(batch=batch, subject=subject, body=body,
                         purpose='notification', audience=audience,
                         recipient=email, status=EmailSendLog.STATUS_PENDING,
                         track_token=uuid.uuid4().hex,
                         sent_by=request.user if request.user.is_authenticated else None,
                         source='notification')
            for email in emails
        ])
        sent, failed = _send_batch(batch, request)

    inapp_total = 0
    if send_inapp:
        # The app can only reach synced accounts, so this is the same audience
        # filtered to addresses that exist in the users DB.
        from .notifications import _notification_audience_emails
        targets = list(dict.fromkeys(
            email for email in _notification_audience_emails(audience, emails)
            if email))
        note = AppNotification.objects.create(
            title=subject[:200], body=body, link_url=link_url,
            audience=AppNotification.AUDIENCE_SELECTED if audience == 'selected'
            else (AppNotification.AUDIENCE_SUBSCRIBED if audience == 'subscribed'
                  else (AppNotification.AUDIENCE_UNSUBSCRIBED
                        if audience == 'unsubscribed' else AppNotification.AUDIENCE_ALL)),
            app=app_obj, created_by=created_by, sent_at=timezone.now(),
        )
        if audience == 'selected' or app_obj is not None:
            AppNotificationRecipient.objects.bulk_create(
                [AppNotificationRecipient(notification=note, email=email)
                 for email in targets], ignore_conflicts=True)
        note.recipients_total = len(targets)
        note.save(update_fields=['recipients_total'])
        inapp_total = len(targets)

    log_action(request, 'mail_send', subject[:80],
               'batch=%s audience=%s email=%d inapp=%d'
               % (batch, audience, len(emails) if send_email else 0, inapp_total))

    if send_email:
        waiting = len(emails) - sent - failed
        messages.success(request, 'Queued for %d recipient%s: %d sent, %d failed, %d waiting. '
                         'Open the campaign to retry what did not leave.'
                         % (len(emails), '' if len(emails) == 1 else 's',
                            sent, failed, waiting))
        return redirect(reverse('mail_campaign_detail', args=[batch]))
    messages.success(request, 'In-app message sent to %d user%s.'
                     % (inapp_total, '' if inapp_total == 1 else 's'))
    return redirect(reverse('mail_center') + '?tab=outbox&kind=inapp')


@login_required
@user_passes_test(lambda u: u.is_staff or u.is_superuser)
def mail_campaign_detail(request, batch):
    """One campaign: every recipient, its status, its error and its read time."""
    rows = EmailSendLog.objects.filter(batch=batch).select_related('address')
    if not rows.exists():
        messages.error(request, 'That campaign no longer exists.')
        return redirect('mail_center')
    status = (request.GET.get('status') or 'all').lower()
    q = (request.GET.get('q') or '').strip()
    if status != 'all':
        rows = rows.filter(status=status)
    if q:
        rows = rows.filter(recipient__icontains=q)

    first = EmailSendLog.objects.filter(batch=batch).order_by('id').first()
    counts = {code: EmailSendLog.objects.filter(batch=batch, status=code).count()
              for code, _label in EmailSendLog.STATUS_CHOICES}
    return render(request, 'core/admin_mail_campaign.html', {
        'batch': batch,
        'campaign': first,
        'counts': counts,
        'total': sum(counts.values()),
        'read_total': EmailSendLog.objects.filter(batch=batch).exclude(read_at=None).count(),
        'statuses': EmailSendLog.STATUS_CHOICES,
        'status': status,
        'q': q,
        'rows': Paginator(rows.order_by('id'), 100).get_page(request.GET.get('page')),
    })


@require_POST
@login_required
@user_passes_test(lambda u: u.is_staff or u.is_superuser)
def mail_retry(request, batch):
    """Re-attempt the rows of a batch that never left."""
    if batch == 'pending':
        # Everything still pending or failed, not just one campaign.
        ids = list(EmailSendLog.objects.filter(
            batch__gt='', status__in=[EmailSendLog.STATUS_PENDING,
                                      EmailSendLog.STATUS_FAILED])
                   .values_list('batch', flat=True).distinct()[:20])
        sent = failed = 0
        for key in ids:
            s, f = _send_batch(key, request)
            sent += s
            failed += f
        log_action(request, 'mail_retry_all', 'pending batches=%d' % len(ids),
                   'sent=%d failed=%d' % (sent, failed))
        messages.success(request, '%d sent, %d still failing.' % (sent, failed))
        return redirect(reverse('mail_center') + '?tab=outbox')

    if not EmailSendLog.objects.filter(batch=batch).exists():
        messages.error(request, 'That campaign no longer exists.')
        return redirect('mail_center')
    sent, failed = _send_batch(batch, request)
    log_action(request, 'mail_retry', 'batch=%s' % batch,
               'sent=%d failed=%d' % (sent, failed))
    if sent or failed:
        messages.success(request, '%d sent, %d still failing.' % (sent, failed))
    else:
        messages.info(request, 'Nothing to retry in that campaign.')
    return redirect(reverse('mail_campaign_detail', args=[batch]))


@login_required
@user_passes_test(lambda u: u.is_staff or u.is_superuser)
def mail_export(request, batch):
    """CSV of one campaign, per recipient, for a record outside the panel."""
    rows = EmailSendLog.objects.filter(batch=batch).order_by('id')
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="mail-%s.csv"' % batch
    response.write('recipient,status,sent_at,read_at,error\n')
    for row in rows.iterator():
        response.write('%s,%s,%s,%s,"%s"\n' % (
            row.recipient, row.status,
            row.created_at.strftime('%Y-%m-%d %H:%M:%S') if row.created_at else '',
            row.read_at.strftime('%Y-%m-%d %H:%M:%S') if row.read_at else '',
            (row.error_message or '').replace('"', "'")))
    return response


@csrf_exempt
def mail_track(request, token):
    """The read pixel. Public by design: the recipient's mail client fetches it."""
    if token:
        row = EmailSendLog.objects.filter(track_token=token).first()
        if row is not None and row.read_at is None:
            row.read_at = timezone.now()
            fields = ['read_at']
            if row.status in (EmailSendLog.STATUS_SENT, EmailSendLog.STATUS_DELIVERED):
                row.status = EmailSendLog.STATUS_READ
                fields.append('status')
            row.save(update_fields=fields)
    response = HttpResponse(_TRACKING_GIF, content_type='image/gif')
    response['Cache-Control'] = 'no-store, max-age=0'
    return response
