import json
import re
from .models import ProviderItem
from .tmdb_client import get_tmdb_db_connection


def _provider_slug(value):
    value = (value or '').strip().lower()
    value = re.sub(r'[^a-z0-9]+', '-', value)
    return value.strip('-')


def _extract_provider_names(raw, provider_names):
    if not raw:
        return
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return
    results = raw.get('results', {}) if isinstance(raw, dict) else {}
    for region_data in results.values():
        if not isinstance(region_data, dict):
            continue
        for section in ['flatrate', 'rent', 'buy', 'ads', 'free']:
            for provider in region_data.get(section, []) or []:
                name = provider.get('provider_name')
                if name:
                    provider_names.add(name)


def sync_provider_items_once():
    provider_names = set()
    tables = ['movies', 'tv_shows']
    batch_size = 200

    for table in tables:
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COALESCE(MIN(id), 0) AS min_id, COALESCE(MAX(id), 0) AS max_id FROM {table}")
                    bounds = cur.fetchone()
                    min_id = bounds.get('min_id', 0) if isinstance(bounds, dict) else bounds[0]
                    max_id = bounds.get('max_id', 0) if isinstance(bounds, dict) else bounds[1]
            finally:
                conn.close()
        except Exception:
            continue

        current = min_id
        while current <= max_id:
            upper = current + batch_size - 1
            try:
                conn = get_tmdb_db_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT id, watch_providers FROM {table} WHERE id BETWEEN %s AND %s AND watch_providers IS NOT NULL ORDER BY id",
                            (current, upper)
                        )
                        rows = cur.fetchall()
                finally:
                    conn.close()

                for row in rows:
                    raw = row.get('watch_providers') if isinstance(row, dict) else row[1]
                    _extract_provider_names(raw, provider_names)
            except Exception:
                pass
            current = upper + 1

    for name in sorted(provider_names):
        ProviderItem.objects.get_or_create(name=name, defaults={'slug': _provider_slug(name), 'is_enabled': True})
