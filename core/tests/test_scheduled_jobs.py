"""Cron heartbeats: a job that stops firing has to be visible.

``job_beat`` is what each crontab line calls; ``scheduled_jobs_status`` is the
check that reads the stamps. The three states matter: fresh (fine), stale or
failed (a finding the operator has to see), and never recorded (unknown — the
weekly jobs have not run yet on a fresh deployment, so it must not alarm).
"""
import io
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.health import SCHEDULED_JOBS, run_checks, scheduled_jobs_status
from core.models import ScheduledJob


class JobBeatTest(TestCase):
    def _beat(self, name, status=0, detail=''):
        out = io.StringIO()
        call_command('job_beat', name, '--status', str(status), '--detail', detail,
                     stdout=out)
        return out.getvalue()

    def test_first_run_creates_the_row(self):
        self._beat('backup_db')
        row = ScheduledJob.objects.get(name='backup_db')
        self.assertTrue(row.last_ok)
        self.assertEqual(1, row.runs)
        self.assertEqual(0, row.failures)
        self.assertIsNotNone(row.last_run_at)

    def test_a_failed_run_is_counted_and_marked(self):
        self._beat('backup_db')
        self._beat('backup_db', status=2, detail='disk full')
        row = ScheduledJob.objects.get(name='backup_db')
        self.assertFalse(row.last_ok)
        self.assertEqual(2, row.runs)
        self.assertEqual(1, row.failures)
        self.assertEqual(2, row.last_status)
        self.assertEqual('disk full', row.last_detail)

    def test_a_later_success_clears_the_failure_flag(self):
        self._beat('backup_db', status=1)
        self._beat('backup_db')
        row = ScheduledJob.objects.get(name='backup_db')
        self.assertTrue(row.last_ok)
        self.assertEqual(1, row.failures)


class ScheduledJobsStatusTest(TestCase):
    def _beat_all(self, status=0):
        for name, _label, _hours in SCHEDULED_JOBS:
            call_command('job_beat', name, '--status', str(status), stdout=io.StringIO())

    def test_nothing_recorded_is_unknown_not_a_failure(self):
        finding = scheduled_jobs_status()
        self.assertIsNone(finding['ok'])
        self.assertIn('never recorded yet', finding['detail'])

    def test_every_job_fresh_is_ok(self):
        self._beat_all()
        finding = scheduled_jobs_status()
        self.assertTrue(finding['ok'])
        self.assertIn('all %d jobs' % len(SCHEDULED_JOBS), finding['detail'])

    def test_a_stale_job_is_a_failure(self):
        self._beat_all()
        ScheduledJob.objects.filter(name='db_maintenance').update(
            last_run_at=timezone.now() - timedelta(days=9))
        finding = scheduled_jobs_status()
        self.assertFalse(finding['ok'])
        self.assertIn('did not run', finding['detail'])
        self.assertIn('Database maintenance', finding['detail'])

    def test_a_failed_run_is_a_failure(self):
        self._beat_all()
        call_command('job_beat', 'verify_backup', '--status', '1', stdout=io.StringIO())
        finding = scheduled_jobs_status()
        self.assertFalse(finding['ok'])
        self.assertIn('last run failed', finding['detail'])
        self.assertIn('Backup verification', finding['detail'])

    def test_a_nightly_job_is_late_after_a_day(self):
        self._beat_all()
        ScheduledJob.objects.filter(name='backup_db').update(
            last_run_at=timezone.now() - timedelta(hours=27))
        self.assertFalse(scheduled_jobs_status()['ok'])
        # …and fine at 20 hours.
        ScheduledJob.objects.filter(name='backup_db').update(
            last_run_at=timezone.now() - timedelta(hours=20))
        self.assertTrue(scheduled_jobs_status()['ok'])

    def test_the_check_runs_with_the_others(self):
        self._beat_all()
        keys = [f['key'] for f in run_checks(record=False)]
        self.assertIn('cron', keys)
        self.assertIn('backup', keys)
