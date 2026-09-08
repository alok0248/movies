"""Backfill missing poster_path from TMDB for PlayHistory and WatchList rows.

Usage:
    python manage.py backfill_posters           # backfill all missing
    python manage.py backfill_posters --model PlayHistory
    python manage.py backfill_posters --model WatchList
    python manage.py backfill_posters --dry-run  # show what would be updated
"""
from django.core.management.base import BaseCommand
from django.conf import settings
from django.db import models
import requests
import time


TMDB_BASE = 'https://api.themoviedb.org/3'
IMG_BASE = settings.TMDB_IMAGE_BASE_URL  # e.g. https://image.tmdb.org/t/p/w500


def _fetch_poster(tmdb_id, media_type):
    """Fetch poster_path from TMDB for a movie or TV show."""
    try:
        api_key = settings.TMDB_API_KEY
    except AttributeError:
        # Try reading from SiteSettings
        try:
            from core.models import SiteSettings
            s = SiteSettings.get_settings()
            api_key = s.tmdb_api_key or ''
        except Exception:
            api_key = ''

    if not api_key:
        return None, None

    path = '/movie' if media_type == 'movie' else '/tv'
    try:
        resp = requests.get(
            f'{TMDB_BASE}{path}/{tmdb_id}',
            params={'api_key': api_key, 'language': 'en-US'},
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            poster = data.get('poster_path') or ''
            title = data.get('title') or data.get('name') or ''
            return poster, title
    except Exception:
        pass
    return None, None


class Command(BaseCommand):
    help = 'Backfill missing poster_path from TMDB for PlayHistory and WatchList'

    def add_arguments(self, parser):
        parser.add_argument('--model', type=str, default='both',
                            choices=['PlayHistory', 'WatchList', 'both'],
                            help='Which model to backfill')
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be updated without writing')
        parser.add_argument('--limit', type=int, default=200,
                            help='Max rows to process per model')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        model = options['model']
        limit = options['limit']

        if model in ('PlayHistory', 'both'):
            self._backfill_play_history(dry_run, limit)
        if model in ('WatchList', 'both'):
            self._backfill_watchlist(dry_run, limit)

    def _backfill_play_history(self, dry_run, limit):
        from core.models import PlayHistory

        qs = PlayHistory.objects.filter(
            models.Q(poster_path='') | models.Q(poster_path__isnull=True)
        ).exclude(tmdb_id__lte=0)[:limit]

        total = qs.count()
        self.stdout.write(f'\nPlayHistory: {total} rows missing poster')
        updated = 0
        skipped = 0

        for h in qs:
            poster, title = _fetch_poster(h.tmdb_id, h.media_type)
            if poster:
                if dry_run:
                    self.stdout.write(f'  WOULD UPDATE: id={h.id} tmdb={h.tmdb_id} title="{h.title}" poster={poster}')
                else:
                    h.poster_path = poster
                    if title and not h.title:
                        h.title = title
                    h.save(update_fields=['poster_path', 'title'] if title and not h.title else ['poster_path'])
                updated += 1
            else:
                skipped += 1
                self.stdout.write(f'  SKIP: id={h.id} tmdb={h.tmdb_id} media={h.media_type} title="{h.title}" (no poster on TMDB)')

            if updated % 10 == 0 and updated > 0:
                self.stdout.write(f'  ... {updated}/{total} updated')
            time.sleep(0.15)  # rate limit

        self.stdout.write(self.style.SUCCESS(f'PlayHistory: {updated} updated, {skipped} skipped'))

    def _backfill_watchlist(self, dry_run, limit):
        from core.models import WatchList

        qs = WatchList.objects.filter(
            models.Q(poster_path='') | models.Q(poster_path__isnull=True)
        ).exclude(tmdb_id__lte=0)[:limit]

        total = qs.count()
        self.stdout.write(f'\nWatchList: {total} rows missing poster')
        updated = 0
        skipped = 0

        for w in qs:
            poster, title = _fetch_poster(w.tmdb_id, w.media_type)
            if poster:
                if dry_run:
                    self.stdout.write(f'  WOULD UPDATE: id={w.id} tmdb={w.tmdb_id} title="{w.title}" poster={poster}')
                else:
                    w.poster_path = poster
                    if title and not w.title:
                        w.title = title
                    w.save(update_fields=['poster_path', 'title'] if title and not w.title else ['poster_path'])
                updated += 1
            else:
                skipped += 1
                self.stdout.write(f'  SKIP: id={w.id} tmdb={w.tmdb_id} media={w.media_type} title="{w.title}" (no poster on TMDB)')

            if updated % 10 == 0 and updated > 0:
                self.stdout.write(f'  ... {updated}/{total} updated')
            time.sleep(0.15)

        self.stdout.write(self.style.SUCCESS(f'WatchList: {updated} updated, {skipped} skipped'))
