"""Support desk workflow on top of the app's existing message channel.

The channel is already there: a user writes from the app inbox (``UserMessage``)
and the answer goes back either inline in the thread or as an app notification.
This module adds the four things that turn it into a desk:

* **auto-acknowledge** — the first message of a thread is answered immediately,
  by a machine, saying when a human will reply. Silence is what makes people
  write again, email, and leave a review;
* **canned replies** — the answers to the recurring questions, saved, so they
  are not retyped (see ``CannedReply``);
* **waiting / SLA counter** — how many messages are unanswered, how long the
  oldest has waited and the median first response, shown on the console and in
  the admin sidebar;
* **one-question CSAT** — after a human reply the user is asked whether it
  helped, through a tokenised link that needs no login.

The message row stays the source of truth for a conversation; ``SupportTicket``
holds only what a message row cannot: that the acknowledgement went out, when
the question was asked and how it was answered.
"""
import statistics
from datetime import timedelta

from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from .models import (AppNotification, AppNotificationRecipient, CannedReply,
                     SupportTicket, UserMessage)

# How long a first human answer may take before the console calls it overdue.
SLA_HOURS = 24

# The acknowledgement is deliberately plain and specific: it says what happened
# to the message and when to expect an answer, nothing more.
ACK_TITLE = 'We have your message'
ACK_BODY = ('Thanks for writing in — your message reached the team and is '
            'waiting for a reply. We usually answer within %d hours. If you '
            'think of anything else, just reply here.' % SLA_HOURS)

# Appended to a human reply once per conversation. Two links, one question.
RATING_QUESTION = 'Was this helpful?'
RATING_LINES = ('%s\nYes: %s\nNo: %s')


def thread_key(row):
    """The conversation key, taken from the console so the two always agree."""
    from .notifications import _conversation_key
    return _conversation_key(row)


def ticket_for(row):
    """The ticket for this row's conversation, created on first use."""
    import secrets
    ticket, _created = SupportTicket.objects.get_or_create(
        key=thread_key(row),
        defaults={
            'app': row.app,
            'email': (row.email or '').strip(),
            'device_id': (row.device_id or '').strip(),
            'token': secrets.token_hex(16),
        })
    return ticket


def _thread_rows(row):
    """Every message of the same conversation, newest first."""
    rows = UserMessage.objects.filter(app=row.app)
    if row.email:
        return rows.filter(email__iexact=row.email.strip())
    return rows.filter(email='', device_id=row.device_id or '')


def auto_acknowledge(row):
    """Answer the first message of a thread right away.

    Only the first message, and only for a signed-in sender. A guest has no
    inbox for a notification to arrive in — their thread is answered in place by
    a human — and inventing an acknowledgement they cannot see would only make
    the desk look faster than it is. Returns the notification, or None.

    Never raises: an app message must not fail because the acknowledgement did.
    """
    try:
        if not (row.email or '').strip():
            return None
        if _thread_rows(row).exclude(pk=row.pk).exists():
            # The conversation already started, so a human is expected on it.
            return None
        ticket = ticket_for(row)
        if ticket.acked_at:
            return None
        note = AppNotification.objects.create(
            title=ACK_TITLE, body=ACK_BODY,
            audience=AppNotification.AUDIENCE_SELECTED, app=row.app,
            created_by='support', sent_at=timezone.now())
        AppNotificationRecipient.objects.bulk_create(
            [AppNotificationRecipient(notification=note, email=row.email,
                                      device_id=row.device_id or '')],
            ignore_conflicts=True)
        note.recipients_total = 1
        note.save(update_fields=['recipients_total'])
        ticket.acked_at = timezone.now()
        ticket.save(update_fields=['acked_at'])
        return note
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            'support: could not acknowledge message #%s', getattr(row, 'id', '?'))
        return None


def ask_for_rating(row, request, reply_text):
    """Append the one-question CSAT link to a reply, once per conversation.

    The question rides on the reply because that is the message the user is
    reading when they know whether it helped. It is asked at most once per
    conversation, and the links are absolute so they survive being read in the
    app on a phone.
    """
    if not request:
        return reply_text
    try:
        ticket = ticket_for(row)
    except Exception:
        return reply_text
    if ticket.csat_asked_at:
        return reply_text
    yes = request.build_absolute_uri(reverse('support_rate', args=[ticket.token, 1]))
    no = request.build_absolute_uri(reverse('support_rate', args=[ticket.token, 0]))
    ticket.csat_asked_at = timezone.now()
    ticket.save(update_fields=['csat_asked_at'])
    return RATING_LINES % (reply_text.rstrip() + '\n\n' + RATING_QUESTION, yes, no)


