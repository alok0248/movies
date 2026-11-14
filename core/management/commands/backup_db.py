"""Snapshot the SQLite databases, checksum them, keep a rotation, optionally ship them off-box.

Nightly cron on the server (installed with the other jobs):

    30 2 * * * cd /home/ubuntu/project/movies && ./venv/bin/python manage.py backup_db >> /home/ubuntu/backup.log 2>&1

Why a command and not ``cp``: copying a live SQLite file gives you a torn page
snapshot as soon as a write lands mid-copy. ``sqlite3.Connection.backup`` takes a
consistent snapshot including the WAL, while the site keeps serving.

What lands in ``backups/`` (``--dir`` to change it):

    <alias>-YYYYmmdd-HHMMSS.sqlite3.gz      the snapshot
    <alias>-YYYYmmdd-HHMMSS.sqlite3.gz.sha256
    media-YYYYmmdd-HHMMSS.tar.gz            only with --media
    status.json                             last run summary, read by check_health

Rotation keeps the newest N snapshots per tier: daily (7), weekly (4, Sundays)
and monthly (6, first of the month). Every run also writes ``status.json`` so the
health check can tell you when the newest copy is stale instead of you finding
out on restore day.

Off-box: set the environment variable ``BACKUP_UPLOAD_CMD`` to a shell command
that copies one file away, using ``{file}`` as the placeholder, e.g.

    BACKUP_UPLOAD_CMD='rclone copy {file} remote:cinestream-backups/'

or point ``BACKUP_UPLOAD_DIR`` at a mounted remote path. Without either, the
snapshots stay on the box and ``status.json`` records ``offbox: not configured``
— which the health check reports as unknown, not as a failure.
"""
import gzip
import hashlib
import json
import re
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand

STATUS_FILE = 'status.json'
STAMP_RE = re.compile(r'(\d{8}-\d{6})\.')


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _count_rows(connection):
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    rows = 0
    for table in tables:
        try:
            rows += connection.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]
        except sqlite3.Error:
            pass
    return len(tables), rows


