"""Weekly database maintenance: prove the SQLite file is sound, then compact it.

The site runs one SQLite file in WAL mode with a 20-second busy timeout
(settings.DATABASES['default']['OPTIONS']['timeout'], plus the per-connection
WAL/synchronous pragmas in core/signals.py). That setup is comfortable at this
size, but it degrades quietly: pages freed by deletes stay in the file as
freelist, and a file that keeps growing with a growing freelist is where the
"random 500" and the slow admin come from.

This command is the weekly look:

* ``PRAGMA quick_check`` (or ``integrity_check`` with ``--full``) proves the
  file and its indexes are readable and consistent.
* page size, page count, freelist and the resulting bloat percentage say how
  much of the file is dead space.
* ``--vacuum`` rewrites the file when the bloat crosses ``--vacuum-if-bloat``
  (default 20%), and reports the bytes reclaimed.

Cron on the server (before the other weekly jobs, so the file is compact when
they run):

    5 4 * * 0 cd /home/ubuntu/project/movies && ./venv/bin/python manage.py db_maintenance --vacuum >> /home/ubuntu/maintenance.log 2>&1

Exit code is 1 when a check fails, so it can gate a deploy or a monitoring
wrapper. The thresholds for moving off SQLite are written down in
``docs/database-growth.md``.
"""

import os
import sqlite3

from django.conf import settings
from django.core.management.base import BaseCommand

# SQLite setting that is *not* a database setting: how long a blocked writer
# waits for the lock before raising "database is locked".
EXPECTED_BUSY_TIMEOUT_MS = 20000


def _plural(count, word):
    return '%d %s%s' % (count, word, '' if count == 1 else 's')


