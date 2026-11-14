"""Fill in the country for every address already stored without one.

Addresses come from every place one is stored: the request log, the device/IP
history and the blocked-address list. Anything already answered is left alone
unless --force is passed, and an address that failed before is asked again.
Lookups are spaced out (--delay, one second by default) so the free API does not
rate-limit the run.

    python manage.py backfill_ip_countries
    python manage.py backfill_ip_countries --delay 0.5 --limit 50
    python manage.py backfill_ip_countries --dry-run
    python manage.py backfill_ip_countries --force
"""

import time

from django.core.management.base import BaseCommand

from core import geoip
from core.models import BlockedIP, DeviceIPHistory, IPLocation, RequestLog


class Command(BaseCommand):
    help = 'Look up the country for every stored address that has no cached location.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--delay', type=float, default=1.0,
            help='Seconds to wait between ipwho.is calls (default: 1.0).',
        )
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Stop after this many lookups (default: no limit).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Only report what would be looked up.',
        )
        parser.add_argument(
            '--force', action='store_true',
            help='Re-fetch addresses that already have a cached answer.',
        )

    def handle(self, *args, **options):
        delay = max(0.0, options['delay'])
        limit = max(0, options['limit'])
        dry_run = options['dry_run']
        force = options['force']

        addresses = set()
        addresses.update(
            ip for ip in RequestLog.objects.values_list('client_ip', flat=True).distinct() if ip
        )
        addresses.update(
            ip for ip in DeviceIPHistory.objects.values_list('ip', flat=True).distinct() if ip
        )
        addresses.update(
            ip for ip in BlockedIP.objects.values_list('ip_address', flat=True).distinct() if ip
        )

        skipped_private = sorted(ip for ip in addresses if not geoip.is_public(ip))
        candidates = sorted(ip for ip in addresses if geoip.is_public(ip))

        cached = {}
        for row in IPLocation.objects.filter(ip__in=candidates).values('ip', 'success', 'fetched_at'):
            if row['success']:
                cached[row['ip']] = row['fetched_at']

        pending = [ip for ip in candidates if force or ip not in cached]
        if limit:
            pending = pending[:limit]

        self.stdout.write(
            'addresses: %d total, %d public, %d already known, %d to look up%s' % (
                len(addresses), len(candidates), len(candidates) - len([ip for ip in candidates if ip not in cached]),
                len(pending), ' (dry run)' if dry_run else '',
            )
        )
        if skipped_private:
            self.stdout.write('skipped %d private/loopback address(es): %s' % (
                len(skipped_private), ', '.join(skipped_private[:8]) +
                (' …' if len(skipped_private) > 8 else ''),
            ))
        if not pending:
            self.stdout.write(self.style.SUCCESS('Nothing to do — every address has a country.'))
            return

        if dry_run:
            for ip in pending:
                self.stdout.write('  would look up %s' % ip)
            return

        done = 0
        found = 0
        failed = 0
        try:
            for position, ip in enumerate(pending, start=1):
                result = geoip.lookup(ip, force=force)
                done += 1
                if result.get('ok'):
                    found += 1
                    label = result.get('country') or 'unknown'
                    self.stdout.write('[%d/%d] %-16s %s' % (position, len(pending), ip, label))
                else:
                    failed += 1
                    self.stdout.write('[%d/%d] %-16s %s' % (
                        position, len(pending), ip, result.get('error') or 'not found'))
                if delay and position < len(pending):
                    time.sleep(delay)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('\nInterrupted — progress is already saved.'))

        self.stdout.write(self.style.SUCCESS(
            'done: %d looked up, %d with a country, %d without' % (done, found, failed)
        ))
