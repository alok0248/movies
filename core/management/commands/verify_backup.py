"""Copy the databases aside and prove the copies open.

A backup nobody has opened is a hope. This copies each SQLite database to a
temporary file, runs ``PRAGMA integrity_check`` on the copy, counts the rows of
every table and prints the result — so a corrupt or truncated backup shows up
here instead of on the day it is needed.
"""
import os
import shutil
import sqlite3
import tempfile

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Copy the SQLite databases, integrity-check the copies and count rows.'

    def add_arguments(self, parser):
        parser.add_argument('--keep', help='Copy the default database here as well.')

    def handle(self, *args, **options):
        failures = 0
        for alias in ('default', 'external'):
            config = settings.DATABASES.get(alias)
            if not config or 'sqlite' not in config.get('ENGINE', ''):
                self.stdout.write('  SKIP  %s — not a SQLite database' % alias)
                continue
            source = config.get('NAME')
            if not source or not os.path.exists(source):
                self.stdout.write('  SKIP  %s — file not found' % alias)
                continue

            handle, copy = tempfile.mkstemp(prefix='backup-check-', suffix='.sqlite3')
            os.close(handle)
            try:
                shutil.copy2(source, copy)
                connection = sqlite3.connect(copy)
                integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
                tables = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                rows = 0
                for table in tables:
                    try:
                        rows += connection.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]
                    except sqlite3.Error:
                        pass
                connection.close()
                size = os.path.getsize(source) / (1024 * 1024)
                status = 'OK  ' if integrity == 'ok' else 'FAIL'
                if integrity != 'ok':
                    failures += 1
                self.stdout.write('  %s  %s — %.1f MB, %d tables, %d rows, integrity %r'
                                  % (status, alias, size, len(tables), rows, integrity))
            finally:
                if os.path.exists(copy):
                    os.remove(copy)

        if failures:
            self.stderr.write(self.style.ERROR('integrity check failed on %d database(s)' % failures))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS('backup copies open cleanly'))