def record_rating(token, rating):
    """Store one answer to the CSAT question. Returns the ticket, or None.

    The first answer wins: a second click (or a link-preview fetch) must not
    rewrite what the user said.
    """
    ticket = SupportTicket.objects.filter(token=(token or '')).first()
    if ticket is None:
        return None
    if ticket.rating is None:
        ticket.rating = rating
        ticket.rated_at = timezone.now()
        ticket.save(update_fields=['rating', 'rated_at'])
    return ticket


def support_rate(request, token, rating):
    """The page a CSAT link opens. No login: the token is the credential.

    ``rating`` is 1 (helped) or 0 (did not). Anything else is a 404, as is an
    unknown token — there is nothing here to enumerate.
    """
    if rating not in (0, 1):
        raise Http404('Unknown rating')
    ticket = record_rating(token, rating)
    if ticket is None:
        raise Http404('Unknown rating link')
    response = render(request, 'core/support_rating.html', {
        'helpful': rating == SupportTicket.RATING_HELPFUL,
        'already': ticket.rating != rating,
        'sla_hours': SLA_HOURS,
    })
    response['Cache-Control'] = 'no-store'
    return response


def stats(days=30):
    """Waiting, overdue, the median first response and the CSAT score."""
    now = timezone.now()
    waiting = UserMessage.objects.filter(replied_at=None)
    oldest = waiting.order_by('created_at').values_list('created_at', flat=True).first()
    since = now - timedelta(days=days)

    spans = []
    for created_at, replied_at in (UserMessage.objects
                                   .filter(replied_at__gte=since)
                                   .values_list('created_at', 'replied_at')):
        if created_at and replied_at:
            spans.append((replied_at - created_at).total_seconds() / 3600.0)

    rated = SupportTicket.objects.filter(rating__isnull=False, rated_at__gte=since)
    rated_count = rated.count()
    helpful = rated.filter(rating=SupportTicket.RATING_HELPFUL).count()

    return {
        'waiting': waiting.count(),
        'overdue': waiting.filter(
            created_at__lt=now - timedelta(hours=SLA_HOURS)).count(),
        'oldest_wait_hours': (round((now - oldest).total_seconds() / 3600.0, 1)
                              if oldest else 0),
        'median_first_reply_hours': (round(statistics.median(spans), 1)
                                     if spans else 0),
        'replied_window': len(spans),
        'csat_count': rated_count,
        'csat_helpful': helpful,
        'csat_pct': round(helpful * 100.0 / rated_count, 1) if rated_count else 0,
        'sla_hours': SLA_HOURS,
        'window_days': days,
    }


def canned_replies(active_only=False):
    rows = CannedReply.objects.all()
    if active_only:
        rows = rows.filter(is_active=True)
    return rows


def add_canned_reply(title, body):
    """Save one canned reply. Raises ValueError when either half is empty."""
    title = (title or '').strip()
    body = (body or '').strip()
    if not title or not body:
        raise ValueError('A canned reply needs both a title and its text.')
    return CannedReply.objects.create(title=title[:120], body=body)


def bump_canned_use(body):
    """Count which saved answers actually get used, when a reply matches one."""
    text = (body or '').strip()
    if not text:
        return None
    row = CannedReply.objects.filter(body=text).first()
    if row is None:
        return None
    CannedReply.objects.filter(pk=row.pk).update(uses=row.uses + 1)
    return row


def handle_canned_post(request):
    """Act on the console's canned-reply form. Returns ``(message, error)``."""
    action = (request.POST.get('canned_action') or '').strip()
    if not action:
        return '', ''
    if action == 'delete':
        row = CannedReply.objects.filter(pk=request.POST.get('canned_id') or 0).first()
        if row is None:
            return '', 'That canned reply no longer exists.'
        title = row.title
        row.delete()
        return 'Canned reply "%s" deleted.' % title, ''
    if action == 'add':
        try:
            row = add_canned_reply(request.POST.get('canned_title'),
                                   request.POST.get('canned_body'))
        except ValueError as exc:
            return '', str(exc)
        return 'Canned reply "%s" saved.' % row.title, ''
    return '', 'Unknown canned reply action.'
