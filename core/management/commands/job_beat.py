"""Record that a cron job ran, with its exit status.

Every crontab line ends with this command, so the health check can see a job
that stopped firing — the one failure a database cannot report by itself:

    5 4 * * 0 cd /home/ubuntu/project/movies && ./venv/bin/python manage.py db_maintenance --vacuum >> /home/ubuntu/maintenance.log 2>&1; ./venv/bin/python manage.py job_beat db_maintenance --status $?

``$?`` is the status of the command just before it, so a job that ran and failed
is recorded as failed rather than as missing. This command itself always exits
0 (unless the name is empty), so cron stays quiet and the finding is reported by
``check_health`` instead.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import ScheduledJob


class Command(BaseCommand):
    help = 'Record that a scheduled job ran (called by the crontab lines).'

    def add_arguments(self, parser):
        parser.add_argument('name', help='Job name, e.g. backup_db.')
        parser.add_argument('--status', type=int, default=0,
                            help='Exit status of the job (cron: pass $?).')
        parser.add_argument('--detail', default='',
                            help='Optional short note stored with the run.')
        parser.add_argument('--label', default='',
                            help='Human label, kept from the first run.')

    def handle(self, *args, **options):
        name = (options['name'] or '').strip()
        if not name:
            raise CommandError('a job name is required')
        status = options['status']
        now = timezone.now()

        row, created = ScheduledJob.objects.get_or_create(
            name=name, defaults={'label': options['label'] or name})
        if created and options['label']:
            row.label = options['label']
        row.last_run_at = now
        row.last_status = status
        row.last_ok = status == 0
        row.runs = (row.runs or 0) + 1
        if status:
            row.failures = (row.failures or 0) + 1
        row.last_detail = (options['detail'] or '')[:255]
        row.save(update_fields=['label', 'last_run_at', 'last_status', 'last_ok',
                                'runs', 'failures', 'last_detail'])

        self.stdout.write('%s: %s%s' % (
            name, 'ok' if status == 0 else 'failed (exit %d)' % status,
            '' if created else ' (run %d)' % row.runs))
