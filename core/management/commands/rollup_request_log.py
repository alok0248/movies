"""Summarise finished hours of the request log before the raw rows are pruned.

``RequestLog`` keeps only its newest ``request_log.MAX_ROWS`` rows, so at busy
hours the raw history is a day at most. This walks every *finished* hour in the
lookback window, groups the rows by category and path, and writes one
``RequestLogRollup`` row per group with the hit count, the error count (4xx/5xx)
and the average / 95th-percentile latency.

The current hour is never touched — it is still being written to. Re-running
the command is safe: buckets are upserted on their unique key, so a re-run
simply refreshes the same rows rather than duplicating them.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import RequestLog, RequestLogRollup


def _p95(values):
    """95th percentile of a list of ints (0 when the list is empty)."""
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return 0
    index = max(0, min(len(values) - 1, int(len(values) * 0.95) - 1))
    return int(values[index])


class Command(BaseCommand):
    help = 'Summarise finished hours of the request log into RequestLogRollup.'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=48,
                            help='Look back this many hours for finished buckets (default 48).')
        parser.add_argument('--prune', action='store_true',
                            help='Drop rollup rows older than --keep-days after writing.')
        parser.add_argument('--keep-days', type=int, default=90,
                            help='How many days of rollup history --prune keeps (default 90).')

    def handle(self, *args, **options):
        now = timezone.now()
        # The newest bucket we may write is the one that ended before this hour.
        newest = now.replace(minute=0, second=0, microsecond=0) - timezone.timedelta(hours=1)
        oldest = newest - timezone.timedelta(hours=max(1, options['hours']) - 1)

        rows = (RequestLog.objects
                .filter(created_at__gte=oldest, created_at__lt=newest + timezone.timedelta(hours=1))
                .only('created_at', 'category', 'path', 'status_code', 'duration_ms'))

        buckets = {}
        for row in rows.iterator():
            bucket = row.created_at.replace(minute=0, second=0, microsecond=0)
            key = (bucket, row.category, row.path or '')
            entry = buckets.setdefault(key, {'hits': 0, 'errors': 0, 'ms': []})
            entry['hits'] += 1
            if (row.status_code or 0) >= 400:
                entry['errors'] += 1
            entry['ms'].append(row.duration_ms or 0)

        written = 0
        for (bucket, category, path), entry in buckets.items():
            RequestLogRollup.objects.update_or_create(
                bucket_start=bucket, category=category, path=path,
                defaults={
                    'hits': entry['hits'],
                    'errors': entry['errors'],
                    'avg_ms': int(sum(entry['ms']) / len(entry['ms'])) if entry['ms'] else 0,
                    'p95_ms': _p95(entry['ms']),
                },
            )
            written += 1

        self.stdout.write('  buckets written: %d (from %s to %s)'
                          % (written, oldest.strftime('%Y-%m-%d %H:%M'),
                             newest.strftime('%Y-%m-%d %H:%M')))

        if options['prune']:
            keep = max(1, options['keep_days'])
            cutoff = now - timezone.timedelta(days=keep)
            removed, _ = RequestLogRollup.objects.filter(bucket_start__lt=cutoff).delete()
            self.stdout.write('  pruned %d rollup row(s) older than %dd' % (removed, keep))

        self.stdout.write(self.style.SUCCESS('request log rollup complete'))
