import re
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from .models import ProviderItem, ProviderRegionAvailability, WatchRegion
from .tmdb_client import TMDBClient, tmdb_db_cursor


def _provider_slug(value):
    value = (value or '').strip().lower()
    value = re.sub(r'[^a-z0-9]+', '-', value)
    return value.strip('-')


def _unique_slug(name, provider_id=None):
    base = _provider_slug(name) or f"provider-{provider_id or 'custom'}"
    slug = base
    suffix = 2
    while ProviderItem.objects.filter(slug=slug).exclude(tmdb_provider_id=provider_id).exists():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def _upsert_provider(item, media_type):
    provider_id = item.get('provider_id')
    if not provider_id:
        return None, False
    provider_name = (item.get('provider_name') or f'Provider {provider_id}').strip()
    provider = ProviderItem.objects.filter(tmdb_provider_id=provider_id).first()
    created = False
    if provider is None:
        # Legacy records were created by name before TMDB IDs were stored.
        # Reuse a matching name instead of violating the unique name constraint.
        provider = ProviderItem.objects.filter(name__iexact=provider_name).first()
    if provider is None:
        provider = ProviderItem.objects.create(
            name=provider_name,
            slug=_unique_slug(provider_name, provider_id),
            tmdb_provider_id=provider_id,
            is_enabled=True,
        )
        created = True
    elif provider.tmdb_provider_id != provider_id:
        provider.tmdb_provider_id = provider_id
    provider.name = provider_name or provider.name
    provider.logo_path = item.get('logo_path') or provider.logo_path
    provider.display_priority = item.get('display_priority') or 0
    provider.supports_movies = provider.supports_movies or media_type == 'movie'
    provider.supports_tv = provider.supports_tv or media_type == 'tv'
    provider.last_synced_at = timezone.now()
    provider.save()
    return provider, created


def _sync_from_api():
    client = TMDBClient()
    region_rows = client.get_watch_provider_regions().get('results', [])
    regions = {}
    created_regions = 0
    for row in region_rows:
        code = (row.get('iso_3166_1') or '').upper()
        if len(code) != 2:
            continue
        region, created = WatchRegion.objects.get_or_create(
            code=code,
            defaults={'name': row.get('english_name') or row.get('native_name') or code, 'is_enabled': True},
        )
        name = row.get('english_name') or row.get('native_name')
        if name and region.name != name:
            region.name = name
            region.save(update_fields=['name', 'updated_at'])
        regions[code] = region
        created_regions += int(created)

    created_providers = 0
    availability_count = 0
    # The global movie/TV provider endpoints contain every provider TMDB makes
    # available for filtering. Do not issue one API request per country here:
    # that turns a manual sync into hundreds of requests and can hit rate limits.
    for media_type in ('movie', 'tv'):
        global_rows = client.get_available_watch_providers(media_type).get('results', [])
        for row in global_rows:
            _, created = _upsert_provider(row, media_type)
            created_providers += int(created)
    return {'regions_created': created_regions, 'providers_created': created_providers, 'availability': availability_count, 'source': 'tmdb_api'}


def _sync_from_extracted_db():
    created_regions = 0
    created_providers = 0
    availability_count = 0
    for media_type, table in (('movie', 'movie_watch_providers'), ('tv', 'tv_watch_providers')):
        with tmdb_db_cursor() as cur:
            cur.execute(f"SELECT DISTINCT country_code, provider_id, provider_name, logo_path, display_priority FROM {table} WHERE provider_id IS NOT NULL AND provider_name IS NOT NULL")
            rows = cur.fetchall()
        for row in rows:
            code = (row.get('country_code') or '').upper()
            if len(code) != 2:
                continue
            region, region_created = WatchRegion.objects.get_or_create(code=code, defaults={'name': code, 'is_enabled': True})
            created_regions += int(region_created)
            provider, provider_created = _upsert_provider(row, media_type)
            created_providers += int(provider_created)
            if provider:
                ProviderRegionAvailability.objects.update_or_create(
                    provider=provider,
                    region=region,
                    media_type=media_type,
                    defaults={'display_priority': row.get('display_priority') or 0},
                )
                availability_count += 1
    return {'regions_created': created_regions, 'providers_created': created_providers, 'availability': availability_count, 'source': 'tmdb_db'}


@transaction.atomic
def sync_provider_items_once():
    try:
        result = _sync_from_api()
    except Exception as api_error:
        try:
            result = _sync_from_extracted_db()
        except Exception:
            # Keep the API error visible to the admin instead of masking both failures.
            raise RuntimeError(f'TMDB provider sync failed: {api_error}') from api_error
    cache.delete('enabled_watch_regions')
    cache.delete('enabled_watch_providers')
    return result
