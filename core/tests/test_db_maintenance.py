"""The weekly SQLite look: quick_check, bloat reporting and the optional VACUUM.

The command works on the file, not through the ORM, so the tests point it at
their own temporary databases: one healthy file with deleted rows to compact,
one without anything to reclaim, and one that is not a database at all — the
case that must report a failure instead of raising.
"""
import io
import os
import sqlite3
import tempfile
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase

from core.management.commands.db_maintenance import Command


def _temp_db():
    handle, path = tempfile.mkstemp(prefix='db-maintenance-', suffix='.sqlite3')
    os.close(handle)
    return path


def _fill(path, rows=4000, delete=3900):
    """One table, filled and then mostly emptied, so the file has free pages."""
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=MEMORY')
    connection.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)')
    connection.executemany('INSERT INTO t (blob) VALUES (?)',
                           [('x' * 200,) for _ in range(rows)])
    connection.commit()
    connection.execute('DELETE FROM t WHERE id > ?', (rows - delete,))
    connection.commit()
    connection.close()


def _options(**over):
    options = {'full': False, 'vacuum': False, 'vacuum_if_bloat': 20.0,
               'vacuum_always': False}
    options.update(over)
    return options


class DbMaintenanceUnitTest(SimpleTestCase):
    def setUp(self):
        self.path = _temp_db()
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _run(self, **over):
        command = Command(stdout=io.StringIO(), stderr=io.StringIO())
        ok = command._check('default', self.path, _options(**over))
        return ok, command.stdout.getvalue()

    def test_reports_pages_bloat_and_busy_timeout(self):
        _fill(self.path)
        ok, out = self._run()
        self.assertTrue(ok)
        self.assertIn('quick_check', out)
        self.assertIn('bloat', out)
        self.assertIn('busy timeout', out)

    def test_vacuum_reclaims_the_free_pages(self):
        _fill(self.path)
        before = os.path.getsize(self.path)
        _, report = self._run()
        bloat = [line for line in report.splitlines() if 'bloat' in line][0]
        self.assertGreater(float(bloat.split('→ ')[1].split('%')[0]), 20.0,
                           'the fixture should leave a lot of freelist behind')
        ok, out = self._run(vacuum=True)
        self.assertTrue(ok)
        self.assertIn('vacuum freed', out)
        self.assertLess(os.path.getsize(self.path), before)

    def test_a_compact_file_is_not_vacuumed(self):
        _fill(self.path, rows=100, delete=0)
        ok, out = self._run(vacuum=True)
        self.assertTrue(ok)
        self.assertIn('SKIP  vacuum', out)

    def test_vacuum_always_overrides_the_threshold(self):
        _fill(self.path, rows=100, delete=0)
        ok, out = self._run(vacuum=True, vacuum_always=True)
        self.assertTrue(ok)
        self.assertIn('vacuum freed', out)

    def test_a_file_that_is_not_a_database_fails(self):
        with open(self.path, 'wb') as fh:
            fh.write(b'this is not a sqlite file at all' * 40)
        ok, out = self._run()
        self.assertFalse(ok)
        self.assertIn('FAIL', out)

    def test_the_configured_busy_timeout_is_reported(self):
        connection = sqlite3.connect(self.path)
        connection.execute('CREATE TABLE t (id INTEGER)')
        connection.commit()
        connection.close()
        ok, out = self._run()
        self.assertTrue(ok)
        self.assertIn('busy timeout 20000 ms', out)

    def test_a_short_busy_timeout_is_warned_about_but_not_a_failure(self):
        with mock.patch.dict(settings.DATABASES['default'],
                             {'OPTIONS': {'timeout': 5}}):
            ok, out = self._run()
        self.assertTrue(ok)
        self.assertIn('WARN  busy timeout is 5000 ms', out)

    def test_a_missing_busy_timeout_is_warned_about(self):
        with mock.patch.dict(settings.DATABASES['default'], {'OPTIONS': {}}):
            ok, out = self._run()
        self.assertTrue(ok)
        self.assertIn('WARN  no busy timeout', out)


class DbMaintenanceCommandTest(SimpleTestCase):
    def test_an_unknown_alias_is_skipped_without_failing(self):
        out = io.StringIO()
        call_command('db_maintenance', '--alias', 'nope', stdout=out)
        self.assertIn('SKIP', out.getvalue())
