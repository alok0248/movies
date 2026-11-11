"""Expire subscriptions whose end date has passed.

``SyncedUser.is_subscribed`` is what the app reads, so a row that expired but
still says subscribed keeps granting paid access. This flips those rows, records
a ``SubscriptionEvent`` for every change, and reports what it would do unless
``--apply`` is passed.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import SubscriptionEvent, SyncedUser


class Command(BaseCommand):
    help = 'Expire subscriptions whose valid_until has passed.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Save the changes.')
        parser.add_argument('--days', type=int, default=0,
                            help='Also expire rows ending within this many days.')

    def handle(self, *args, **options):
        today = timezone.localdate()
        cutoff = today + timezone.timedelta(days=options['days'])
        lapsed = SyncedUser.objects.filter(is_subscribed=True, valid_until__lt=cutoff)

        changed = 0
        for profile in lapsed.iterator():
            changed += 1
            self.stdout.write('  EXPIRE %s (ended %s, plan %r)' % (
                profile.email, profile.valid_until, profile.plan))
            if not options['apply']:
                continue
            before = {'is_subscribed': True, 'plan': profile.plan,
                      'valid_until': profile.valid_until}
            profile.is_subscribed = False
            profile.features = []
            profile.save(update_fields=['is_subscribed', 'features'])
            SubscriptionEvent.objects.create(
                profile_id=profile.pk, email=profile.email, actor='system',
                action='expired', was_subscribed=before['is_subscribed'],
                is_subscribed=False, old_plan=before['plan'], new_plan='',
                old_valid_until=before['valid_until'], new_valid_until=profile.valid_until)

        verb = 'expired' if options['apply'] else 'would expire'
        self.stdout.write(self.style.SUCCESS('%s %d subscription(s).%s' % (
            verb, changed, '' if options['apply'] else ' Re-run with --apply to save.')))