class Command(BaseCommand):
    help = ('Integrity-check every SQLite database, report bloat and (with '
            '--vacuum) compact it. Exit code 1 when a check fails.')

    def add_arguments(self, parser):
        parser.add_argument('--alias', default='default',
                            help='Database alias to check: default, external or all.')
        parser.add_argument('--full', action='store_true',
                            help='Run integrity_check instead of the cheaper quick_check.')
        parser.add_argument('--vacuum', action='store_true',
                            help='VACUUM the file when the checks pass.')
        parser.add_argument('--vacuum-if-bloat', type=float, default=20.0,
                            help='Only VACUUM when the freelist is at least this '
                                 'percentage of the file (default 20).')
        parser.add_argument('--vacuum-always', action='store_true',
                            help='VACUUM even when the bloat is below the threshold.')

    def handle(self, *args, **options):
        aliases = self._aliases(options['alias'])
        failures = 0
        checked = 0
        for alias in aliases:
            config = settings.DATABASES.get(alias)
            if not config:
                self.stdout.write('  SKIP  %s — no such database alias' % alias)
                continue
            if 'sqlite' not in (config.get('ENGINE') or ''):
                self.stdout.write('  SKIP  %s — not a SQLite database (%s)'
                                  % (alias, config.get('ENGINE')))
                continue
            path = config.get('NAME')
            if not path or not os.path.exists(path):
                self.stdout.write('  SKIP  %s — file not found' % alias)
                continue
            checked += 1
            if not self._check(alias, path, options):
                failures += 1

        if not checked:
            self.stdout.write('nothing to check')
        if failures:
            raise SystemExit(1)

    def _aliases(self, wanted):
        wanted = (wanted or 'default').strip().lower()
        if wanted == 'all':
            return list(settings.DATABASES.keys())
        return [wanted]

    # ------------------------------------------------------------------ one db
    def _check(self, alias, path, options):
        """Check one file. Returns False when something is wrong."""
        self.stdout.write('%s (%s)' % (alias, path))
        connection = sqlite3.connect(path, timeout=30)
        ok = True
        try:
            pragma = 'integrity_check' if options['full'] else 'quick_check'
            try:
                result = connection.execute('PRAGMA %s' % pragma).fetchone()
            except sqlite3.DatabaseError as exc:
                # A file that is not a database at all (or a truncated one)
                # answers this with an error rather than a verdict.
                self.stdout.write('  FAIL  %s — %s' % (pragma, str(exc)[:500]))
                return False
            verdict = (result[0] if result else 'no result') or 'no result'
            if verdict.lower() != 'ok':
                ok = False
                self.stdout.write('  FAIL  %s — %s' % (pragma, verdict[:500]))
            else:
                self.stdout.write('  OK    %s' % pragma)

            page_size = self._scalar(connection, 'PRAGMA page_size')
            page_count = self._scalar(connection, 'PRAGMA page_count') or 0
            freelist = self._scalar(connection, 'PRAGMA freelist_count') or 0
            journal = connection.execute('PRAGMA journal_mode').fetchone()
            journal = (journal[0] if journal else '?') or '?'

            size = os.path.getsize(path)
            bloat = (100.0 * freelist / page_count) if page_count else 0.0
            self.stdout.write('        %s, %s pages of %s B, %.1f MB'
                              % (journal, page_count, page_size or 0,
                                 size / 1048576.0))
            self.stdout.write('        freelist %s → %.1f%% bloat'
                              % (_plural(freelist, 'page'), bloat))
            self._report_busy_timeout(alias, connection)

            if options['vacuum'] and ok:
                self._vacuum(connection, path, size, bloat, options)
            elif options['vacuum']:
                self.stdout.write('  SKIP  vacuum — the check did not pass')
        finally:
            connection.close()
        return ok

    def _report_busy_timeout(self, alias, connection):
        """Say how long a writer waits for the lock.

        The timeout that matters is the one Django hands the driver
        (``DATABASES['default']['OPTIONS']['timeout']``); the pragma on this
        command's own connection only tells us what this run uses.
        """
        options = (settings.DATABASES.get(alias) or {}).get('OPTIONS') or {}
        configured = options.get('timeout')
        try:
            configured_ms = int(float(configured) * 1000)
        except (TypeError, ValueError):
            configured_ms = None
        actual = self._scalar(connection, 'PRAGMA busy_timeout') or 0
        if configured_ms is None:
            self.stdout.write('  WARN  no busy timeout in DATABASES["%s"]["OPTIONS"] — '
                              'a blocked writer would fail immediately' % alias)
        elif configured_ms < EXPECTED_BUSY_TIMEOUT_MS:
            self.stdout.write('  WARN  busy timeout is %d ms (expected >= %d ms) '
                              '— busy writes may fail with "database is locked"'
                              % (configured_ms, EXPECTED_BUSY_TIMEOUT_MS))
        else:
            self.stdout.write('        busy timeout %d ms (this connection: %d ms)'
                              % (configured_ms, actual))

    def _vacuum(self, connection, path, size, bloat, options):
        """Compact the file, unless the bloat threshold says it is not worth it."""
        if bloat < options['vacuum_if_bloat'] and not options['vacuum_always']:
            self.stdout.write('  SKIP  vacuum — %.1f%% bloat is below %.1f%% '
                              '(use --vacuum-always to force it)'
                              % (bloat, options['vacuum_if_bloat']))
            return
        before_size = os.path.getsize(path)
        before_pages = self._scalar(connection, 'PRAGMA page_count') or 0
        page_size = self._scalar(connection, 'PRAGMA page_size') or 0
        self.stdout.write('        vacuuming…')
        connection.isolation_level = None
        connection.execute('VACUUM')
        # Under WAL the shrink is only visible to the filesystem after the WAL
        # is checkpointed, so the report would otherwise always say 0 MB freed.
        try:
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.DatabaseError:
            pass
        after_pages = self._scalar(connection, 'PRAGMA page_count') or 0
        after_size = os.path.getsize(path)
        freed_pages = max(0, before_pages - after_pages)
        self.stdout.write('  OK    vacuum freed %s (%.1f MB → %.1f MB, %d → %d pages)'
                          % ('%.1f MB' % (freed_pages * page_size / 1048576.0)
                             if page_size else _plural(freed_pages, 'page'),
                             before_size / 1048576.0, after_size / 1048576.0,
                             before_pages, after_pages))

    @staticmethod
    def _scalar(connection, pragma):
        row = connection.execute(pragma).fetchone()
        return row[0] if row else None
