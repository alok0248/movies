import re
from .models import ProviderItem
from .tmdb_client import tmdb_db_cursor


def _provider_slug(value):
    value = (value or '').strip().lower()
    value = re.sub(r'[^a-z0-9]+', '-', value)
    return value.strip('-')


def sync_provider_items_once():
    provider_names = set()
    provider_tables = ['movie_watch_providers', 'tv_watch_providers']

    for table in provider_tables:
        try:
            with tmdb_db_cursor() as cur:
                cur.execute(f"SELECT DISTINCT provider_name FROM {table} WHERE provider_name IS NOT NULL AND provider_name != ''")
                rows = cur.fetchall()
            for row in rows:
                provider_name = row.get('provider_name') if isinstance(row, dict) else row[0]
                if provider_name:
                    provider_names.add(provider_name)
        except Exception:
            continue

    for name in sorted(provider_names):
        ProviderItem.objects.get_or_create(name=name, defaults={'slug': _provider_slug(name), 'is_enabled': True})
