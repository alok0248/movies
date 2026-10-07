"""Check for scraping bots and optionally block them.

Uses the same report as the Probes & Bots admin page: an address is a candidate
when it asked for at least ``--min-hits`` pages that do not exist on the site
within ``--hours`` and carries at least one bot signal (no user agent, a
crawler/command-line client, scanner paths, or an inhuman request rate).

Dry-run by default — it prints what it found and blocks nothing. Run
``--apply`` to actually block, and leave that on a schedule only once the
dry-run output looks right:

    # what would be blocked, last 24h
    venv/bin/python manage.py block_scrapers

    # block candidates that hit 50+ missing pages in the last 6 hours
    venv/bin/python manage.py block_scrapers --hours 6 --min-hits 50 --apply
"""

from django.core.management.base import BaseCommand

from core.ip_block import block_ip
from core.probes import suspicious_ips


class Command(BaseCommand):
    help = ('Find IPs that keep asking for pages the site does not have and look '
            'automated; block them with --apply (dry-run otherwise).')

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=24,
                            help='How far back to look (default: 24).')
        parser.add_argument('--min-hits', type=int, default=20,
                            help='Minimum unknown-page hits to be a candidate (default: 20).')
        parser.add_argument('--apply', action='store_true',
                            help='Actually block the candidates (default: report only).')
        parser.add_argument('--reason', default='auto: scraping / probing',
                            help='Reason stored on the block.')
        parser.add_argument('--limit', type=int, default=25,
                            help='Never block more than this many in one run (default: 25).')

    def handle(self, *args, **options):
        hours = max(1, options['hours'])
        min_hits = max(1, options['min_hits'])
        limit = max(1, options['limit'])
        apply_blocks = options['apply']
        reason = (options['reason'] or '')[:300]

        candidates = suspicious_ips(hours=hours, min_hits=min_hits)
        self.stdout.write(
            'scraper check: last %sh, min %s unknown-page hits -> %s candidate(s)'
            % (hours, min_hits, len(candidates))
        )
        if not candidates:
            return

        for row in candidates[:limit]:
            signals = ', '.join(row['signals']) or '-'
            self.stdout.write(
                '  %-16s unknown=%-6s probes=%-5s req=%-7s rate/h=%-7s %s'
                % (row['ip'], row['unknown'], row['probes'], row['requests'],
                   row['per_hour'], signals)
            )
            self.stdout.write('      paths: %s' % ', '.join(
                '%s x%s' % (p['path'], p['count']) for p in row['paths'][:5]))

        if len(candidates) > limit:
            self.stdout.write('  ... %s more not shown (limit %s)'
                              % (len(candidates) - limit, limit))

        if not apply_blocks:
            self.stdout.write('dry-run: nothing blocked. Re-run with --apply to block these.')
            return

        blocked = 0
        for row in candidates[:limit]:
            _, created = block_ip(row['ip'], 'block_scrapers', reason)
            blocked += 1
            self.stdout.write('  blocked %s%s' % (row['ip'], '' if created else ' (re-blocked)'))
        self.stdout.write(self.style.SUCCESS('blocked %s address(es)' % blocked))
