"""Restore one snapshot from ``backups/`` after proving it is intact.

    ./venv/bin/python manage.py restore_backup backups/default-20261114-023000.sqlite3.gz --to /tmp/restore/

Writes the database to ``--to`` (default ``/tmp/restore``), never over the live
file unless ``--in-place`` is given, because restoring onto a live database under
gunicorn is how a small mistake becomes an outage. The checksum sidecar written by
``backup_db`` is verified first, then ``PRAGMA integrity_check`` and the row counts
are printed so the restored copy is proven before it is used.
"""
import gzip
import hashlib
import os
import shutil
import sqlite3

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class Command(BaseCommand):
    help = 'Restore a backup_db snapshot, verifying its checksum and integrity first.'

    def add_arguments(self, parser):
        parser.add_argument('snapshot', help='Path to a .sqlite3.gz snapshot from backup_db.')
        parser.add_argument('--to', default='/tmp/restore',
                            help='Directory to write the restored database into (default /tmp/restore).')
        parser.add_argument('--name', default=None,
                            help='Output filename (default: snapshot name without .gz).')
        parser.add_argument('--in-place', action='store_true',
                            help='Overwrite the live default database. Stop gunicorn first.')
        parser.add_argument('--force', action='store_true',
                            help='Overwrite an existing file at the destination.')

    def handle(self, *args, **options):
        snapshot = options['snapshot']
        if not os.path.exists(snapshot):
            raise CommandError('no snapshot at %s' % snapshot)

        sidecar = snapshot + '.sha256'
        if os.path.exists(sidecar):
            expected = open(sidecar, encoding='utf-8').read().split()[0]
            actual = _sha256(snapshot)
            if expected != actual:
                raise CommandError('checksum mismatch: sidecar %s, file %s' % (expected[:16], actual[:16]))
            self.stdout.write('  checksum matches %s…' % expected[:16])
        else:
            self.stderr.write(self.style.WARNING('  no .sha256 beside the snapshot — skipping checksum check'))

        target_dir = options['to']
        name = options['name'] or os.path.basename(snapshot)[:-3]
        if options['in_place']:
            target_dir = os.path.dirname(str(settings.DATABASES['default']['NAME']))
            name = os.path.basename(str(settings.DATABASES['default']['NAME']))
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, name)

        if os.path.exists(target) and not options['force']:
            raise CommandError('%s exists — pass --force to overwrite' % target)

        with gzip.open(snapshot, 'rb') as src, open(target, 'wb') as dst:
            shutil.copyfileobj(src, dst)

        connection = sqlite3.connect(target)
        try:
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            rows = 0
            for table in tables:
                try:
                    rows += connection.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]
                except sqlite3.Error:
                    pass
        finally:
            connection.close()

        size = os.path.getsize(target) / 1048576.0
        self.stdout.write('  restored %s — %.1f MB, %d tables, %d rows, integrity %r'
                          % (target, size, len(tables), rows, integrity))
        if integrity != 'ok':
            raise CommandError('restored copy failed integrity_check: %r' % integrity)

        if options['in_place']:
            self.stdout.write(self.style.WARNING(
                '  live database replaced — restart gunicorn (sudo systemctl restart movies) and re-run check_health'))
        else:
            self.stdout.write(self.style.SUCCESS(
                '  safe copy ready at %s (compare row counts, then swap manually)' % target))
