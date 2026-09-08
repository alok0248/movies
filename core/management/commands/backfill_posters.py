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


def _get_api_key():
    """Get a TMDB API key from settings, credentials.json, or TMDBApiKey DB model."""
    # 1. Environment / credentials.json
    api_key = getattr(settings, 'TMDB_API_KEY', None) or ''
    if api_key:
        return api_key
    # 2. TMDBApiKey model (primary storage on this project)
    try:
        from core.models import TMDBApiKey
        key_obj = TMDBApiKey.objects.filter(is_active=True).order_by('last_used_at').first()
        if key_obj:
            key_obj.usage_count += 1
            from django.utils import timezone as _tz
            key_obj.last_used_at = _tz.now()
            key_obj.save(update_fields=['usage_count', 'last_used_at'])
            return key_obj.key
    except Exception:
        pass
    return ''


def _fetch_poster(tmdb_id, media_type):
    """Fetch poster_path from TMDB for a movie or TV show.

    Falls back to the other media type when the primary lookup fails —
    app syncs sometimes store a movie ID as tv or vice versa.
    """
    api_key = _get_api_key()
    if not api_key:
        return None, None

    types = [media_type, 'movie' if media_type != 'movie' else 'tv']
    for mtype in types:
        path = '/movie' if mtype == 'movie' else '/tv'
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
                if poster or title:
                    return poster or None, title or None
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
            | models.Q(title='') | models.Q(title__isnull=True)
        ).exclude(tmdb_id__lte=0)[:limit]

        total = qs.count()
        self.stdout.write(f'\nPlayHistory: {total} rows missing poster')
        updated = 0
        skipped = 0

        for h in qs:
            poster, title = _fetch_poster(h.tmdb_id, h.media_type)
            changed = []
            if title and not h.title:
                if not dry_run:
                    h.title = title
                changed.append('title')
            if poster and not h.poster_path:
                if not dry_run:
                    h.poster_path = poster
                changed.append('poster')
            if changed:
                if dry_run:
                    self.stdout.write(f'  WOULD UPDATE: id={h.id} tmdb={h.tmdb_id} -> {", ".join(changed)}')
                else:
                    h.save(update_fields=[f + ('_path' if f == 'poster' else '') for f in changed])
                updated += 1
            else:
                skipped += 1
                self.stdout.write(f'  SKIP: id={h.id} tmdb={h.tmdb_id} media={h.media_type} title="{h.title}" (nothing on TMDB)')

            if updated % 10 == 0 and updated > 0:
                self.stdout.write(f'  ... {updated}/{total} updated')
            time.sleep(0.15)  # rate limit

        self.stdout.write(self.style.SUCCESS(f'PlayHistory: {updated} updated, {skipped} skipped'))

    def _backfill_watchlist(self, dry_run, limit):
        from core.models import WatchList

        qs = WatchList.objects.filter(
            models.Q(poster_path='') | models.Q(poster_path__isnull=True)
            | models.Q(title='') | models.Q(title__isnull=True)
        ).exclude(tmdb_id__lte=0)[:limit]

        total = qs.count()
        self.stdout.write(f'\nWatchList: {total} rows missing poster')
        updated = 0
        skipped = 0

        for w in qs:
            poster, title = _fetch_poster(w.tmdb_id, w.media_type)
            changed = []
            if title and not w.title:
                if not dry_run:
                    w.title = title
                changed.append('title')
            if poster and not w.poster_path:
                if not dry_run:
                    w.poster_path = poster
                changed.append('poster')
            if changed:
                if dry_run:
                    self.stdout.write(f'  WOULD UPDATE: id={w.id} tmdb={w.tmdb_id} -> {", ".join(changed)}')
                else:
                    w.save(update_fields=[f + ('_path' if f == 'poster' else '') for f in changed])
                updated += 1
            else:
                skipped += 1
                self.stdout.write(f'  SKIP: id={w.id} tmdb={w.tmdb_id} media={w.media_type} title="{w.title}" (nothing on TMDB)')

            if updated % 10 == 0 and updated > 0:
                self.stdout.write(f'  ... {updated}/{total} updated')
            time.sleep(0.15)

        self.stdout.write(self.style.SUCCESS(f'WatchList: {updated} updated, {skipped} skipped'))
