"""Probe a sample of titles on every active player and hide the dead ones.

    ./venv/bin/python manage.py check_catalogue_links                # 8 titles, 4 providers
    ./venv/bin/python manage.py check_catalogue_links --limit 20 --threshold 3
    ./venv/bin/python manage.py check_catalogue_links --no-hide      # record only

A streaming catalogue rots from the edges: the title is there, the player answers
200 for a while, then one day it serves "not found" and the visitor sees a broken
player. This walks a sample of titles that people actually watched, resolves each
one through every active player (the same ``get_player_url`` the site uses), asks
for the page and records what came back.

One probe run is one vote: if no provider answered, the title's failure counter
goes up; any success resets it and lifts an automatic hide, because a provider
blip must not bury a title for good. At ``--threshold`` consecutive failing runs
the title is marked hidden, which the sources endpoint honours — the app stops
being handed a stream that does not work.

Cron (shared with the other maintenance jobs, weekly is plenty):

    20 4 * * 0  check_catalogue_links --limit 20
"""
import time
import urllib.error
import urllib.request

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import (CatalogueLinkCheck, CatalogueTitleHealth, PlayHistory,
                         PlayerConfiguration)

USER_AGENT = 'Mozilla/5.0 (compatible; CineStreamLinkCheck/1.0)'
DEFAULT_ORIGIN = 'https://newmovies.linkpc.net'


class Command(BaseCommand):
    help = 'Probe sampled titles on the active players and hide titles that keep failing.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=8, help='Titles to probe (default 8).')
        parser.add_argument('--providers', type=int, default=4, help='Providers per title (default 4).')
        parser.add_argument('--threshold', type=int, default=3,
                            help='Consecutive failing runs before a title is hidden (default 3).')
        parser.add_argument('--timeout', type=float, default=10.0, help='Per-request timeout in seconds.')
        parser.add_argument('--media', choices=['movie', 'tv', 'both'], default='both',
                            help='Which media types to sample (default both).')
        parser.add_argument('--no-hide', action='store_true', help='Record results but never hide a title.')
        parser.add_argument('--tmdb', default='', help='Comma-separated tmdb ids to probe instead of sampling.')

    # ------------------------------------------------------------------ probes
    def _origin(self):
        """Base URL for players configured as a path on this site."""
        host = ''
        try:
            from django.conf import settings as dj_settings
            hosts = [h for h in (getattr(dj_settings, 'ALLOWED_HOSTS', []) or [])
                     if h and h != '*' and not h.startswith('.')]
            host = hosts[0] if hosts else ''
            if host in ('localhost', '127.0.0.1'):
                host = ''
        except Exception:
            host = ''
        return ('https://' + host) if host else DEFAULT_ORIGIN

    def _probe(self, url, timeout):
        started = time.monotonic()
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                                       'Accept': 'text/html,*/*'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(4096)
                latency = int((time.monotonic() - started) * 1000)
                ok = 200 <= response.status < 400 and len(body) > 0
                return ok, response.status, latency, '' if ok else 'empty response'
        except urllib.error.HTTPError as exc:
            latency = int((time.monotonic() - started) * 1000)
            return False, exc.code, latency, 'HTTP %s' % exc.code
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            return False, None, latency, str(exc)[:200]

    def _titles(self, options):
        if options['tmdb']:
            ids = [int(x) for x in options['tmdb'].replace(' ', '').split(',') if x.strip().isdigit()]
            rows = []
            for tmdb_id in ids[:options['limit']]:
                row = PlayHistory.objects.filter(tmdb_id=tmdb_id).first()
                rows.append({
                    'tmdb_id': tmdb_id,
                    'media_type': row.media_type if row else 'movie',
                    'title': row.title if row else '',
                })
            return rows

        history = PlayHistory.objects.exclude(tmdb_id=0)
        if options['media'] != 'both':
            history = history.filter(media_type=options['media'])
        rows, seen = [], set()
        for row in history.order_by('-id')[:400]:
            key = (row.media_type, row.tmdb_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append({'tmdb_id': row.tmdb_id, 'media_type': row.media_type, 'title': row.title})
            if len(rows) >= options['limit']:
                break
        return rows

    # ------------------------------------------------------------------- entry
    def handle(self, *args, **options):
        titles = self._titles(options)
        providers = list(PlayerConfiguration.objects.filter(is_active=True).order_by('order', 'id'))
        if not titles:
            self.stdout.write('No titles to probe yet — play history is empty.')
            return
        if not providers:
            self.stdout.write('No active players configured — nothing to probe against.')
            return

        threshold = max(1, options['threshold'])
        hide = not options['no_hide']
        now = timezone.now()
        probed = hidden = lifted = 0

        for entry in titles:
            media_type = entry['media_type'] or 'movie'
            candidates = [p for p in providers if p.media_type in ('both', media_type)][:options['providers']]
            if not candidates:
                continue

            health, _ = CatalogueTitleHealth.objects.get_or_create(
                media_type=media_type, tmdb_id=entry['tmdb_id'],
                defaults={'title': entry['title'] or ''})

            results = []
            for provider in candidates:
                try:
                    url = provider.get_player_url(media_type, entry['tmdb_id'])
                except Exception as exc:
                    results.append((provider, '', False, None, 0, 'could not build url: %s' % exc))
                    continue
                if not url:
                    results.append((provider, '', False, None, 0, 'player has no url configured'))
                    continue
                if url.startswith('/'):
                    # A player can be configured with a path on this site
                    # (e.g. CinePlayer); probe it on the real origin.
                    url = self._origin() + url
                ok, status, latency, error = self._probe(url, options['timeout'])
                results.append((provider, url, ok, status, latency, error))

            any_ok = any(r[2] for r in results)
            CatalogueLinkCheck.objects.bulk_create([
                CatalogueLinkCheck(health=health, provider=provider, provider_name=provider.name,
                                   url=url[:2000], ok=ok, status_code=status, latency_ms=latency,
                                   error=error[:300])
                for provider, url, ok, status, latency, error in results
            ])

            health.checks += 1
            health.providers_checked = len(results)
            health.last_checked_at = now
            if any_ok:
                health.successes += 1
                health.consecutive_failures = 0
                health.last_ok_at = now
                if health.hidden and not health.hidden_reason.startswith('manual'):
                    health.hidden = False
                    health.hidden_at = None
                    health.hidden_reason = ''
                    lifted += 1
            else:
                health.consecutive_failures += 1
                if hide and not health.hidden and health.consecutive_failures >= threshold:
                    health.hidden = True
                    health.hidden_at = now
                    health.hidden_reason = 'auto: no provider answered %d times in a row' % health.consecutive_failures
                    hidden += 1
            if entry['title']:
                health.title = entry['title'][:255]
            health.save()
            probed += 1

            marks = ''.join('+' if r[2] else '-' for r in results)
            self.stdout.write('  %-6s %-8s %s  %s' % (
                media_type, entry['tmdb_id'], marks,
                'HIDDEN' if health.hidden else ('ok' if any_ok else 'fails=%d' % health.consecutive_failures)))

        self.stdout.write(self.style.SUCCESS(
            'probed %d title(s): %d newly hidden, %d unhidden, threshold %d'
            % (probed, hidden, lifted, threshold)))
