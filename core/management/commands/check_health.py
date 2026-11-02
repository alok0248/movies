"""Run the health checks and alert by email / Telegram.

Cron on the server:

    */5 * * * * cd /home/ubuntu/project/movies && ./venv/bin/python manage.py check_health >> /tmp/health.log 2>&1

``--no-send`` only prints what the checks see, which is handy when tuning the
thresholds. Exit code is 1 when a check is failing, so cron mailers and
monitoring wrappers can see it too.
"""

from django.core.management.base import BaseCommand

from core import health


class Command(BaseCommand):
    help = 'Check 5xx rate, disk space and gunicorn workers; alert when something is wrong.'

    def add_arguments(self, parser):
        parser.add_argument('--no-send', action='store_true',
                            help='Only print the findings, never send an alert.')
        parser.add_argument('--force', action='store_true',
                            help='Send even when every check passes (a test message).')

    def handle(self, *args, **options):
        if options['no_send']:
            findings = health.run_checks()
            for f in findings:
                mark = {True: 'OK', False: 'PROBLEM', None: 'unknown'}[f.get('ok')]
                self.stdout.write('[%s] %s — %s' % (mark, f['title'], f['detail']))
            failing = [f for f in findings if f.get('ok') is False]
            if failing:
                raise SystemExit(1)
            return

        result = health.dispatch(force=options['force'])
        for f in result['findings']:
            mark = {True: 'OK', False: 'PROBLEM', None: 'unknown'}[f.get('ok')]
            self.stdout.write('[%s] %s — %s' % (mark, f['title'], f['detail']))
        if result['throttled']:
            self.stdout.write('Alert already sent recently — not repeating it.')
        if result['sent_email']:
            self.stdout.write('Email sent to %d recipient(s).' % result['sent_email'])
        if result['sent_telegram']:
            self.stdout.write('Telegram message sent.')
        for error in result['errors']:
            self.stderr.write('Alert delivery: ' + error)
        if any(f.get('ok') is False for f in result['findings']):
            raise SystemExit(1)
