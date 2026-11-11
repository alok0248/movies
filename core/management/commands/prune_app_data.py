"""Trim the app tables that grow without bound.

Visits and app logs are only useful while they are recent, and guest heartbeat
rows belong to devices that were never signed in. This prunes all three by age,
reporting first; ``--apply`` deletes.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import AndroidAppDeviceVisit, AndroidAppLog, UserHeartbeat


class Command(BaseCommand):
    help = 'Prune old device visits, app logs and inactive guest heartbeats.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Delete the rows.')
        parser.add_argument('--days', type=int, default=90,
                            help='Keep rows newer than this many days (default 90).')
        parser.add_argument('--guest-days', type=int, default=30,
                            help='Drop guest heartbeats inactive this long (default 30).')

    def handle(self, *args, **options):
        now = timezone.now()
        visit_cutoff = now - timezone.timedelta(days=options['days'])
        guest_cutoff = now - timezone.timedelta(days=options['guest_days'])

        visits = AndroidAppDeviceVisit.objects.filter(visited_at__lt=visit_cutoff)
        logs = AndroidAppLog.objects.filter(timestamp__lt=visit_cutoff)
        guests = UserHeartbeat.objects.filter(email__startswith='guest_',
                                              last_seen_at__lt=guest_cutoff)
        counts = {'visits': visits.count(), 'logs': logs.count(), 'guests': guests.count()}

        self.stdout.write('  device visits older than %dd: %d' % (options['days'], counts['visits']))
        self.stdout.write('  app logs older than %dd:     %d' % (options['days'], counts['logs']))
        self.stdout.write('  guest heartbeats idle %dd:   %d' % (options['guest_days'], counts['guests']))

        if options['apply']:
            visits.delete()
            logs.delete()
            guests.delete()
            self.stdout.write(self.style.SUCCESS('pruned %d row(s)' % sum(counts.values())))
        else:
            self.stdout.write(self.style.WARNING(
                'report only — re-run with --apply to delete %d row(s)' % sum(counts.values())))
