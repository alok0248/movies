import re
import time
import requests
from django.core.management.base import BaseCommand
from django.conf import settings
from core.models import ProviderItem, WatchRegion, TMDBApiKey


def get_api_key():
    keys = list(TMDBApiKey.objects.values_list('key', flat=True))
    return keys[0] if keys else 'ce9d3dcbdb958906922ca6fbead78b95'


def api_get(endpoint, retries=3, delay=3):
    api_key = get_api_key()
    url = f'https://api.themoviedb.org/3{endpoint}'
    for attempt in range(retries):
        try:
            r = requests.get(url, params={'api_key': api_key}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                print(f'  Retry {attempt+1}/{retries} after error: {e}')
                time.sleep(delay * (attempt + 1))
            else:
                raise


class Command(BaseCommand):
    help = 'Populate watch providers and regions from TMDB API'

    def handle(self, *args, **options):
        self.populate_regions()
        self.populate_providers()

    def populate_regions(self):
        self.stdout.write('Fetching watch regions...')
        try:
            data = api_get('/watch/providers/regions')
            regions = data.get('results', [])
            self.stdout.write(f'  Got {len(regions)} regions')
            count = 0
            for r in regions:
                code = r.get('iso_3166_1', '').strip().upper()
                name = r.get('english_name', r.get('name', ''))
                if code and name:
                    _, created = WatchRegion.objects.get_or_create(
                        code=code,
                        defaults={'name': name, 'is_enabled': True, 'display_order': count}
                    )
                    if created:
                        count += 1
            self.stdout.write(self.style.SUCCESS(f'  Created {count} new regions (total: {WatchRegion.objects.count()})'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  Error fetching regions: {e}'))

    def populate_providers(self):
        self.stdout.write('Fetching providers...')
        try:
            movie_data = api_get('/watch/providers/movie')
            time.sleep(1)
            tv_data = api_get('/watch/providers/tv')

            movie_providers = movie_data.get('results', [])
            tv_providers = tv_data.get('results', [])
            self.stdout.write(f'  Movie: {len(movie_providers)}, TV: {len(tv_providers)}')

            all_p = {}
            for p in movie_providers:
                pid = p.get('provider_id')
                if pid:
                    all_p[pid] = {**p, 'sm': True, 'st': False}
            for p in tv_providers:
                pid = p.get('provider_id')
                if pid:
                    if pid in all_p:
                        all_p[pid]['st'] = True
                    else:
                        all_p[pid] = {**p, 'sm': False, 'st': True}

            batch = []
            names = set()
            slugs = set()
            for pid, p in sorted(all_p.items()):
                name = p.get('provider_name', 'Unknown')
                bn = name
                while name in names:
                    name = f'{bn}-{pid}'
                names.add(name)
                slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
                bs = slug
                while slug in slugs:
                    slug = f'{bs}-{pid}'
                slugs.add(slug)
                batch.append(ProviderItem(
                    name=name, slug=slug, tmdb_provider_id=pid,
                    logo_path=p.get('logo_path', ''),
                    supports_movies=p.get('sm', False),
                    supports_tv=p.get('st', False),
                    is_enabled=True
                ))

            ProviderItem.objects.bulk_create(batch, batch_size=500)
            self.stdout.write(self.style.SUCCESS(
                f'  Created {len(batch)} providers (total: {ProviderItem.objects.count()})'
            ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  Error fetching providers: {e}'))
