"""One timeline of admin actions.

Every destructive or protective thing an admin does leaves a row here —
blocking an address, lifting a header rule, wiping a user's cloud data,
switching the bot profile on, changing an app's settings, editing the alert
recipients. The page built on this module answers "who changed this, and
when?" in one place instead of three tables that each remembered only part.

Writing a row must never break the action it describes: ``log_action``
swallows its own errors, so a missing table or a database hiccup cannot turn a
working block button into a 500.
"""

import logging

logger = logging.getLogger(__name__)

# Human labels for the action codes stored in AdminAuditLog.action.
ACTION_LABELS = {
    'block_ip': 'Blocked an address',
    'unblock_ip': 'Lifted an address block',
    'trust_ip': 'Trusted an address',
    'untrust_ip': 'Stopped trusting an address',
    'block_header': 'Blocked a header rule',
    'unblock_header': 'Lifted a header rule',
    'delete_header': 'Deleted a header rule',
    'robots_header': 'Changed robots.txt for a rule',
    'bot_profile': 'Switched the bot profile on',
    'wipe_data': 'Wiped a user\'s cloud data',
    'app_settings': 'Changed app settings',
    'alert_settings': 'Changed alert settings',
    'alert_recipient': 'Changed alert recipients',
    'alert_test': 'Sent a test alert',
    'health_run': 'Ran the health check',
}


def client_ip(request):
    """The address the action came from, proxy header first."""
    try:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        if forwarded:
            return forwarded.split(',')[0].strip()[:64]
        return (request.META.get('REMOTE_ADDR') or '')[:64]
    except Exception:
        return ''


def log_action(request, action, target='', detail='', username=None):
    """Record one admin action. Never raises."""
    try:
        from .models import AdminAuditLog
        user = getattr(request, 'user', None)
        who = username
        if who is None:
            who = getattr(user, 'username', '') or ''
        AdminAuditLog.objects.create(
            username=str(who)[:150],
            action=(action or '')[:40],
            target=str(target or '')[:300],
            detail=str(detail or '')[:2000],
            ip_address=client_ip(request),
        )
    except Exception:
        logger.debug('audit: could not record %s', action, exc_info=True)


def action_label(action):
    return ACTION_LABELS.get(action, action.replace('_', ' ').title() if action else '—')


def audit_report(action='', username='', search='', limit=200):
    """Newest-first rows for the audit page, with the filters it offers."""
    from .models import AdminAuditLog
    qs = AdminAuditLog.objects.all()
    if action:
        qs = qs.filter(action=action)
    if username:
        qs = qs.filter(username=username)
    term = (search or '').strip()
    if term:
        from django.db.models import Q
        qs = qs.filter(Q(target__icontains=term) | Q(detail__icontains=term)
                       | Q(username__icontains=term))
    return list(qs.order_by('-created_at')[:limit])


def action_options():
    """The actions that actually appear in the table, for the filter chips."""
    from .models import AdminAuditLog
    values = (AdminAuditLog.objects.order_by('action')
              .values_list('action', flat=True).distinct()[:40])
    return [(v, action_label(v)) for v in values if v]