class Command(BaseCommand):
    help = 'Snapshot the SQLite databases, verify them, rotate old copies and optionally upload them.'

    def add_arguments(self, parser):
        parser.add_argument('--dir', default=None,
                            help='Where to write snapshots (default: <BASE_DIR>/backups).')
        parser.add_argument('--daily', type=int, default=7, help='Daily copies to keep (default 7).')
        parser.add_argument('--weekly', type=int, default=4, help='Weekly copies to keep (default 4).')
        parser.add_argument('--monthly', type=int, default=6, help='Monthly copies to keep (default 6).')
        parser.add_argument('--media', action='store_true',
                            help='Also archive the media directory (small posters only).')
        parser.add_argument('--no-upload', action='store_true',
                            help='Skip the off-box copy even when BACKUP_UPLOAD_CMD is set.')
        parser.add_argument('--keep', type=int, default=0,
                            help='Leave only this many newest snapshots overall (0 = ignore, use tiers).')

    # ---------------------------------------------------------------- helpers
    def _snapshot(self, alias, source, target_dir, stamp):
        snapshot = os.path.join(target_dir, '%s-%s.sqlite3' % (alias, stamp))
        if os.path.exists(snapshot):
            os.remove(snapshot)
        src = sqlite3.connect(source, timeout=30)
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        check = sqlite3.connect(snapshot)
        try:
            integrity = check.execute('PRAGMA integrity_check').fetchone()[0]
            tables, rows = _count_rows(check)
        finally:
            check.close()
        if integrity != 'ok':
            os.remove(snapshot)
            raise RuntimeError('integrity_check on %s returned %r' % (alias, integrity))

        gz_path = snapshot + '.gz'
        with open(snapshot, 'rb') as src_fh, gzip.open(gz_path, 'wb', compresslevel=9) as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh)
        os.remove(snapshot)

        digest = _sha256(gz_path)
        with open(gz_path + '.sha256', 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('%s  %s\n' % (digest, os.path.basename(gz_path)))
        return {
            'alias': alias,
            'file': os.path.basename(gz_path),
            'bytes': os.path.getsize(gz_path),
            'sha256': digest,
            'tables': tables,
            'rows': rows,
        }

    def _media(self, target_dir, stamp):
        media_dir = str(getattr(settings, 'MEDIA_ROOT', '') or '')
        if not media_dir or not os.path.isdir(media_dir):
            return None
        name = 'media-%s.tar.gz' % stamp
        path = os.path.join(target_dir, name)
        with tarfile.open(path, 'w:gz') as tar:
            tar.add(media_dir, arcname='media')
        digest = _sha256(path)
        with open(path + '.sha256', 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('%s  %s\n' % (digest, name))
        return {'file': name, 'bytes': os.path.getsize(path), 'sha256': digest}

    def _upload(self, path):
        command = os.environ.get('BACKUP_UPLOAD_CMD')
        directory = os.environ.get('BACKUP_UPLOAD_DIR')
        if command:
            rendered = command.replace('{file}', path)
            done = subprocess.run(rendered, shell=True, capture_output=True, text=True)
            if done.returncode != 0:
                raise RuntimeError((done.stderr or done.stdout or '').strip()[:300])
            return 'uploaded via BACKUP_UPLOAD_CMD'
        if directory:
            os.makedirs(directory, exist_ok=True)
            shutil.copy2(path, os.path.join(directory, os.path.basename(path)))
            return 'copied to %s' % directory
        return None

    def _prune(self, target_dir, daily, weekly, monthly):
        """Delete snapshots outside the daily / weekly / monthly tiers."""
        groups = {}
        for name in os.listdir(target_dir):
            if not name.endswith('.sqlite3.gz') and not name.endswith('.tar.gz'):
                continue
            found = STAMP_RE.search(name)
            if not found:
                continue
            try:
                stamp = datetime.strptime(found.group(1), '%Y%m%d-%H%M%S')
            except ValueError:
                continue
            groups.setdefault(stamp, []).append(name)

        stamps = sorted(groups, reverse=True)
        keep = set()
        seen_days, seen_weeks, seen_months = set(), set(), set()
        for stamp in stamps:
            day = stamp.date()
            week = stamp.isocalendar()[:2]
            month = (stamp.year, stamp.month)
            if day not in seen_days and len(seen_days) < daily:
                seen_days.add(day)
                keep |= set(groups[stamp])
                continue
            if stamp.weekday() == 6 and week not in seen_weeks and len(seen_weeks) < weekly:
                seen_weeks.add(week)
                keep |= set(groups[stamp])
                continue
            if stamp.day == 1 and month not in seen_months and len(seen_months) < monthly:
                seen_months.add(month)
                keep |= set(groups[stamp])
        # Always keep anything newer than the oldest kept day, so a run of daily
        # copies is never half-deleted.
        removed = []
        for name in sorted(os.listdir(target_dir)):
            if name == STATUS_FILE or name.endswith('.sha256'):
                continue
            if not (name.endswith('.sqlite3.gz') or name.endswith('.tar.gz')):
                continue
            if name not in keep:
                os.remove(os.path.join(target_dir, name))
                removed.append(name)
                sidecar = os.path.join(target_dir, name + '.sha256')
                if os.path.exists(sidecar):
                    os.remove(sidecar)
        return removed

    # ------------------------------------------------------------------ entry
    def handle(self, *args, **options):
        target_dir = options['dir'] or os.path.join(str(settings.BASE_DIR), 'backups')
        os.makedirs(target_dir, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        started = time.time()

        records, failures = [], []
        for alias in ('default', 'external'):
            config = settings.DATABASES.get(alias)
            if not config or 'sqlite' not in (config.get('ENGINE') or ''):
                self.stdout.write('  SKIP  %s — not a SQLite database' % alias)
                continue
            source = config.get('NAME')
            if not source or not os.path.exists(source):
                self.stdout.write('  SKIP  %s — file not found' % alias)
                continue
            try:
                record = self._snapshot(alias, source, target_dir, stamp)
            except Exception as exc:
                failures.append('%s: %s' % (alias, exc))
                self.stderr.write(self.style.ERROR('  FAIL  %s — %s' % (alias, exc)))
                continue
            records.append(record)
            self.stdout.write('  OK    %s — %s (%.1f MB, %d tables, %d rows)'
                              % (alias, record['file'], record['bytes'] / 1048576.0,
                                 record['tables'], record['rows']))

        if options['media']:
            media = self._media(target_dir, stamp)
            if media:
                records.append(media)
                self.stdout.write('  OK    media — %s (%.1f MB)'
                                  % (media['file'], media['bytes'] / 1048576.0))

        if not records:
            self.stderr.write(self.style.ERROR('nothing was written to %s' % target_dir))
            raise SystemExit(1)

        offbox = {'configured': bool(os.environ.get('BACKUP_UPLOAD_CMD')
                                     or os.environ.get('BACKUP_UPLOAD_DIR')),
                  'ok': None, 'detail': 'not configured'}
        if offbox['configured'] and not options['no_upload']:
            detail, uploaded = None, 0
            for record in records:
                try:
                    detail = self._upload(os.path.join(target_dir, record['file']))
                    uploaded += 1
                except Exception as exc:
                    detail = str(exc)
                    break
            offbox['ok'] = uploaded == len(records)
            offbox['detail'] = detail or 'no uploader responded'

        removed = []
        if options['keep']:
            for name in sorted(os.listdir(target_dir), reverse=True)[options['keep']:]:
                if name == STATUS_FILE or name.endswith('.sha256'):
                    continue
                os.remove(os.path.join(target_dir, name))
                removed.append(name)
        else:
            removed = self._prune(target_dir, options['daily'], options['weekly'], options['monthly'])

        status = {
            'at': datetime.now().isoformat(timespec='seconds'),
            'seconds': round(time.time() - started, 1),
            'dir': target_dir,
            'files': records,
            'offbox': offbox,
            'pruned': removed,
            'failures': failures,
        }
        with open(os.path.join(target_dir, STATUS_FILE), 'w', encoding='utf-8') as fh:
            json.dump(status, fh, indent=2)

        if removed:
            self.stdout.write('  pruned %d old copy(ies)' % len(removed))
        self.stdout.write('  off-box: %s' % ('ok' if offbox['ok'] else offbox['detail']))

        if failures:
            self.stderr.write(self.style.ERROR('backup finished with %d failure(s)' % len(failures)))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS('backup written to %s (%d file(s))' % (target_dir, len(records))))
