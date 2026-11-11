
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden, HttpResponseRedirect, StreamingHttpResponse, Http404
from django.http.request import UnreadablePostError
from django.template.loader import render_to_string
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.contrib import messages
from django.contrib.auth.tokens import default_token_generator
from django.utils.text import slugify
from django.utils import timezone
from django.core.mail import send_mail
from django.core.cache import cache
from django.db.models import Sum, Count, F, Q
from django.urls import reverse
from django.db.models.functions import TruncDate
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt
from django.db import models, DatabaseError
import http.cookiejar
import urllib.request as _urlreq
import urllib.error as _urlerr
from urllib.parse import urlparse as _urlparse, urlencode as _urlencode
import json
import logging
import datetime
import csv
import requests
import calendar
import base64
import secrets
from functools import wraps
from .middleware import get_client_ip
from bs4 import BeautifulSoup
import psutil
import platform
from .models import (SiteSettings, ContentRow, WatchList, PlayerConfiguration, TMDBApiKey, NavbarItem, DataSourceUsageLog, ProviderItem, ProviderRegionAvailability, WatchRegion, CalendarMonthCache, AndroidApp, AndroidAppAccessLog, AndroidAppBuildLog, AndroidAppFailedAttempt, AndroidAppDevice, AndroidAppDailyUniqueVisitor, AndroidAppDeviceVisit, AndroidAppLog, ApkDownload, WebsiteVisitor, WebsiteVisitorVisit, Ad, AdImpression, AdsterraLink, AmazonAffiliateProduct, UserActivity)
from .tmdb_client import get_data_client, get_tmdb_db_connection, TMDBClient
from .utils import normalize_movie_item, normalize_series_item, normalize_movie_detail, normalize_series_detail

logger = logging.getLogger(__name__)
from .forms import (
    ContentRowForm, PlayerConfigurationForm, TMDBApiKeyForm, TMDBApiKeyEditForm, NavbarItemForm, ProviderItemForm, WatchRegionForm,
    BrandingSettingsForm, DisplaySettingsForm, FooterSettingsForm, DataSourceSettingsForm, TMDBDBSettingsForm,
    PlayerSettingsForm, URLBlockingSettingsForm, EmailSettingsForm, AndroidAppForm, AdForm, AdsterraSettingsForm, AdsterraLinkForm, AdMonetizationSettingsForm,
    AdSwitchesForm, GeneralSettingsForm, SecurityProtectionForm,
    GENERAL_FIELD_SECTIONS, SECURITY_FIELD_SECTIONS
)


def _provider_slug_matches(item, provider_slug):
    if not provider_slug:
        return True

    provider_exists = ProviderItem.objects.filter(slug=provider_slug, is_enabled=True).exists()
    if not provider_exists:
        return False

    watch_providers = item.get('watch_providers') or {}
    if isinstance(watch_providers, str):
        try:
            watch_providers = json.loads(watch_providers)
        except Exception:
            watch_providers = {}
    results = watch_providers.get('results', {}) if isinstance(watch_providers, dict) else {}

    if provider_slug == 'no-provider':
        if results == [] or results == {}:
            return True
        for region_data in results.values() if isinstance(results, dict) else []:
            if not isinstance(region_data, dict):
                continue
            for section in ['flatrate', 'rent', 'buy', 'ads', 'free']:
                if region_data.get(section):
                    return False
        return True

    for region_data in results.values() if isinstance(results, dict) else []:
        if not isinstance(region_data, dict):
            continue
        for section in ['flatrate', 'rent', 'buy', 'ads', 'free']:
            for provider in region_data.get(section, []) or []:
                name = provider.get('provider_name', '')
                slug = slugify(name)
                if slug == provider_slug:
                    return True
    return False


def _get_most_viewed_items(items, media_type, limit=6):
    """Rank normalized content items by real, non-bot detail-page visits."""
    if not items:
        return []

    item_paths = {}
    prefix = 'movies' if media_type == 'movie' else 'series'
    for item in items:
        paths = []
        item_id = item.get('id')
        slug = item.get('slug')
        if item_id:
            paths.append(f'/{prefix}/id/{item_id}/')
        if slug:
            paths.append(f'/{prefix}/{slug}/')
        for path in paths:
            item_paths[path] = item

    visit_counts = {
        row['path']: row['total']
        for row in WebsiteVisitorVisit.objects.filter(
            is_bot=False,
            path__in=list(item_paths),
        ).values('path').annotate(total=Count('id'))
    }

    ranked = []
    seen_ids = set()
    for path, count in sorted(visit_counts.items(), key=lambda entry: entry[1], reverse=True):
        item = item_paths[path]
        item_key = (item.get('id'), item.get('slug'))
        if item_key in seen_ids:
            continue
        seen_ids.add(item_key)
        ranked.append(item.copy())

    # New installations may not have detail-page history yet. Keep the sidebar useful
    # while real visits accumulate, without presenting the fallback as a view count.
    if len(ranked) < limit:
        fallback = sorted(items, key=lambda item: item.get('popularity') or item.get('vote_average') or 0, reverse=True)
        for item in fallback:
            item_key = (item.get('id'), item.get('slug'))
            if item_key in seen_ids:
                continue
            seen_ids.add(item_key)
            ranked.append(item.copy())
            if len(ranked) >= limit:
                break

    for position, item in enumerate(ranked[:limit], start=1):
        item['rank'] = position
    return ranked[:limit]


def _store_calendar_month_data(year, month, calendar_data):
    CalendarMonthCache.objects.update_or_create(
        year=year,
        month=month,
        defaults={
            'month_name': calendar_data['month_name'],
            'first_day': calendar_data['first_day'],
            'last_day': calendar_data['last_day'],
            'movies': calendar_data['movies'],
            'series': calendar_data['series'],
        }
    )


def _seed_calendar_month_window():
    """Non-blocking: skip seeding on page load. Use management command instead."""
    pass  # Calendar loads on-demand from frontend via calendar-db endpoint


def get_calendar_month_data(year, month):
    """Get movies and series for a specific month with caching"""
    cache_key = f"calendar_{year}_{month}"
    cached_data = cache.get(cache_key)
    if cached_data:
        return cached_data

    client = get_data_client()
    fallback_client = TMDBClient()
    tmdb = TMDBClient()
    
    # Calculate date range for the month
    first_day = datetime.date(year, month, 1)
    if month == 12:
        last_day = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    
    # Fetch movies — multiple pages, sort by popularity for better dates
    movies = []
    try:
        movie_params = {
            'primary_release_date.gte': first_day.strftime('%Y-%m-%d'),
            'primary_release_date.lte': last_day.strftime('%Y-%m-%d'),
            'sort_by': 'popularity.desc'
        }
        for page in range(1, 4):
            movie_params['page'] = page
            try:
                movie_data = client.discover_movies(movie_params)
            except Exception:
                movie_data = fallback_client.discover_movies(movie_params)
            results = movie_data.get('results', [])
            if not results:
                break
            for item in results:
                movies.append(normalize_movie_item(item))
            if page >= movie_data.get('total_pages', 1):
                break
    except Exception as e:
        logger.error("fetching calendar movies: %s", e)
    
    # Fetch series — new premieres this month + ongoing shows with episodes
    series = []
    seen_series = set()
    month_prefix = f'{year}-{month:02d}'
    try:
        # 1) New premieres this month (first_air_date in range)
        premieres_params = {
            'first_air_date.gte': first_day.strftime('%Y-%m-%d'),
            'first_air_date.lte': last_day.strftime('%Y-%m-%d'),
            'sort_by': 'popularity.desc'
        }
        for page in range(1, 3):
            premieres_params['page'] = page
            try:
                prem_data = client.discover_series(premieres_params)
            except Exception:
                prem_data = fallback_client.discover_series(premieres_params)
            results = prem_data.get('results', [])
            if not results:
                break
            for item in results:
                s = normalize_series_item(item)
                s['air_date'] = s.get('first_air_date', '')
                if s['id'] not in seen_series:
                    seen_series.add(s['id'])
                    series.append(s)
            if page >= prem_data.get('total_pages', 1):
                break

        # 2) Ongoing popular shows — fetch their episode air dates
        ongoing_params = {
            'sort_by': 'popularity.desc',
            'with_status': 'Returning Series'
        }
        try:
            ongoing_data = client.discover_series(ongoing_params)
        except Exception:
            ongoing_data = fallback_client.discover_series(ongoing_params)
        ongoing_results = ongoing_data.get('results', [])[:10]
        for show_item in ongoing_results:
            s = normalize_series_item(show_item)
            if s['id'] in seen_series:
                continue
            try:
                season_num = s.get('number_of_seasons', 1)
                ep_resp = tmdb._make_request(f'/tv/{s["id"]}/season/{season_num}', {})
                episodes = ep_resp.get('episodes', []) if ep_resp else []
                for ep in episodes:
                    ep_date = ep.get('air_date', '')
                    if ep_date and ep_date.startswith(month_prefix):
                        ep_show = dict(s)
                        ep_show['air_date'] = ep_date
                        ep_show['episode_number'] = ep.get('episode_number')
                        ep_show['season_number'] = ep.get('season_number', season_num)
                        ep_show['episode_title'] = ep.get('name', '')
                        if s['id'] not in seen_series:
                            seen_series.add(s['id'])
                        series.append(ep_show)
            except Exception:
                pass
    except Exception as e:
        logger.error("fetching calendar series: %s", e)
    
    calendar_data = {
        'year': year,
        'month': month,
        'month_name': calendar.month_name[month],
        'first_day': first_day.strftime('%Y-%m-%d'),
        'last_day': last_day.strftime('%Y-%m-%d'),
        'movies': movies,
        'series': series
    }
    
    # Cache for 24 hours
    cache.set(cache_key, calendar_data, 86400)
    _store_calendar_month_data(year, month, calendar_data)
    return calendar_data


def get_content_row_items(row, page=1):
    """Helper function to fetch items for a ContentRow using TMDB API directly with caching"""
    # Create a unique cache key based on row data and page
    cache_key = f"content_row_{row.id}_page_{page}_{row.media_type}_{row.row_type}_{row.genre_tmdb_id or 'no_genre'}"
    
    # Try to get from cache first
    cached_data = cache.get(cache_key)
    if cached_data:
        return cached_data

    client = get_data_client()
    fallback_client = TMDBClient()
    params = {'page': page}

    # Add region filter
    if row.region:
        params['region'] = row.region
    # Add language filter
    if row.language:
        params['language'] = row.language

    # Add sort parameter
    if row.sort_by:
        params['sort_by'] = row.sort_by

    # Add any custom filter params
    if row.filter_params:
        try:
            custom_params = json.loads(row.filter_params)
            params.update(custom_params)
        except json.JSONDecodeError:
            pass

    # Handle genre filter
    if row.row_type == 'genre' and row.genre_tmdb_id:
        params['with_genres'] = row.genre_tmdb_id

    # Determine the endpoint
    if row.media_type == 'movie':
        if row.row_type == 'popular':
            data = client.get_popular_movies(page=page, params=params)
        elif row.row_type == 'top_rated':
            data = client.get_top_rated_movies(page=page, params=params)
        elif row.row_type == 'upcoming':
            data = client.get_upcoming_movies(page=page, params=params)
        elif row.row_type == 'now_playing':
            data = client.get_now_playing_movies(page=page, params=params)
        else:  # genre or custom
            data = client.discover_movies(params)
            if not data.get('results') and client.__class__ is not TMDBClient:
                data = fallback_client.discover_movies(params)
    else:  # tv
        if row.row_type == 'popular':
            data = client.get_popular_series(page=page, params=params)
        elif row.row_type == 'top_rated':
            data = client.get_top_rated_series(page=page, params=params)
        elif row.row_type == 'on_the_air':
            data = client.get_on_the_air_series(page=page, params=params)
        elif row.row_type == 'airing_today':
            data = client.get_airing_today_series(page=page, params=params)
        else:  # genre or custom
            data = client.discover_series(params)
            if not data.get('results') and client.__class__ is not TMDBClient:
                data = fallback_client.discover_series(params)

    # Process results and add necessary fields for templates
    items = []
    results = data.get('results', [])
    for item in results:
        if row.media_type == 'movie':
            items.append(normalize_movie_item(item))
        else:
            items.append(normalize_series_item(item))

    # Cache the result for 1 hour (3600 seconds)
    result = (items, data.get('total_pages', 1))
    cache.set(cache_key, result, 3600)
    return result


def calendar_page(request):
    """Full-page release calendar"""
    # Seed calendar data for current window on first load
    _seed_calendar_month_window()
    return render(request, 'core/calendar.html')


@csrf_exempt
def calendar_db(request):
    """
    Fast calendar endpoint served from local DB (CalendarMonthCache).
    Returns movies and series for a specific month.
    GET /ajax/calendar-db/?year=2026&month=9
    POST /ajax/calendar-db/ — save data for a month (staff only)
    """
    if request.method == 'POST':
        # Writing the shared month cache is an admin action: without this gate
        # any anonymous visitor could overwrite the site-wide calendar data.
        if not (request.user.is_authenticated
                and (request.user.is_staff or request.user.is_superuser)):
            return JsonResponse({'success': False, 'error': 'Staff only'}, status=403)
        # Save fetched data to DB
        try:
            import json as _json
            body = _json.loads(request.body)
            year = body.get('year')
            month = body.get('month')
            movies = body.get('movies', [])
            series = body.get('series', [])
            if year and month:
                CalendarMonthCache.objects.update_or_create(
                    year=year, month=month,
                    defaults={
                        'month_name': calendar.month_name[month],
                        'first_day': f'{year}-{month:02d}-01',
                        'last_day': f'{year}-{month:02d}-{calendar.monthrange(year, month)[1]}',
                        'movies': movies,
                        'series': series,
                    }
                )
                return JsonResponse({'success': True, 'saved': len(movies) + len(series)})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})

    # GET — return cached data
    year = int(request.GET.get('year', timezone.localdate().year))
    month = int(request.GET.get('month', timezone.localdate().month))

    try:
        cached = CalendarMonthCache.objects.get(year=year, month=month)
        return JsonResponse({
            'success': True,
            'source': 'db',
            'year': cached.year,
            'month': cached.month,
            'month_name': cached.month_name,
            'first_day': cached.first_day,
            'last_day': cached.last_day,
            'movies': cached.movies or [],
            'series': cached.series or [],
            'last_synced': timezone.localtime(cached.last_synced_at).isoformat() if cached.last_synced_at else None,
        })
    except CalendarMonthCache.DoesNotExist:
        return JsonResponse({'success': False, 'movies': [], 'series': []})


def calendar_month_data(request):
    """AJAX endpoint to get calendar data for a specific month"""
    year = int(request.GET.get('year', timezone.localdate().year))
    month = int(request.GET.get('month', timezone.localdate().month))
    calendar_data = get_calendar_month_data(year, month)
    return JsonResponse(calendar_data)


def login_view(request):
    """Redirect to homepage and trigger login modal"""
    next_url = request.GET.get('next', '/')
    response = redirect(f'/?login_required=true&next={next_url}')
    return response


def page_not_found_view(request, exception=None):
    """Custom 404 page"""
    return render(request, 'core/404.html', status=404)


def permission_denied_view(request, exception=None):
    """Custom 403 page - for when staff/superuser is required"""
    return render(request, 'core/403.html', status=403)


def _build_home_data():
    movie_rows = ContentRow.objects.filter(media_type='movie', is_active=True)
    series_rows = ContentRow.objects.filter(media_type='tv', is_active=True)
    site_settings = SiteSettings.get_settings()

    current_month_data = {
        'year': timezone.localdate().year,
        'month': timezone.localdate().month,
        'month_name': calendar.month_name[timezone.localdate().month],
        'first_day': None,
        'last_day': None,
        'movies': [],
        'series': [],
    }

    movie_rows_data = []
    for row in movie_rows:
        items, total_pages = get_content_row_items(row, page=1)
        movie_rows_data.append({
            'row': row,
            'items': items,
            'total_pages': total_pages,
            'current_page': 1
        })

    series_rows_data = []
    for row in series_rows:
        items, total_pages = get_content_row_items(row, page=1)
        series_rows_data.append({
            'row': row,
            'items': items,
            'total_pages': total_pages,
            'current_page': 1
        })

    client = get_data_client()
    top_movies = []
    top_series = []

    if site_settings.curated_top_movie_ids:
        top_movies_cache_key = f"curated_top_movies_{site_settings.curated_top_movie_ids[:100]}"
        cached_top_movies = cache.get(top_movies_cache_key)
        if cached_top_movies:
            top_movies = cached_top_movies
        else:
            try:
                movie_ids = [int(x.strip()) for x in site_settings.curated_top_movie_ids.split(',') if x.strip().isdigit()]
                if hasattr(client, 'get_movies_by_ids'):
                    batched_movies = client.get_movies_by_ids(movie_ids)
                    top_movies = [normalize_movie_item(item) for item in batched_movies]
                else:
                    for movie_id in movie_ids:
                        item = client.get_movie_details(movie_id)
                        if item:
                            top_movies.append(normalize_movie_item(item))
                cache.set(top_movies_cache_key, top_movies, 3600)
            except Exception as e:
                logger.error("fetching curated top movies: %s", e)

    if not top_movies:
        top_movies_cache_key = 'top_movies'
        cached_top_movies = cache.get(top_movies_cache_key)
        if cached_top_movies:
            top_movies = cached_top_movies
        else:
            try:
                data = client.get_top_rated_movies(page=1)
                top_movies = [normalize_movie_item(item) for item in data.get('results', [])]
                cache.set(top_movies_cache_key, top_movies, 3600)
            except Exception as e:
                logger.error("fetching top movies: %s", e)

    if site_settings.curated_top_series_ids:
        top_series_cache_key = f"curated_top_series_{site_settings.curated_top_series_ids[:100]}"
        cached_top_series = cache.get(top_series_cache_key)
        if cached_top_series:
            top_series = cached_top_series
        else:
            try:
                series_ids = [int(x.strip()) for x in site_settings.curated_top_series_ids.split(',') if x.strip().isdigit()]
                if hasattr(client, 'get_series_by_ids'):
                    batched_series = client.get_series_by_ids(series_ids)
                    top_series = [normalize_series_item(item) for item in batched_series]
                else:
                    for series_id in series_ids:
                        item = client.get_series_details(series_id)
                        if item:
                            top_series.append(normalize_series_item(item))
                cache.set(top_series_cache_key, top_series, 3600)
            except Exception as e:
                logger.error("fetching curated top series: %s", e)

    if not top_series:
        top_series_cache_key = 'top_series'
        cached_top_series = cache.get(top_series_cache_key)
        if cached_top_series:
            top_series = cached_top_series
        else:
            try:
                data = client.get_top_rated_series(page=1)
                top_series = [normalize_series_item(item) for item in data.get('results', [])]
                cache.set(top_series_cache_key, top_series, 3600)
            except Exception as e:
                logger.error("fetching top series: %s", e)

    return {
        'movie_rows': movie_rows_data,
        'series_rows': series_rows_data,
        'top_movies': top_movies,
        'top_series': top_series,
        'current_month_data': current_month_data,
    }

@cache_control(public=True, max_age=600, stale_while_revalidate=3600)
def index(request):
    request.session.pop('active_provider_slug', None)
    request.session.pop('active_provider_id', None)
    request.session.modified = True
    return render(request, 'core/index.html')

EXTRACTOR_PLAYER_NAME = 'Cineplayer 2'


def _ensure_extractor_player():
    """Guarantee the built-in extractor player exists in the DB.

    The detail-page server list is DB-driven; if the 'Cineplayer 2' row
    is missing (fresh DB, imported dump) or was deactivated, users would have
    no way to select browser extraction. This creates/repairs the row so the
    extractor always shows up as a selectable player, in every environment.
    Idempotent and cheap — one indexed query on the happy path.
    """
    try:
        # Legacy name from before the rename — migrate it so old DBs keep
        # working without a manual step.
        PlayerConfiguration.objects.filter(name='Videasy Extractor').update(
            name=EXTRACTOR_PLAYER_NAME)
        obj = PlayerConfiguration.objects.filter(name=EXTRACTOR_PLAYER_NAME).first()
        if obj is None:
            PlayerConfiguration.objects.create(
                name=EXTRACTOR_PLAYER_NAME,
                media_type='both',
                is_active=True,
                order=1,
                player_color='e50914',
            )
        elif (not obj.is_active) or obj.media_type not in ('both', 'movie', 'tv') or (
                obj.custom_movie_iframe_url or obj.custom_tv_iframe_url
                or obj.custom_iframe_url or obj.custom_iframe_html
                or obj.custom_movie_iframe_html or obj.custom_tv_iframe_html):
            # Repair a broken/inactive row so it always behaves as pure extraction
            obj.is_active = True
            obj.media_type = 'both'
            obj.custom_movie_iframe_url = ''
            obj.custom_tv_iframe_url = ''
            obj.custom_iframe_url = ''
            obj.custom_iframe_html = ''
            obj.custom_movie_iframe_html = ''
            obj.custom_tv_iframe_html = ''
            obj.save(update_fields=[
                'is_active', 'media_type',
                'custom_movie_iframe_url', 'custom_tv_iframe_url',
                'custom_iframe_url', 'custom_iframe_html',
                'custom_movie_iframe_html', 'custom_tv_iframe_html',
            ])
    except Exception:
        logger.warning("_ensure_extractor_player failed", exc_info=True)


def _get_detail_players(media_type):
    """Active players for a detail page, with the extractor guaranteed present."""
    _ensure_extractor_player()
    players = PlayerConfiguration.objects.filter(
        media_type__in=[media_type, 'both'],
        is_active=True,
    ).order_by('order', 'id')
    return _strip_videasy_custom_urls(players)


def _strip_videasy_custom_urls(all_players):
    """Force the extractor player to always use extraction — strip custom URLs from DB.
    Only affects the extractor row (not CinePlayer or others)."""
    players = list(all_players)
    for v in players:
        if v.name == EXTRACTOR_PLAYER_NAME:
            v.custom_movie_iframe_url = ''
            v.custom_iframe_url = ''
            v.custom_iframe_html = ''
            v.custom_movie_iframe_html = ''
            v.custom_tv_iframe_url = ''
            v.custom_tv_iframe_html = ''
            break
    return players


def _resolve_genre_name_to_id(genre_name, media_type='movie'):
    """Resolve a genre name (e.g. 'Action') to its TMDB genre ID."""
    if not genre_name:
        return ''
    genre_name = genre_name.strip()
    # TV genre aliases — footer uses short names, TMDB uses combined names
    tv_alias = {
        'action': 'action & adventure',
        'sci-fi': 'sci-fi & fantasy',
    }
    lookup_name = tv_alias.get(genre_name.lower(), genre_name.lower()) if media_type == 'tv' else genre_name.lower()
    cache_key = f'{media_type}_genres'
    genres = cache.get(cache_key)
    if not genres:
        try:
            client = get_data_client()
            if media_type == 'tv':
                result = client.get_series_genres()
            else:
                result = client.get_movie_genres()
            genres = result.get('genres', [])
            cache.set(cache_key, genres, 3600 * 24)
        except Exception:
            genres = []
    for g in genres:
        if g.get('name', '').lower() == lookup_name:
            return str(g.get('id', ''))
    return ''


def _resolve_country_name_to_code(country_name):
    """Resolve a country name (e.g. 'United States') to its ISO 3166-1 code."""
    if not country_name:
        return ''
    country_name = country_name.strip()
    mapping = {
        'australia': 'AU', 'canada': 'CA', 'netherlands': 'NL',
        'united kingdom': 'GB', 'united states': 'US',
        'india': 'IN', 'germany': 'DE', 'france': 'FR',
        'japan': 'JP', 'south korea': 'KR', 'brazil': 'BR',
        'mexico': 'MX', 'spain': 'ES', 'italy': 'IT',
        'russia': 'RU', 'china': 'CN', 'turkey': 'TR',
    }
    return mapping.get(country_name.lower(), '')


def _resolve_catalog_provider(request):
    """Resolve an explicit provider or the session-wide catalog provider."""
    if request.GET.get('clear_provider') == '1' or ('provider' in request.GET and not request.GET.get('provider')):
        request.session.pop('active_provider_slug', None)
        request.session.pop('active_provider_id', None)
        request.session.modified = True
        return None

    requested_slug = (request.GET.get('provider') or '').strip().lower()
    if requested_slug:
        provider = ProviderItem.objects.filter(slug=requested_slug, is_enabled=True).first()
        if provider:
            request.session['active_provider_slug'] = provider.slug
            request.session['active_provider_id'] = provider.tmdb_provider_id
            request.session.modified = True
            return provider
        request.session.pop('active_provider_slug', None)
        request.session.pop('active_provider_id', None)
        request.session.modified = True
        return None

    session_slug = request.session.get('active_provider_slug')
    if not session_slug:
        return None
    provider = ProviderItem.objects.filter(slug=session_slug, is_enabled=True).first()
    if not provider:
        request.session.pop('active_provider_slug', None)
        request.session.pop('active_provider_id', None)
        request.session.modified = True
    return provider


def _render_content_row(row_data):
    """Render a single content row (title + horizontal scrolling cards) as HTML."""
    row = row_data['row']
    items = row_data['items']
    if not items:
        return ''
    site_settings = SiteSettings.get_settings()
    cards = []
    for item in items:
        title = item.get('title', '')
        slug = item.get('slug', '')
        item_id = item.get('id', '')
        img = item.get('cover_url', '')
        media = item.get('media_type', row.media_type)
        if media == 'tv':
            link = f'/series/id/{item_id}/'
        else:
            link = f'/movies/id/{item_id}/'
        rating = item.get('vote_average', 0)
        year = item.get('release_date', '') or item.get('first_air_date', '')
        if year and len(str(year)) >= 4:
            year = str(year)[:4]
        else:
            year = ''
        rating_val = round(float(rating or 0), 1) if rating else 0
        rating_html = f'<div class="home-card-rating">\u2605 {rating_val}</div>' if rating_val > 0 else ''
        year_html = f'<div class="home-card-year">{year}</div>' if year else ''
        card = (
            f'<a href="{link}" class="home-card" title="{title}">'
            f'<div class="home-card-img-wrap">'
            f'{rating_html}'
            f'<img src="{img}" alt="{title}" loading="lazy">'
            f'</div>'
            f'<div class="home-card-title">{title}</div>'
            f'{year_html}'
            f'</a>'
        )
        cards.append(card)
    return (
        f'<div class="home-row reveal-on-scroll">'
        f'<div class="home-row-header">'
        f'<h2 class="home-row-title">{row.title or row.get_row_type_display()}</h2>'
        f'</div>'
        f'<div class="home-row-scroll reveal-stagger">{"".join(cards)}</div>'
        f'</div>'
    )


def _render_top_cards(items):
    """Render top-rated items as numbered cards."""
    cards = []
    for i, item in enumerate(items, 1):
        title = item.get('title', '')
        item_id = item.get('id', '')
        img = item.get('cover_url', '')
        media = item.get('media_type', 'movie')
        link = f'/series/id/{item_id}/' if media == 'tv' else f'/movies/id/{item_id}/'
        cards.append(
            f'<a href="{link}" class="top-card" title="{title}">'
            f'<img src="{img}" alt="{title}" loading="lazy" class="top-card-img">'
            f'<span class="top-card-title">{title}</span>'
            f'</a>'
        )
    return ''.join(cards)


def home_initial_data(request):
    """Return just the shell (stats bar + row IDs). Content rows loaded progressively by JS."""
    cached = cache.get('home_page_shell')
    # Safety: if cache has empty row_ids but DB has content, bust stale cache
    if cached and cached.get('row_ids'):
        return JsonResponse(cached)
    if cached:
        cache.delete('home_page_shell')
    try:
        row_ids = list(ContentRow.objects.filter(is_active=True).order_by('order').values_list('id', flat=True))
        stats_html = (
            '<div class="reveal-on-scroll" style="display:flex;justify-content:center;gap:3rem;padding:2rem 2rem 1rem;margin:0 auto;max-width:800px;">'
            '<div style="text-align:center;"><div style="font-size:1.8rem;font-weight:800;color:#00c896;">1000+</div><div style="font-size:0.75rem;color:#6a6a7a;text-transform:uppercase;letter-spacing:0.08em;margin-top:0.2rem;">Movies</div></div>'
            '<div style="text-align:center;"><div style="font-size:1.8rem;font-weight:800;color:#00c896;">500+</div><div style="font-size:0.75rem;color:#6a6a7a;text-transform:uppercase;letter-spacing:0.08em;margin-top:0.2rem;">TV Series</div></div>'
            '<div style="text-align:center;"><div style="font-size:1.8rem;font-weight:800;color:#00c896;">24/7</div><div style="font-size:0.75rem;color:#6a6a7a;text-transform:uppercase;letter-spacing:0.08em;margin-top:0.2rem;">Streaming</div></div>'
            '<div style="text-align:center;"><div style="font-size:1.8rem;font-weight:800;color:#00c896;">HD</div><div style="font-size:0.75rem;color:#6a6a7a;text-transform:uppercase;letter-spacing:0.08em;margin-top:0.2rem;">Quality</div></div>'
            '</div>'
        )
        result = {'html': stats_html, 'row_ids': row_ids, 'has_top': True}
        cache.set('home_page_shell', result, 1800)
        return JsonResponse(result)
    except Exception as e:
        logger.error('home_initial_data error: %s', e)
        return JsonResponse({'html': '', 'row_ids': [], 'has_top': False})


@require_GET
def home_row_ajax(request):
    """Return HTML for a single content row — called progressively by the home page JS."""
    row_id = request.GET.get('row_id', '')
    page = int(request.GET.get('page', 1))
    cache_key = f"home_row_{row_id}_p{page}"
    cached = cache.get(cache_key)
    if cached:
        return JsonResponse({'html': cached, 'row_id': row_id})
    try:
        row = ContentRow.objects.get(id=row_id, is_active=True)
        items, total_pages = get_content_row_items(row, page=page)
        if not items:
            return JsonResponse({'html': '', 'row_id': row_id})
        row_data = {'row': row, 'items': items, 'total_pages': total_pages, 'current_page': page}
        html = _render_content_row(row_data)
        cache.set(cache_key, html, 900)
        return JsonResponse({'html': html, 'row_id': row_id})
    except ContentRow.DoesNotExist:
        return JsonResponse({'html': '', 'row_id': row_id})
    except Exception as e:
        logger.error('home_row_ajax error: %s', e)
        return JsonResponse({'html': '', 'row_id': row_id, 'error': str(e)})


@require_GET
def home_top_ajax(request):
    """Return HTML for top-rated curated movies or series — called by the home page JS."""
    media_type = request.GET.get('media_type', 'movie')
    cache_key = f"home_top_{media_type}"
    cached = cache.get(cache_key)
    if cached:
        return JsonResponse({'html': cached, 'media_type': media_type})
    try:
        settings_obj = SiteSettings.get_settings()
        client = get_data_client()
        ids_str = settings_obj.curated_top_movie_ids if media_type == 'movie' else settings_obj.curated_top_series_ids
        if not ids_str:
            return JsonResponse({'html': '', 'media_type': media_type})
        movie_ids = [int(x.strip()) for x in ids_str.split(',') if x.strip().isdigit()]
        if hasattr(client, 'get_movies_by_ids'):
            items = [normalize_movie_item(item) for item in client.get_movies_by_ids(movie_ids)]
        elif media_type == 'movie':
            items = []
            for mid in movie_ids:
                try:
                    data = client.get_movie_details(mid)
                    if data:
                        items.append(normalize_movie_item(data))
                except Exception:
                    pass
        else:
            items = []
            for mid in movie_ids:
                try:
                    data = client.get_tv_details(mid)
                    if data:
                        data['media_type'] = 'tv'
                        items.append(normalize_movie_item(data))
                except Exception:
                    pass
        if not items:
            return JsonResponse({'html': '', 'media_type': media_type})
        label = 'Top Rated Movies' if media_type == 'movie' else 'Top Rated Series'
        html = (
            '<div class="home-row reveal-on-scroll"><div class="home-row-header">'
            '<h2 class="home-row-title">' + label + '</h2>'
            '</div><div class="home-row-scroll reveal-stagger">'
            + _render_top_cards(items) + '</div></div>'
        )
        cache.set(cache_key, html, 900)
        return JsonResponse({'html': html, 'media_type': media_type})
    except Exception as e:
        logger.error('home_top_ajax error: %s', e)
        return JsonResponse({'html': '', 'media_type': media_type})


def is_staff_or_superuser(user):
    return user.is_staff or user.is_superuser

@login_required
@user_passes_test(is_staff_or_superuser)
def admin_dashboard(request):
    site_settings = SiteSettings.get_settings()
    from django.contrib.auth.models import User
    from .models import PlayHistory, WebsiteVisitor
    from django.utils import timezone
    import os

    today = timezone.localdate()

    # Determine which DB to query for user data
    # Try external first if routing enabled, fall back to default
    user_db = 'default'
    try:
        from .models import DBRoutingConfig
        cfg = DBRoutingConfig.get_config()
        if cfg.use_external_db and cfg.external_db_ready:
            user_db = 'external'
    except Exception:
        pass

    # Visitor count today — try user_db first, then default
    visitors_today = 0
    for db_alias in ([user_db, 'default'] if user_db != 'default' else ['default']):
        try:
            visitors_today = WebsiteVisitor.objects.using(db_alias).filter(
                last_seen_at__date=today
            ).count()
            break
        except Exception:
            continue

    # Total users (always on default — auth.User is local)
    try:
        user_count = User.objects.count()
    except Exception:
        user_count = 0
    # Also count users from external DB if different
    if user_db == 'default':
        try:
            external_user_count = User.objects.using('external').count()
            if external_user_count > user_count:
                user_count = external_user_count
        except Exception:
            pass

    # Play sessions today — try user_db first, then default
    plays_today = 0
    for db_alias in ([user_db, 'default'] if user_db != 'default' else ['default']):
        try:
            plays_today = PlayHistory.objects.using(db_alias).filter(
                last_played_at__date=today
            ).count()
            break
        except Exception:
            continue

    # DB size — show both local and external if applicable
    db_size = '--'
    try:
        db_path = os.path.join(settings.BASE_DIR, 'db.sqlite3')
        if os.path.exists(db_path):
            size = os.path.getsize(db_path)
            db_size = f'{size / 1024 / 1024:.0f}MB'
    except Exception:
        pass

    # External DB size
    ext_db_size = '--'
    if user_db == 'external':
        try:
            from django.db import connections
            cursor = connections['external'].cursor()
            cursor.execute("SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024, 1) FROM information_schema.tables WHERE table_schema = DATABASE()")
            row = cursor.fetchone()
            if row and row[0]:
                ext_db_size = f'{row[0]:.0f}MB'
        except Exception:
            pass

    # ------------------------------------------------------------------ status
    # The dashboard used to be a launcher: four grids of links that repeated the
    # sidebar. It now answers "is anything wrong right now", because that is the
    # one question the sidebar cannot. Everything below is read-only and each
    # block is wrapped, so a missing table shows a gap rather than a 500.
    findings, open_incidents, recent_audit = [], [], []
    blocked_now = blocked_today = api_errors_day = 0
    five_xx_pct, five_xx_failed, five_xx_total = 0.0, 0, 0
    builds, gate_rejections = [], 0

    try:
        from .health import run_checks
        # record=False: the sidebar badge already opens/closes incidents on its
        # own schedule, so the dashboard only reports and never writes.
        findings = run_checks(record=False)
    except Exception:
        logger.debug('dashboard: health checks unavailable', exc_info=True)

    try:
        from .models import AlertIncident
        open_incidents = list(
            AlertIncident.objects.filter(resolved_at__isnull=True)
            .order_by('opened_at')[:5])
    except Exception:
        logger.debug('dashboard: could not read incidents', exc_info=True)

    try:
        from .audit import audit_report
        recent_audit = audit_report(limit=6)
    except Exception:
        logger.debug('dashboard: could not read the audit log', exc_info=True)

    try:
        from .models import BlockedIP, ErrorLog, SyncedUser
        blocked_now = BlockedIP.objects.filter(is_active=True).count()
        blocked_today = SyncedUser.objects.filter(
            block_mode__gt='', blocked_at__date=today).count()
        api_errors_day = ErrorLog.objects.filter(
            created_at__gte=timezone.now() - timezone.timedelta(hours=24)).count()
    except Exception:
        logger.debug('dashboard: could not read the counters', exc_info=True)

    try:
        from .health import five_xx_rate
        five_xx_pct, five_xx_failed, five_xx_total = five_xx_rate(1440)
    except Exception:
        logger.debug('dashboard: could not compute the 5xx rate', exc_info=True)

    try:
        from datetime import timedelta as _td
        from django.db.models import Count, Sum
        from .models import AndroidAppBuildLog, AndroidAppFailedAttempt
        week_ago = today - _td(days=7)
        builds = list(
            AndroidAppBuildLog.objects.filter(access_date__gte=week_ago)
            .values('build_identifier')
            .annotate(reqs=Sum('connection_count'))
            .order_by('-reqs')[:5])
        total_reqs = sum((b['reqs'] or 0) for b in builds) or 1
        for row in builds:
            row['share'] = round(100.0 * (row['reqs'] or 0) / total_reqs, 1)
        gate_rejections = AndroidAppFailedAttempt.objects.filter(
            attempted_at__date__gte=week_ago,
            failure_reason='identity_invalid').count()
    except Exception:
        logger.debug('dashboard: could not read the build logs', exc_info=True)

    failing = [f for f in findings if f.get('ok') is False]
    unknown = [f for f in findings if f.get('ok') is None]

    # ------------------------------------------------------- traffic charts
    # One place that answers "who is actually connected, and what are we
    # refusing": web and Android connections per day for the last two weeks,
    # plus the blocks standing right now and the refusals served each day.
    traffic = {'labels': [], 'web': [], 'android': [], 'blocked_hits': []}
    traffic_totals = {
        'web_today': 0, 'android_today': 0, 'web_14': 0, 'android_14': 0,
        'blocked_now': blocked_now, 'blocked_headers': 0, 'blocked_today_hits': 0,
        'permanent': 0, 'new_today': 0, 'top_bots': [],
    }
    try:
        from datetime import timedelta as _td2
        from django.db.models import Count, Sum, Q
        from .models import (AndroidAppAccessLog, BlockedHeader, RequestLog,
                             WebsiteVisitorVisit)
        start = today - _td2(days=13)
        day_list = [start + _td2(days=i) for i in range(14)]
        traffic['labels'] = [d.strftime('%d %b') for d in day_list]

        # Web: distinct visitors per day, bots excluded (the tracker keeps them
        # apart, so a scraper never inflates "people connected").
        web_by_day = dict(
            WebsiteVisitorVisit.objects
            .filter(visited_at__date__gte=start, is_bot=False)
            .values('visited_at__date')
            .annotate(n=Count('visitor', distinct=True))
            .values_list('visited_at__date', 'n'))

        # Android: the app's own per-day connection counter.
        android_by_day = dict(
            AndroidAppAccessLog.objects
            .filter(access_date__gte=start)
            .values('access_date')
            .annotate(n=Sum('connection_count'))
            .values_list('access_date', 'n'))

        # Refusals served per day, from the request log (403 = block page).
        blocked_by_day = dict(
            RequestLog.objects
            .filter(created_at__date__gte=start, status_code=403)
            .values('created_at__date')
            .annotate(n=Count('id'))
            .values_list('created_at__date', 'n'))

        for d in day_list:
            traffic['web'].append(int(web_by_day.get(d) or 0))
            traffic['android'].append(int(android_by_day.get(d) or 0))
            traffic['blocked_hits'].append(int(blocked_by_day.get(d) or 0))

        traffic_totals['web_today'] = traffic['web'][-1]
        traffic_totals['android_today'] = traffic['android'][-1]
        traffic_totals['web_14'] = sum(traffic['web'])
        traffic_totals['android_14'] = sum(traffic['android'])
        traffic_totals['blocked_today_hits'] = traffic['blocked_hits'][-1]
        traffic_totals['blocked_headers'] = BlockedHeader.objects.filter(
            is_active=True).count()
        traffic_totals['new_today'] = BlockedIP.objects.filter(
            is_active=True, blocked_at__date=today).count()
        traffic_totals['permanent'] = BlockedIP.objects.filter(
            is_active=True, is_permanent=True).count()
        # Which programs the 403s came from, so a repeating bot is visible
        # without opening the log.
        top_bots = (RequestLog.objects
                    .filter(created_at__gte=timezone.now() - timezone.timedelta(hours=24),
                            status_code=403)
                    .exclude(user_agent='')
                    .values('user_agent')
                    .annotate(n=Count('id'))
                    .order_by('-n')[:5])
        traffic_totals['top_bots'] = [
            {'ua': (r['user_agent'] or '')[:60], 'n': r['n']} for r in top_bots]
    except Exception:
        logger.debug('dashboard: could not build the traffic charts', exc_info=True)

    return render(request, 'core/admin_dashboard.html', {
        'traffic': traffic,
        'traffic_totals': traffic_totals,
        'site_settings': site_settings,
        'visitors_today': visitors_today,
        'user_count': user_count,
        'plays_today': plays_today,
        'db_size': db_size,
        'ext_db_size': ext_db_size,
        'active_db': user_db,
        'findings': findings,
        'failing_checks': failing,
        'unknown_checks': unknown,
        'open_incidents': open_incidents,
        'recent_audit': recent_audit,
        'blocked_now': blocked_now,
        'blocked_today': blocked_today,
        'api_errors_day': api_errors_day,
        'five_xx_pct': five_xx_pct,
        'five_xx_failed': five_xx_failed,
        'five_xx_total': five_xx_total,
        'builds': builds,
        'gate_rejections': gate_rejections,
    })


@require_http_methods(["GET"])
def ajax_dashboard_stats(request):
    """Live stats for the admin dashboard — called every 30s by auto-refresh."""
    from django.contrib.auth.models import User
    from .models import PlayHistory, UserSession, UserPageView, WebsiteVisitor, EmailSendLog
    from django.utils import timezone
    import os

    now = timezone.now()
    today = now.date()
    hour_ago = now - timezone.timedelta(hours=1)
    five_min_ago = now - timezone.timedelta(minutes=5)

    # Determine DB alias
    user_db = 'default'
    try:
        from .models import DBRoutingConfig
        cfg = DBRoutingConfig.get_config()
        if cfg.use_external_db and cfg.external_db_ready:
            user_db = 'external'
    except Exception:
        pass

    def _count(model, **kwargs):
        for db in ([user_db, 'default'] if user_db != 'default' else ['default']):
            try:
                return model.objects.using(db).filter(**kwargs).count()
            except Exception:
                continue
        return 0


    # Active users (logged in in last 5 min)
    active_users = 0
    try:
        active_users = User.objects.filter(
            last_login__gte=five_min_ago
        ).count()
    except Exception:
        pass

    # Visitors today
    visitors_today = _count(WebsiteVisitor, last_seen_at__date=today)

    # Total users
    user_count = User.objects.count()

    # Views today
    views_today = _count(UserPageView, viewed_at__date=today)

    # Plays today
    plays_today = _count(PlayHistory, last_played_at__date=today)

    # Active sessions

    active_sessions = 0
    try:
        active_sessions = UserSession.objects.using(user_db).filter(
            is_active=True
        ).count()
    except Exception:
        pass
    if active_sessions == 0 and user_db != 'default':
        try:
            active_sessions = UserSession.objects.filter(is_active=True).count()
        except Exception:
            pass

    # Emails sent today
    emails_today = 0
    try:
        emails_today = EmailSendLog.objects.filter(
            created_at__date=today, status='sent'
        ).count()
    except Exception:
        pass

    # Server health
    import subprocess
    server_health = 'healthy'
    try:
        # Check disk usage
        result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=5)
        lines = result.stdout.strip().split('\n')
        if len(lines) > 1:
            parts = lines[1].split()
            usage_pct = int(parts[4].replace('%', ''))
            if usage_pct > 90:
                server_health = 'critical'
            elif usage_pct > 75:
                server_health = 'warning'
    except Exception:
        pass

    # Recent activity (last 10 events)
    activity = []
    try:
        # Recent logins
        recent_sessions = UserSession.objects.using(user_db).order_by('-logged_in_at')[:5]
        for s in recent_sessions:
            try:
                uname = s.user.username
            except Exception:
                uname = f'User #{s.user_id}'
            activity.append({
                'type': 'login',
                'icon': 'sign-in-alt',
                'color': '#4ade80',
                'text': f'{uname} logged in via {s.source}',
                'time': timezone.localtime(s.logged_in_at).strftime('%I:%M %p'),
            })
    except Exception:
        pass

    try:
        # Recent plays
        recent_plays = PlayHistory.objects.using(user_db).order_by('-last_played_at')[:5]
        for p in recent_plays:
            try:
                uname = p.user.username
            except Exception:
                uname = f'User #{p.user_id}'
            title = p.title or f'TMDB #{p.tmdb_id}'
            activity.append({
                'type': 'play',
                'icon': 'play',
                'color': '#60a5fa',
                'text': f'{uname} watched {title}',
                'time': timezone.localtime(p.last_played_at).strftime('%I:%M %p'),
            })
    except Exception:
        pass

    try:
        # Recent emails

        recent_emails = EmailSendLog.objects.order_by('-created_at')[:5]
        for e in recent_emails:
            activity.append({
                'type': 'email',
                'icon': 'envelope',
                'color': '#a78bfa',
                'text': f'Email sent: {e.subject[:50]}',
                'time': timezone.localtime(e.created_at).strftime('%I:%M %p'),
            })
    except Exception:
        pass

    # Sort by most recent (heuristic: just sort by time string)
    activity.sort(key=lambda x: x['time'], reverse=True)
    activity = activity[:15]

    return JsonResponse({
        'ok': True,
        'stats': {
            'active_users': active_users,
            'visitors_today': visitors_today,
            'user_count': user_count,
            'views_today': views_today,
            'plays_today': plays_today,
            'active_sessions': active_sessions,
            'emails_today': emails_today,
            'server_health': server_health,
        },
        'activity': activity,
        'updated_at': timezone.localtime(now).strftime('%I:%M:%S %p'),
    })


# Helper functions for ads
def get_user_today_clicks(user, ip_address):
    today = timezone.localdate()
    try:
        if user and user.is_authenticated:
            activity, created = UserActivity.objects.get_or_create(user=user, activity_date=today, defaults={'ip_address': ip_address})
        else:
            activity, created = UserActivity.objects.get_or_create(ip_address=ip_address, activity_date=today, defaults={'user': None})
    except Exception:
        # Race condition fallback
        if user and user.is_authenticated:
            activity = UserActivity.objects.get(user=user, activity_date=today)
        else:
            activity = UserActivity.objects.get(ip_address=ip_address, activity_date=today)
    return activity.clicks_today


def get_eligible_ads(user, ip_address):
    today_clicks = get_user_today_clicks(user, ip_address)
    eligible_ads = Ad.objects.filter(is_active=True, clicks_required__lte=today_clicks).order_by('order', 'name')
    return eligible_ads


@require_http_methods(["GET"])
def ajax_get_autoclick_ads(request):
    """Return fresh eligible auto-click ad URLs + every-N-clicks cadence setting."""
    user = request.user if request.user.is_authenticated else None
    ip_address = request.META.get('REMOTE_ADDR')
    try:
        eligible_ads = get_eligible_ads(user, ip_address)
        urls = []
        for ad in eligible_ads:
            url = None
            if ad.provider == 'amazon_affiliate' and ad.affiliate_url:
                url = ad.affiliate_url
            elif ad.provider == 'custom_image' and ad.link_url:
                url = ad.link_url
            if url:
                urls.append(url)
        today_clicks = get_user_today_clicks(user, ip_address)
        site_settings = SiteSettings.get_settings()
        every_clicks = int(getattr(site_settings, 'auto_click_every_clicks', 10) or 10)
        if every_clicks < 1:
            every_clicks = 1
        return JsonResponse({
            'success': True,
            'urls': urls,
            'today_clicks': today_clicks,
            'every_clicks': every_clicks,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'urls': [], 'today_clicks': 0, 'every_clicks': 10, 'error': str(e)})


# Ad views
def _ad_inventory():
    """One row per ad unit from every ad source, active units first.

    The Ads page shows this so an administrator can see - and flip - every ad
    the site can serve from a single place.
    """
    rows = []
    for ad in Ad.objects.all().order_by('order', 'name'):
        rows.append({
            'kind': 'ad',
            'kind_label': 'Ad unit',
            'id': ad.id,
            'name': ad.name or 'Untitled ad unit',
            'detail': '%s \u00b7 %s' % (ad.get_provider_display(), ad.get_position_display()),
            'is_active': ad.is_active,
            'edit_url': reverse('ad_edit', args=[ad.id]),
        })
    for link in AdsterraLink.objects.all().order_by('order', 'id'):
        detail = 'Adsterra'
        if link.ad_type:
            detail += ' \u00b7 ' + link.ad_type
        if link.url:
            detail += ' \u00b7 link unit (opens after the click threshold)'
        if (link.code or '').strip():
            detail += ' \u00b7 JS unit (loaded on every public page)'
        if not link.url and not (link.code or '').strip():
            detail += ' \u00b7 no URL or code saved yet'
        rows.append({
            'kind': 'adsterra',
            'kind_label': 'Adsterra',
            'id': link.id,
            'name': link.name or link.ad_type or 'Untitled Adsterra unit',
            'detail': detail,
            'is_active': link.is_active,
            'edit_url': reverse('admin_adsterra'),
        })
    for product in AmazonAffiliateProduct.objects.all().order_by('order', 'title'):
        rows.append({
            'kind': 'amazon',
            'kind_label': 'Amazon product',
            'id': product.id,
            'name': product.title or product.affiliate_url[:80],
            'detail': 'Amazon Affiliate \u00b7 in-grid tile / gating source',
            'is_active': product.is_active,
            'edit_url': '',
        })
    rows.sort(key=lambda r: (not r['is_active'], r['kind_label'], str(r['name']).lower()))
    return rows


@login_required
@user_passes_test(is_staff_or_superuser)
def ad_list(request):
    """Single Ads page: ad control centre, ad units, Adsterra, monetization settings."""
    site_settings = SiteSettings.get_settings()

    if request.method == 'POST':
        if request.POST.get('form') == 'switches':
            switches_form = AdSwitchesForm(request.POST, instance=site_settings)
            if switches_form.is_valid():
                switches_form.save()
                messages.success(request, 'Ad switches updated.')
            else:
                messages.error(request, 'Could not save ad switches: %s' % '; '.join(
                    '%s: %s' % (name, ', '.join(errs))
                    for name, errs in switches_form.errors.items()
                ))
            return HttpResponseRedirect(reverse('ad_list') + '#ad-control')

        ads_form = AdMonetizationSettingsForm(request.POST, instance=site_settings)
        if ads_form.is_valid():
            ads_form.save()
            messages.success(request, 'Ad settings saved.')
        else:
            messages.error(request, 'Could not save ad settings: %s' % '; '.join(
                '%s: %s' % (name, ', '.join(errs))
                for name, errs in ads_form.errors.items()
            ))
        return redirect('ad_list')

    ads = Ad.objects.all().order_by('order', 'name')
    inventory = _ad_inventory()
    inventory_sources = []
    for kind, label in (('ad', 'Ad unit'), ('adsterra', 'Adsterra'), ('amazon', 'Amazon product')):
        rows_of_kind = [r for r in inventory if r['kind'] == kind]
        inventory_sources.append({
            'kind': kind,
            'label': label,
            'total': len(rows_of_kind),
            'active': len([r for r in rows_of_kind if r['is_active']]),
        })
    return render(request, 'core/ad_list.html', {
        'ads': ads,
        'inventory': inventory,
        'inventory_sources': inventory_sources,
        'inventory_active': len([r for r in inventory if r['is_active']]),
        'links': AdsterraLink.objects.all().order_by('order', 'id'),
        'link_form': AdsterraLinkForm(),
        'adsterra_form': AdsterraSettingsForm(instance=site_settings),
        'ads_form': AdMonetizationSettingsForm(instance=site_settings),
        'switches_form': AdSwitchesForm(instance=site_settings),
        'site_settings': site_settings,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def ad_toggle_any(request, kind, obj_id):
    """Enable/disable one ad unit of any source from the Ads control centre."""
    sources = {
        'ad': (Ad, 'Ad unit'),
        'adsterra': (AdsterraLink, 'Adsterra unit'),
        'amazon': (AmazonAffiliateProduct, 'Amazon product'),
    }
    if request.method != 'POST':
        return redirect('ad_list')
    if kind not in sources:
        messages.error(request, 'Unknown ad source.')
        return redirect('ad_list')
    model, label = sources[kind]
    obj = get_object_or_404(model, id=obj_id)
    obj.is_active = not obj.is_active
    obj.save()
    messages.success(request, '%s "%s" is now %s.' % (
        label, str(obj)[:80], 'enabled' if obj.is_active else 'disabled'))
    return HttpResponseRedirect(reverse('ad_list') + '#ad-control')


@login_required
@user_passes_test(is_staff_or_superuser)
def ad_create(request):
    if request.method == 'POST':
        form = AdForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('ad_list')
    else:
        form = AdForm()
    return render(request, 'core/ad_form.html', {'form': form, 'action': 'Create'})


@login_required
@user_passes_test(is_staff_or_superuser)
def ad_edit(request, ad_id):
    ad = get_object_or_404(Ad, id=ad_id)
    if request.method == 'POST':
        form = AdForm(request.POST, instance=ad)
        if form.is_valid():
            form.save()
            return redirect('ad_list')
    else:
        form = AdForm(instance=ad)
    return render(request, 'core/ad_form.html', {'form': form, 'action': 'Edit'})


@login_required
@user_passes_test(is_staff_or_superuser)
def ad_delete(request, ad_id):
    ad = get_object_or_404(Ad, id=ad_id)
    if request.method == 'POST':
        ad.delete()
        return redirect('ad_list')
    return render(request, 'core/object_delete_confirm.html', {
        'page_title': 'Delete Ad',
        'object_label': 'the ad',
        'object_name': ad.name,
        'cancel_url_name': 'ad_list',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def ad_toggle(request, ad_id):
    ad = get_object_or_404(Ad, id=ad_id)
    if request.method == 'POST':
        ad.is_active = not ad.is_active
        ad.save()
        return redirect('ad_list')
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_adsterra(request):
    """Admin page for the Adsterra direct-link ads.

    Manage the saved links (add, enable/disable, delete, reset counts) and the
    click threshold that triggers opening the next enabled link.
    """
    site_settings = SiteSettings.get_settings()

    links = list(AdsterraLink.objects.all())

    def render_page():
        return render(request, 'core/admin_adsterra.html', {
            'adsterra_form': AdsterraSettingsForm(instance=site_settings),
            'link_form': AdsterraLinkForm(),
            'links': links,
            'site_settings': site_settings,
        })

    if request.method == 'POST':
        action = (request.POST.get('action') or 'save_settings').strip().lower()

        if action == 'edit_link':
            link = get_object_or_404(AdsterraLink, id=request.POST.get('link_id'))
            link_form = AdsterraLinkForm(request.POST, instance=link)
            if link_form.is_valid():
                link_form.save()
                messages.success(request, '%s updated.' % (link.name or link.ad_type or 'Unit'))
                return redirect('ad_list')
            messages.error(request, 'Could not save that unit: %s' % '; '.join(
                '%s: %s' % (field, ', '.join(errs))
                for field, errs in link_form.errors.items()
            ))
            return redirect('ad_list')

        if action == 'add_link':
            link_form = AdsterraLinkForm(request.POST)
            if link_form.is_valid():
                link_form.save()
                messages.success(request, 'Adsterra unit added.')
                return redirect('ad_list')
            messages.error(request, 'Could not add that unit: %s' % '; '.join(
                '%s: %s' % (field, ', '.join(errs))
                for field, errs in link_form.errors.items()
            ))
            return redirect('ad_list')

        settings_form = AdsterraSettingsForm(request.POST, instance=site_settings)
        if settings_form.is_valid():
            settings_form.save()
            messages.success(request, 'Adsterra settings saved.')
            return redirect('ad_list')
        messages.error(request, 'Could not save Adsterra settings.')
        return redirect('ad_list')

    return render_page()


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def admin_adsterra_link_toggle(request, link_id):
    """Enable / disable a saved Adsterra link."""
    link = get_object_or_404(AdsterraLink, id=link_id)
    link.is_active = not link.is_active
    link.save(update_fields=['is_active', 'updated_at'])
    messages.success(request, 'Adsterra link %s.' % ('enabled' if link.is_active else 'disabled'))
    return redirect('ad_list')


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def admin_adsterra_link_delete(request, link_id):
    """Delete a saved Adsterra link."""
    link = get_object_or_404(AdsterraLink, id=link_id)
    link.delete()
    messages.success(request, 'Adsterra link deleted.')
    return redirect('ad_list')


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def admin_adsterra_link_reset(request, link_id):
    """Reset the click counter of a saved Adsterra link."""
    AdsterraLink.objects.filter(id=link_id).update(click_count=0)
    messages.success(request, 'Adsterra link click count reset.')
    return redirect('ad_list')


@csrf_exempt
@require_http_methods(["POST"])
def ajax_adsterra_click(request):
    """POST /ajax/adsterra/click/ — record that an Adsterra link was opened.

    Increments the click counter on the given link (or the next enabled link when
    no id is supplied) and returns the running total. The client decides (from the
    configured threshold) when to open a link; this endpoint just persists it.
    """
    try:
        ss = SiteSettings.get_settings()
        if int(ss.adsterra_clicks_required or 0) < 1:
            return JsonResponse({'success': False, 'error': 'adsterra_disabled'})

        link_id = request.POST.get('link_id')
        usable = AdsterraLink.objects.filter(is_active=True).exclude(url__isnull=True).exclude(url='')
        link = None
        if link_id:
            link = usable.filter(id=link_id).first()
        if link is None:
            link = usable.order_by('order', 'id').first()
        if link is None:
            return JsonResponse({'success': False, 'error': 'no_active_link'})

        AdsterraLink.objects.filter(pk=link.pk).update(click_count=F('click_count') + 1)
        link.refresh_from_db(fields=['click_count'])
        logger.info("ADSTERRA_OPEN link_id=%s total=%s url=%s", link.id, link.click_count, link.url)
        return JsonResponse({'success': True, 'link_id': link.id, 'click_count': link.click_count, 'url': link.url})
    except Exception as e:
        logger.error("adsterra click: %s", e)
        return JsonResponse({'success': False, 'error': str(e)})


@csrf_exempt
@require_http_methods(["POST"])
def track_user_click(request):
    """Track user clicks for ad targeting.

    Increments both:
      1. UserActivity.clicks_today — per-day per-user/ip DB counter (per-Ad clicks_required gating)
      2. request.session['ad_valid_clicks'] — per-session counter (for deferred ad loading threshold)
    Both are protected by server-side 1/s session throttling; clients also throttle.
    """
    user = request.user if request.user.is_authenticated else None
    ip_address = request.META.get('REMOTE_ADDR')
    today = timezone.localdate()
    now_ts = int(timezone.now().timestamp())

    try:
        last_click_ts = int(request.session.get('ad_last_click_ts', 0) or 0)
        session_clicks = int(request.session.get('ad_valid_clicks', 0) or 0)
        if now_ts - last_click_ts >= 1:
            session_clicks += 1
            request.session['ad_last_click_ts'] = now_ts
            request.session['ad_valid_clicks'] = session_clicks
            request.session.modified = True

        try:
            if user:
                user_activity, created = UserActivity.objects.get_or_create(
                    user=user,
                    activity_date=today,
                    defaults={'ip_address': ip_address}
                )
            else:
                user_activity, created = UserActivity.objects.get_or_create(
                    ip_address=ip_address,
                    activity_date=today,
                    defaults={'user': None}
                )
        except Exception:
            # Race condition: another request created the record between our
            # get and create. Fall back to fetching the existing record.
            if user:
                user_activity = UserActivity.objects.get(user=user, activity_date=today)
            else:
                user_activity = UserActivity.objects.get(ip_address=ip_address, activity_date=today)

        user_activity.clicks_today += 1
        user_activity.save(update_fields=['clicks_today'])
        return JsonResponse({'success': True, 'session_clicks': session_clicks})
    except Exception as e:
        logger.error("tracking user click: %s", e)
        return JsonResponse({'success': False, 'error': str(e)})


@require_http_methods(["GET"])
def ajax_click_status(request):
    """GET /ajax/click-status/ — return threshold state + deferred ad fragments (only when eligible).

    Returns ad HTML fragments ONLY when can_load_ads=True:
      - today_clicks >= effective_threshold (max(default_clicks_required, all per-Ad clicks_required))
      - consent given or not required
    Never returns ad HTML prematurely.
    """
    user = request.user if request.user.is_authenticated else None
    ip_address = request.META.get('REMOTE_ADDR')
    try:
        site_settings = SiteSettings.get_settings()
        default_req = max(0, int(getattr(site_settings, 'default_clicks_required', 0) or 0))
        consent_required = bool(getattr(site_settings, 'require_ad_consent', False))
        max_retries = max(0, int(getattr(site_settings, 'max_ad_load_retries', 3) or 0))

        session_clicks = int(request.session.get('ad_valid_clicks', 0) or 0)
        today_clicks = get_user_today_clicks(user, ip_address)

        all_active_ads = Ad.objects.filter(is_active=True)
        per_ad_req = [max(0, int(a.clicks_required or 0)) for a in all_active_ads]
        effective_threshold = default_req
        if per_ad_req:
            effective_threshold = max(effective_threshold, *per_ad_req)
        threshold_met = today_clicks >= effective_threshold

        consent_val = request.session.get('ad_consent')
        if consent_val == 'given':
            consent_given = True
        elif consent_val == 'denied':
            consent_given = False
        else:
            consent_given = not consent_required

        can_load_ads = threshold_met and ((not consent_required) or consent_given)

        slots = None
        if can_load_ads:
            eligible_ads = get_eligible_ads(user, ip_address)
            by_position = {}
            from django.template import engines
            django_engine = engines['django']
            try:
                ad_template = django_engine.get_template('core/_ad_content.html')
            except Exception:
                ad_template = django_engine.get_template('_ad_content.html')
            for ad in eligible_ads:
                html = ad_template.render({'ad': ad})
                by_position.setdefault(ad.position, []).append({
                    'id': ad.id,
                    'name': ad.name,
                    'provider': ad.provider,
                    'position': ad.position,
                    'html': html,
                })
            slots = by_position

        return JsonResponse({
            'success': True,
            'session_clicks': session_clicks,
            'today_clicks': today_clicks,
            'effective_threshold': effective_threshold,
            'threshold_met': threshold_met,
            'consent_required': consent_required,
            'consent_given': consent_given,
            'consent_denied': consent_val == 'denied',
            'consent_message': str(getattr(site_settings, 'ad_consent_message', '') or ''),
            'can_load_ads': can_load_ads,
            'max_retries': max_retries,
            'slots': slots,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@require_http_methods(["POST"])
def ajax_record_ad_event(request):
    """POST /ajax/record-ad-event/ — client-side ad pipeline event log for debugging/analytics.

    Fields: event (required), slot (optional), detail (optional).
    Ring buffer of last 200 events per session; printed server-side if DEBUG.
    """
    event = (request.POST.get('event') or '').strip()
    if not event:
        return JsonResponse({'success': False, 'error': 'missing_event'})

    slot = (request.POST.get('slot') or '').strip()[:200]
    detail = (request.POST.get('detail') or '').strip()[:500]
    now_ts = int(timezone.now().timestamp())

    try:
        log = request.session.get('ad_events_log') or []
        if not isinstance(log, list):
            log = []
        log.append({'t': now_ts, 'ev': event, 'slot': slot, 'detail': detail})
        if len(log) > 200:
            log = log[-200:]
        request.session['ad_events_log'] = log
        request.session.modified = True
        logger.debug("AD_EVENT t=%s ev=%s slot=%s detail=%s", now_ts, event, slot, detail)
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@require_http_methods(["POST"])
def ajax_give_ad_consent(request):
    """POST /ajax/ad-consent/ with action=give|deny."""
    action = (request.POST.get('action') or '').strip().lower()
    if action not in ('give', 'deny'):
        return JsonResponse({'success': False, 'error': 'invalid_action'})
    try:
        request.session['ad_consent'] = 'given' if action == 'give' else 'denied'
        request.session.modified = True
        return JsonResponse({'success': True, 'consent': request.session['ad_consent']})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


def _is_future_date(date_str):
    """Check if a date string is in the future."""
    if not date_str:
        return False
    try:
        from datetime import date as _date
        d = _date.fromisoformat(str(date_str)[:10])
        return d > timezone.localdate()
    except Exception:
        return False


def _hover_preview_html(rating, year, overview, title_html=''):
    """Netflix-style hover details block for card grids (title, rating pill, year, plot)."""
    import html
    meta = []
    if rating:
        try:
            meta.append('<b>{:.1f}</b>'.format(float(rating)))
        except (TypeError, ValueError):
            meta.append('<b>{}</b>'.format(html.escape(str(rating))))
    if year:
        meta.append('<span>{}</span>'.format(html.escape(str(year))))
    parts = ['<div class="card-hover-overlay"></div>', '<div class="card-hover-preview">']
    if title_html:
        parts.append('<div class="preview-title">' + title_html + '</div>')
    if meta:
        parts.append('<div class="preview-meta">' + ''.join(meta) + '</div>')
    if overview:
        ov = html.escape(str(overview).strip()[:170])
        if len(str(overview).strip()) > 170:
            ov += '&hellip;'
        parts.append('<p class="hover-description" style="display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;">' + ov + '</p>')
    parts.append('</div>')
    return ''.join(parts)


def _render_card_items(items, build_url, date_field):
    """Shared card renderer for AJAX load-more and catalog grids (deduplicated)."""
    import html as _html
    parts = []
    for item in items:
        date_value = item.get(date_field, '') or item.get('year', '')
        if _is_future_date(date_value):
            badge = '<div class="card-rating-badge" style="background:rgba(39,174,96,.85);color:#fff">Coming Soon</div>'
        else:
            badge = '<div class="card-rating-badge">{rating}</div>'.format(rating=item.get('vote_average', ''))
        title_esc = _html.escape(str(item.get('title', '')))
        preview = _hover_preview_html(item.get('vote_average'), item.get('year'), item.get('overview'), title_html=title_esc)
        parts.append(
            '<div class="card-wrapper"><a href="{url}" class="movie-card" title="{title}">'
            '<div class="card-image-container">'
            '<img class="card-image" src="{img}" alt="{title}" loading="lazy">'
            '{badge}'
            '{preview}'
            '</div></a></div>'.format(
                url=build_url(item),
                title=title_esc,
                img=item.get('cover_url', ''),
                badge=badge,
                preview=preview,
            )
        )
    return ''.join(parts)


def _render_movie_cards(movies):
    """Render movie cards HTML for AJAX load-more and catalog grids."""
    def build_url(m):
        return '/movies/{}/'.format(m.get('slug', ''))
    return _render_card_items(movies, build_url, 'release_date')


def _render_series_cards(series_list):
    """Render series cards HTML for AJAX load-more and catalog grids."""
    def build_url(s):
        return '/series/id/{}/'.format(s.get('id', ''))
    return _render_card_items(series_list, build_url, 'first_air_date')


@cache_control(public=True, max_age=300, stale_while_revalidate=3600)
def movie_list(request):
    client = get_data_client()
    search_client = TMDBClient()
    site_settings = SiteSettings.get_settings()

    search_query = request.GET.get('search', '')
    genre_id = request.GET.get('genre', '')
    sort_by = request.GET.get('sort', '')
    order = request.GET.get('order', 'desc')
    filter_type = request.GET.get('filter_type', '')
    provider = _resolve_catalog_provider(request)
    provider_slug = provider.slug if provider else ''
    provider_id = provider.tmdb_provider_id if provider else None
    requested_region = request.GET.get('region')
    default_region = (site_settings.watch_region or 'US').upper()
    watch_region = requested_region.strip().upper() if (requested_region is not None and requested_region.strip()) else default_region
    page = int(request.GET.get('page', 1))

    # Resolve genre name to ID (footer passes names like 'Action')
    genre_name_original = genre_id
    if genre_id and not genre_id.isdigit():
        genre_id = _resolve_genre_name_to_id(genre_id, 'movie')
    # Resolve country name to code (footer passes 'United States')
    country_name = request.GET.get('country', '')
    country_code = _resolve_country_name_to_code(country_name) if country_name else ''

    cache_key = f"movie_list_{search_query}_{genre_id}_{country_code}_{sort_by}_{order}_{filter_type}_{provider_slug}_{provider_id}_{watch_region}_{page}"
    cached_result = cache.get(cache_key)
    if cached_result:
        items, total_pages, all_genres = cached_result
    else:
        params = {'page': page}
        if genre_id:
            params['with_genres'] = genre_id
        if country_code:
            params['with_release_countries'] = country_code
        if provider_id:
            params['with_watch_providers'] = provider_id
            params['watch_region'] = watch_region or 'US'
        movie_sort_map = {
            'title': f"title.{order}",
            'year': f"primary_release_date.{order}",
            'rating': f"vote_average.{order}",
        }
        if sort_by in movie_sort_map:
            params['sort_by'] = movie_sort_map[sort_by]
        elif filter_type == 'top_rated':
            params['sort_by'] = 'vote_average.desc'
        elif filter_type == 'upcoming':
            params['sort_by'] = 'primary_release_date.asc'
        elif filter_type == 'popular':
            params['sort_by'] = 'popularity.desc'
        else:
            params['sort_by'] = 'primary_release_date.desc'

        if search_query:
            data = search_client.search_movies(search_query, page=page)
        elif genre_id or country_code or provider_id or sort_by or filter_type:
            data = client.discover_movies(params)
        else:
            data = client.get_now_playing_movies(page=page, params=params)

        items = [normalize_movie_item(item) for item in data.get('results', [])]

        if provider and not provider_id:
            items = [item for item in items if _provider_slug_matches(item, provider_slug)]

        total_pages = data.get('total_pages', 1)
        all_genres = cache.get('movie_genres')
        if not all_genres:
            try:
                all_genres = client.get_movie_genres().get('genres', [])
                cache.set('movie_genres', all_genres, 3600 * 24)
            except Exception as e:
                logger.error("fetching genres: %s", e)
                all_genres = []

        cache.set(cache_key, (items, total_pages, all_genres), 1800)

    has_next = page < total_pages
    base_col = 12 // site_settings.items_per_row
    col_class = f"col-{base_col} col-sm-{max(1, base_col-1)} col-md-{base_col} col-lg-{max(1, base_col-2)} col-xl-{max(1, base_col-3)}"
    image_heights = {'small': '200px', 'medium': '300px', 'large': '400px'}
    image_height = image_heights[site_settings.card_size]

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        html = _render_movie_cards(items)
        return JsonResponse({'html': html, 'has_next': has_next})
    return render(request, 'core/movie_list.html', {
        'movies': items,
        'search_query': search_query,
        'sort_by': sort_by,
        'order': order,
        'genre_id': genre_name_original,
        'filter_type': filter_type,
        'provider_slug': provider_slug,
        'active_provider': provider,
        'watch_region': watch_region,
        'watch_regions': WatchRegion.objects.filter(is_enabled=True),
        'catalog_providers': ProviderItem.objects.filter(is_enabled=True, supports_movies=True).order_by('display_priority')[:50],
        'all_genres': all_genres,
        'most_viewed': _get_most_viewed_items(items, 'movie'),
        'has_next': has_next,
        'col_class': col_class,
        'image_height': image_height
    })


@cache_control(public=True, max_age=300, stale_while_revalidate=3600)
def series_list(request):
    client = get_data_client()
    search_client = TMDBClient()
    site_settings = SiteSettings.get_settings()

    # Get filters
    search_query = request.GET.get('search', '')
    genre_id = request.GET.get('genre', '')
    sort_by = request.GET.get('sort', '')
    order = request.GET.get('order', 'desc')
    filter_type = request.GET.get('filter_type', '')
    provider = _resolve_catalog_provider(request)
    provider_slug = provider.slug if provider else ''
    provider_id = provider.tmdb_provider_id if provider else None
    requested_region = request.GET.get('region')
    default_region = (site_settings.watch_region or 'US').upper()
    watch_region = requested_region.strip().upper() if (requested_region is not None and requested_region.strip()) else default_region
    page = int(request.GET.get('page', 1))

    # Resolve genre name to ID (footer passes names like 'Action')
    genre_name_original = genre_id
    if genre_id and not genre_id.isdigit():
        genre_id = _resolve_genre_name_to_id(genre_id, 'tv')
    # Resolve country name to code
    country_name = request.GET.get('country', '')
    country_code = _resolve_country_name_to_code(country_name) if country_name else ''

    # Create cache key based on all filters and page
    cache_key = f"series_list_{search_query}_{genre_id}_{country_code}_{sort_by}_{order}_{filter_type}_{provider_slug}_{provider_id}_{watch_region}_{page}"
    cached_result = cache.get(cache_key)
    if cached_result:
        items, total_pages, all_genres = cached_result
    else:
        params = {'page': page}

        if genre_id:
            params['with_genres'] = genre_id
        if country_code:
            params['with_origin_country'] = country_code
        if provider_id:
            params['with_watch_providers'] = provider_id
            params['watch_region'] = watch_region or 'US'
        series_sort_map = {
            'title': f"name.{order}",
            'year': f"first_air_date.{order}",
            'rating': f"vote_average.{order}",
        }
        if sort_by in series_sort_map:
            params['sort_by'] = series_sort_map[sort_by]
        elif filter_type == 'top_rated':
            params['sort_by'] = 'vote_average.desc'
        elif filter_type in ('airing_today', 'on_the_air'):
            params['sort_by'] = 'first_air_date.desc'
        else:
            params['sort_by'] = 'popularity.desc'

        if search_query:
            data = search_client.search_series(search_query, page=page)
        elif genre_id or country_code or provider_id or sort_by or filter_type:
            data = client.discover_series(params)
        else:
            data = client.get_on_the_air_series(page=page, params=params)

        # Process results
        items = [normalize_series_item(item) for item in data.get('results', [])]

        if provider and not provider_id:
            items = [item for item in items if _provider_slug_matches(item, provider_slug)]

        total_pages = data.get('total_pages', 1)

        # Get all series genres from API (cached)
        all_genres = cache.get('series_genres')
        if not all_genres:
            try:
                all_genres = client.get_series_genres().get('genres', [])
                cache.set('series_genres', all_genres, 3600 * 24)  # Cache for 24 hours
            except Exception as e:
                logger.error("fetching genres: %s", e)
                all_genres = []

        # Cache the series list results for 30 minutes
        cache.set(cache_key, (items, total_pages, all_genres), 1800)
    
    has_next = page < total_pages

    # Calculate bootstrap column class with responsive breakpoints
    base_col = 12 // site_settings.items_per_row
    col_class = f"col-{base_col} col-sm-{max(1, base_col-1)} col-md-{base_col} col-lg-{max(1, base_col-2)} col-xl-{max(1, base_col-3)}"
    image_heights = {
        'small': '200px',
        'medium': '300px',
        'large': '400px'
    }
    image_height = image_heights[site_settings.card_size]

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        html = _render_series_cards(items)
        return JsonResponse({'html': html, 'has_next': has_next})
    return render(request, 'core/series_list.html', {
        'series': items,
        'search_query': search_query,
        'sort_by': sort_by,
        'order': order,
        'genre_id': genre_name_original,
        'filter_type': filter_type,
        'provider_slug': provider_slug,
        'active_provider': provider,
        'watch_region': watch_region,
        'watch_regions': WatchRegion.objects.filter(is_enabled=True),
        'catalog_providers': ProviderItem.objects.filter(is_enabled=True, supports_tv=True).order_by('display_priority')[:50],
        'all_genres': all_genres,
        'most_viewed': _get_most_viewed_items(items, 'tv'),
        'has_next': has_next,
        'col_class': col_class,
        'image_height': image_height
    })


def load_more_row_content(request, row_id):
    """AJAX view to load more content for a specific ContentRow"""
    row = get_object_or_404(ContentRow, id=row_id)
    page = int(request.GET.get('page', 1))

    items, total_pages = get_content_row_items(row, page)
    has_next = page < total_pages

    # Render the items
    if row.media_type == 'movie':
        html = _render_movie_cards(items)
    else:
        html = _render_series_cards(items)

    return JsonResponse({
        'html': html,
        'has_next': has_next,
        'next_page': page + 1
    })


def _get_best_watch_region(watch_providers, preferred_region):
    """Find a region with available providers, falling back from the preferred region."""
    if not watch_providers or not isinstance(watch_providers, dict):
        return preferred_region
    provider_keys = ['flatrate', 'free', 'ads', 'rent', 'buy']
    region_data = watch_providers.get(preferred_region, {})
    if isinstance(region_data, dict) and any(region_data.get(k) for k in provider_keys):
        return preferred_region
    for fallback in ['US', 'GB', 'IN', 'DE', 'CA', 'AU']:
        fb_data = watch_providers.get(fallback, {})
        if isinstance(fb_data, dict) and any(fb_data.get(k) for k in provider_keys):
            return fallback
    return preferred_region


def _fetch_tmdb_extra(movie_id, media_type='movie'):
    """Fetch credits, videos, collection from TMDB API for a movie."""
    import time as _time
    result = {'cast': [], 'crew': [], 'directors': '', 'trailers': [], 'collection': None, 'collection_movies': [],
              'backdrops': [], 'posters': []}
    try:
        api_key = TMDBApiKey.objects.filter(is_active=True).first()
        if not api_key:
            return result
        # Use a fresh session (don't reuse client's cached session which may have stale connections)
        session = requests.Session()
        session.headers.update({'Accept': 'application/json'})
        data = None
        for attempt in range(3):
            try:
                resp = session.get(
                    f'https://api.themoviedb.org/3/{media_type}/{movie_id}',
                    params={'api_key': api_key.key, 'append_to_response': 'credits,videos,images',
                            'include_image_language': 'en,null'},
                    timeout=(5, 15)
                )
                if resp.status_code == 429:
                    _time.sleep(2 + attempt * 2)
                    continue
                if resp.status_code != 200:
                    return result
                data = resp.json()
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt < 2:
                    _time.sleep(2 + attempt * 2)
                    try:
                        session.close()
                    except Exception:
                        pass
                continue
        if not data:
            return result
        # Media gallery — every backdrop & poster TMDB has, in original size.
        # TMDB returns them sorted by quality score; we keep the order.
        images = data.get('images', {}) or {}
        result['backdrops'] = [b.get('file_path', '') for b in images.get('backdrops', []) if b.get('file_path')]
        result['posters'] = [p.get('file_path', '') for p in images.get('posters', []) if p.get('file_path')]
        # Cast (with photos)
        credits = data.get('credits', {})
        raw_cast = credits.get('cast', [])[:20]
        result['cast'] = [{'name': c.get('name', ''), 'character': c.get('character', ''), 'profile_path': c.get('profile_path', ''), 'id': c.get('id', 0)} for c in raw_cast]
        # Crew (directors, writers, etc.)
        raw_crew = credits.get('crew', [])
        result['directors'] = ', '.join(c['name'] for c in raw_crew if c.get('job') == 'Director')
        result['crew'] = [{'name': c.get('name', ''), 'job': c.get('job', ''), 'profile_path': c.get('profile_path', ''), 'id': c.get('id', 0)} for c in raw_crew if c.get('profile_path')]
        # Trailers / videos
        videos = data.get('videos', {}).get('results', [])
        trailers = [v for v in videos if v.get('type') == 'Trailer' and v.get('site') == 'YouTube']
        if not trailers:
            trailers = [v for v in videos if v.get('site') == 'YouTube']
        result['trailers'] = [{'key': v.get('key', ''), 'name': v.get('name', ''), 'type': v.get('type', '')} for v in trailers[:5]]
        # Collection
        collection_data = data.get('belongs_to_collection')
        if collection_data and collection_data.get('id'):
            result['collection'] = {'id': collection_data['id'], 'name': collection_data.get('name', ''), 'poster_path': collection_data.get('poster_path', ''), 'backdrop_path': collection_data.get('backdrop_path', '')}
            # Fetch collection parts
            try:
                col_resp = session.get(
                    f'https://api.themoviedb.org/3/collection/{collection_data["id"]}',
                    params={'api_key': api_key.key},
                    timeout=(5, 15)
                )
                if col_resp.status_code == 200:
                    col_data = col_resp.json()
                    parts = col_data.get('parts', [])
                    result['collection_movies'] = [{'id': p.get('id'), 'title': p.get('title', ''), 'poster_path': p.get('poster_path', ''), 'release_date': p.get('release_date', ''), 'vote_average': p.get('vote_average', 0)} for p in parts]
            except Exception:
                pass
    except Exception as e:
        logger.warning('Could not fetch TMDB extra for %s: %s', movie_id, e)
    finally:
        try:
            session.close()
        except Exception:
            pass
    return result


@require_GET
def tmdb_extra_ajax(request):
    """Lightweight AJAX endpoint for client-side lazy fetching of cast, crew, trailers, collection."""
    media_type = request.GET.get('media_type', 'movie')
    tmdb_id = request.GET.get('tmdb_id', '')
    if not tmdb_id:
        return JsonResponse({'error': 'tmdb_id required'}, status=400)
    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid tmdb_id'}, status=400)
    cache_key = f'tmdb_extra_ajax_v2_{media_type}_{tmdb_id}'
    cached = cache.get(cache_key)
    if cached:
        return JsonResponse(cached)
    result = _fetch_tmdb_extra(tmdb_id, media_type)
    cache.set(cache_key, result, 1800)
    return JsonResponse(result)


@cache_control(private=True, max_age=0, must_revalidate=True)
def movie_detail_by_id(request, movie_id):
    client = get_data_client()
    site_settings = SiteSettings.get_settings()

    cache_key = f"movie_detail_v2_{movie_id}"
    cached_result = cache.get(cache_key)
    if cached_result:
        processed_movie, more_movies, watch_providers, tmdb_extra = cached_result
    else:
        movie = client.get_movie_details(movie_id)
        if not movie:
            return render(request, '404.html', status=404)

        processed_movie = normalize_movie_detail(movie)
        processed_movie.setdefault('directors', '')
        processed_movie.setdefault('cast', '')

        # Client JS fetches cast/crew/trailers lazily via /ajax/tmdb-extra/
        tmdb_extra = {'cast': [], 'crew': [], 'directors': '', 'trailers': [], 'collection': None, 'collection_movies': []}

        more_movies = []
        try:
            similar_data = client.get_similar_movies(movie_id, page=1)
            more_movies = [normalize_movie_item(item) for item in similar_data.get('results', [])[:12]]
        except Exception as e:
            logger.error("fetching similar movies: %s", e)

        watch_providers = None
        try:
            providers_data = client.get_movie_watch_providers(movie_id)
            watch_providers = providers_data.get('results', {})
        except Exception as e:
            logger.error("fetching movie watch providers: %s", e)

        cache.set(cache_key, (processed_movie, more_movies, watch_providers, tmdb_extra), 3600)

    if site_settings.url_format == 'slug':
        return redirect('movie_detail', movie_slug=processed_movie['slug'])

    provider_urls = {
        provider.name.lower(): provider.url
        for provider in ProviderItem.objects.exclude(url__isnull=True).exclude(url='')
    }

    active_player = site_settings.active_movie_player
    all_players = _get_detail_players('movie')
    # Include CinePlayer config if enabled
    try:
        from core.models import CinePlayerConfig
        _cp_cfg = CinePlayerConfig.get_config()
        cineplayer_enabled = _cp_cfg.enabled
    except Exception:
        cineplayer_enabled = False

    return render(request, 'core/movie_detail.html', {
        'movie': processed_movie,
        'more_movies': more_movies,
        'watch_providers': watch_providers,
        'watch_region': _get_best_watch_region(watch_providers, site_settings.watch_region or 'US'),
        'active_player': active_player,
        'all_players': all_players,
        'movie_id': movie_id,
        'provider_urls': provider_urls,
        'cast_list': tmdb_extra.get('cast', []),
        'crew_list': tmdb_extra.get('crew', []),
        'trailers': tmdb_extra.get('trailers', []),
        'collection': tmdb_extra.get('collection'),
        'collection_movies': tmdb_extra.get('collection_movies', []),
        'cineplayer_enabled': cineplayer_enabled,
    })

@cache_control(private=True, max_age=0, must_revalidate=True)
def movie_detail(request, movie_slug):
    client = get_data_client()
    site_settings = SiteSettings.get_settings()
    
    # Check cache for movie detail first
    cache_key = f"movie_detail_{movie_slug}"
    cached_result = cache.get(cache_key)
    if cached_result:
        processed_movie, more_movies, movie_id, watch_providers = cached_result
    else:
        # Convert slug back to a search query (replace hyphens with spaces)
        search_query = movie_slug.replace('-', ' ')
        
        # Search for movies by name
        search_results = TMDBClient().search_movies(search_query)
        
        # Find the best matching movie
        movie_id = None
        movie = None
        if search_results.get('results'):
            selected_result = next(
                (
                    result for result in search_results['results']
                    if slugify(result.get('title', '')) == movie_slug
                ),
                search_results['results'][0]
            )
            movie_id = selected_result['id']
            movie = client.get_movie_details(movie_id)
        
        if not movie:
            # If no movie found, 404
            return render(request, '404.html', status=404)

        # Process movie data for template
        processed_movie = normalize_movie_detail(movie)

        # Get similar movies
        more_movies = []
        try:
            similar_data = client.get_similar_movies(movie_id, page=1)
            more_movies = [normalize_movie_item(item) for item in similar_data.get('results', [])[:12]]
        except Exception as e:
            logger.error("fetching similar movies: %s", e)

        # Get watch providers
        watch_providers = None
        try:
            providers_data = client.get_movie_watch_providers(movie_id)
            watch_providers = providers_data.get('results', {})
        except Exception as e:
            logger.error("fetching movie watch providers: %s", e)
        
        # Cache for 1 hour
        cache.set(cache_key, (processed_movie, more_movies, movie_id, watch_providers), 3600)

    if site_settings.url_format == 'id' and movie_id is not None:
        return redirect('movie_detail_by_id', movie_id=movie_id)

    # Client JS fetches cast/crew/trailers lazily via /ajax/tmdb-extra/
    tmdb_extra = {'cast': [], 'crew': [], 'directors': '', 'trailers': [], 'collection': None, 'collection_movies': []}

    provider_urls = {
        provider.name.lower(): provider.url
        for provider in ProviderItem.objects.exclude(url__isnull=True).exclude(url='')
    }

    active_player = site_settings.active_movie_player
    all_players = _get_detail_players('movie')
    try:
        from core.models import CinePlayerConfig
        _cp_cfg2 = CinePlayerConfig.get_config()
        cineplayer_enabled = _cp_cfg2.enabled
    except Exception:
        cineplayer_enabled = False
    return render(request, 'core/movie_detail.html', {
        'movie': processed_movie,
        'more_movies': more_movies,
        'watch_providers': watch_providers,
        'watch_region': _get_best_watch_region(watch_providers, site_settings.watch_region or 'US'),
        'active_player': active_player,
        'all_players': all_players,
        'movie_id': movie_id,
        'provider_urls': provider_urls,
        'cast_list': tmdb_extra.get('cast', []),
        'crew_list': tmdb_extra.get('crew', []),
        'trailers': tmdb_extra.get('trailers', []),
        'collection': tmdb_extra.get('collection'),
        'collection_movies': tmdb_extra.get('collection_movies', []),
        'cineplayer_enabled': cineplayer_enabled,
    })


@cache_control(private=True, max_age=0, must_revalidate=True)
def series_detail_by_id(request, series_id):
    client = get_data_client()
    site_settings = SiteSettings.get_settings()

    season_number = int(request.GET.get('season', 1))
    episode_number = int(request.GET.get('episode', 1))

    cache_key = f"series_detail_v2_{series_id}_{season_number}"
    cached_result = cache.get(cache_key)
    if cached_result:
        processed_series, seasons, episodes, more_series, watch_providers, tmdb_extra = cached_result
    else:
        series_details = client.get_series_details(series_id)
        if not series_details:
            return render(request, '404.html', status=404)

        processed_series = normalize_series_detail(series_details)

        # Client JS fetches cast/crew/trailers lazily via /ajax/tmdb-extra/
        tmdb_extra = {'cast': [], 'crew': [], 'directors': '', 'trailers': [], 'collection': None, 'collection_movies': []}

        seasons = series_details.get('seasons', [])
        if not seasons and series_details.get('number_of_seasons'):
            seasons = [{'season_number': n, 'name': f'Season {n}'} for n in range(1, int(series_details.get('number_of_seasons', 0)) + 1)]
        episodes = []
        try:
            if season_number > 0:
                season_details = client.get_season_details(series_id, season_number)
                episodes = season_details.get('episodes', [])
        except Exception as e:
            logger.error("fetching season details: %s", e)

        more_series = []
        try:
            similar_data = client.get_similar_series(series_id, page=1)
            more_series = [normalize_series_item(item) for item in similar_data.get('results', [])[:12]]
        except Exception as e:
            logger.error("fetching similar series: %s", e)

        watch_providers = None
        try:
            providers_data = client.get_series_watch_providers(series_id)
            watch_providers = providers_data.get('results', {})
        except Exception as e:
            logger.error("fetching series watch providers: %s", e)

        cache.set(cache_key, (processed_series, seasons, episodes, more_series, watch_providers, tmdb_extra), 3600)

    if site_settings.url_format == 'slug':
        return redirect('series_detail', series_slug=processed_series['slug'])

    provider_urls = {
        provider.name.lower(): provider.url
        for provider in ProviderItem.objects.exclude(url__isnull=True).exclude(url='')
    }

    active_player = site_settings.active_tv_player
    all_players = _get_detail_players('tv')
    try:
        from core.models import CinePlayerConfig
        _cp_cfg3 = CinePlayerConfig.get_config()
        cineplayer_enabled = _cp_cfg3.enabled
    except Exception:
        cineplayer_enabled = False
    return render(request, 'core/series_detail.html', {
        'series': processed_series,
        'seasons': seasons,
        'episodes': episodes,
        'current_season': season_number,
        'current_episode': episode_number,
        'more_series': more_series,
        'watch_providers': watch_providers,
        'watch_region': _get_best_watch_region(watch_providers, site_settings.watch_region or 'US'),
        'active_player': active_player,
        'all_players': all_players,
        'series_id': series_id,
        'provider_urls': provider_urls,
        'cast_list': tmdb_extra.get('cast', []),
        'crew_list': tmdb_extra.get('crew', []),
        'trailers': tmdb_extra.get('trailers', []),
        'cineplayer_enabled': cineplayer_enabled,
    })

def series_season_episodes(request, series_id, season_number):
    client = get_data_client()

    try:
        season_details = client.get_season_details(series_id, season_number)
        return JsonResponse({'episodes': season_details.get('episodes', [])})
    except Exception as e:
        return JsonResponse({'episodes': [], 'error': str(e)}, status=500)


@cache_control(private=True, max_age=0, must_revalidate=True)
def series_detail(request, series_slug):
    client = get_data_client()
    site_settings = SiteSettings.get_settings()
    
    # Get season and episode from request, default to 1
    season_number = int(request.GET.get('season', 1))
    episode_number = int(request.GET.get('episode', 1))
    
    # Check cache for series detail with season
    cache_key = f"series_detail_{series_slug}_{season_number}"
    cached_result = cache.get(cache_key)
    if cached_result:
        processed_series, seasons, episodes, more_series, series_id, watch_providers = cached_result
    else:
        # Convert slug back to a search query (replace hyphens with spaces)
        search_query = series_slug.replace('-', ' ')
        
        # Search for series by name
        search_results = TMDBClient().search_series(search_query)
        
        # Find the best matching series
        series_id = None
        series_details = None
        if search_results.get('results'):
            selected_result = next(
                (
                    result for result in search_results['results']
                    if slugify(result.get('name', '')) == series_slug
                ),
                search_results['results'][0]
            )
            series_id = selected_result['id']
            series_details = client.get_series_details(series_id)
        
        if not series_details:
            return render(request, '404.html', status=404)

        # Process series data
        processed_series = normalize_series_detail(series_details)

        # Fetch credits (created by, cast) from TMDB API
        try:
            from core.models import TMDBApiKey
            api_key = TMDBApiKey.objects.filter(is_active=True).first()
            if api_key:
                import requests as _req
                cred_resp = _req.get(
                    f'https://api.themoviedb.org/3/tv/{series_id}',
                    params={'api_key': api_key.key, 'append_to_response': 'credits'},
                    timeout=10
                )
                if cred_resp.status_code == 200:
                    cred_data = cred_resp.json()
                    credits = cred_data.get('credits', {})
                    crew = credits.get('crew', [])
                    cast_list = credits.get('cast', [])
                    creators = [c['name'] for c in crew if c.get('job') == 'Creator' or c.get('known_for_department') == 'Writing']
                    cast_names = [c['name'] for c in cast_list[:8]]
                    processed_series['directors'] = ', '.join(creators[:3]) if creators else ''
                    processed_series['cast'] = ', '.join(cast_names) if cast_names else ''
        except Exception as e:
            logger.debug('Could not fetch series credits: %s', e)
            processed_series['directors'] = ''
            processed_series['cast'] = ''

        seasons = series_details.get('seasons', [])
        if not seasons and series_details.get('number_of_seasons'):
            seasons = [
                {
                    'season_number': n,
                    'name': f'Season {n}'
                }
                for n in range(1, int(series_details.get('number_of_seasons', 0)) + 1)
            ]
        episodes = []

        try:
            if season_number > 0:
                season_details = client.get_season_details(series_id, season_number)
                episodes = season_details.get('episodes', [])
        except Exception as e:
            logger.error("fetching season details: %s", e)

        # Get similar series
        more_series = []
        try:
            similar_data = client.get_similar_series(series_id, page=1)
            more_series = [normalize_series_item(item) for item in similar_data.get('results', [])[:12]]
        except Exception as e:
            logger.error("fetching similar series: %s", e)

        # Get watch providers
        watch_providers = None
        try:
            providers_data = client.get_series_watch_providers(series_id)
            watch_providers = providers_data.get('results', {})
        except Exception as e:
            logger.error("fetching series watch providers: %s", e)
        
        # Cache for 1 hour
        cache.set(cache_key, (processed_series, seasons, episodes, more_series, series_id, watch_providers), 3600)

    # Season and episode are managed client-side, never in the URL

    # Client JS fetches cast/crew/trailers lazily via /ajax/tmdb-extra/
    tmdb_extra = {'cast': [], 'crew': [], 'directors': '', 'trailers': [], 'collection': None, 'collection_movies': []}

    provider_urls = {
        provider.name.lower(): provider.url
        for provider in ProviderItem.objects.exclude(url__isnull=True).exclude(url='')
    }

    active_player = site_settings.active_tv_player
    all_players = _get_detail_players('tv')
    try:
        from core.models import CinePlayerConfig
        _cp_cfg4 = CinePlayerConfig.get_config()
        cineplayer_enabled = _cp_cfg4.enabled
    except Exception:
        cineplayer_enabled = False
    return render(request, 'core/series_detail.html', {
        'series': processed_series,
        'seasons': seasons,
        'episodes': episodes,
        'current_season': season_number,
        'current_episode': episode_number,
        'more_series': more_series,
        'watch_providers': watch_providers,
        'watch_region': _get_best_watch_region(watch_providers, site_settings.watch_region or 'US'),
        'active_player': active_player,
        'all_players': all_players,
        'series_id': series_id,
        'provider_urls': provider_urls,
        'cast_list': tmdb_extra.get('cast', []),
        'crew_list': tmdb_extra.get('crew', []),
        'trailers': tmdb_extra.get('trailers', []),
        'cineplayer_enabled': cineplayer_enabled,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def edit_settings(request):
    """Old /edit-settings/ URL: the General panel, opened on its own.

    The all-fields form that used to live here is gone — it could save values
    that eight other settings pages also own, so the last screen to be opened
    silently won. General keeps only the fields nothing else looks after.
    """
    return general_settings(request)


def _settings_field_sections(form, sections):
    """Group a bound form's fields under the titles a section map gives.

    A field the map does not mention still renders, under "Other", so adding a
    field to a form can never make it vanish from the page.
    """
    if not sections:
        return [{'title': '', 'hint': '', 'fields': list(form)}]
    grouped, used = [], set()
    for title, hint, names in sections:
        fields = [form[name] for name in names if name in form.fields]
        if fields:
            grouped.append({'title': title, 'hint': hint, 'fields': fields})
        used.update(names)
    rest = [form[name] for name in form.fields if name not in used]
    if rest:
        grouped.append({'title': 'Other', 'hint': '', 'fields': rest})
    return grouped


@login_required
@user_passes_test(is_staff_or_superuser)
@cache_control(private=True, no_store=True)
def _settings_section_view(request, form_class, title, description='', back_url='admin_dashboard',
                           sections=None):
    """Shared handler for the single-section admin settings pages (deduplicated)."""
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = form_class(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return _settings_redirect(request, back_url)
    else:
        form = form_class(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'sections': _settings_field_sections(form, sections),
        'title': title,
        'description': description,
        'back_url': back_url,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def general_settings(request):
    return _settings_section_view(
        request, GeneralSettingsForm, 'General',
        description='Site identity, measurement IDs, Google sign-in and how ads behave.',
        sections=GENERAL_FIELD_SECTIONS,
    )


@login_required
@user_passes_test(is_staff_or_superuser)
def security_protection_settings(request):
    return _settings_section_view(
        request, SecurityProtectionForm, 'Protection',
        description='Addresses that are never blocked, bot signatures, and browser protection.',
        sections=SECURITY_FIELD_SECTIONS,
    )


@login_required
@user_passes_test(is_staff_or_superuser)
def branding_settings(request):
    return _settings_section_view(request, BrandingSettingsForm, 'Branding Settings')


@login_required
@user_passes_test(is_staff_or_superuser)
def display_settings(request):
    return _settings_section_view(request, DisplaySettingsForm, 'Display Settings')


@login_required
@user_passes_test(is_staff_or_superuser)
def data_source_settings(request):
    return _settings_section_view(request, DataSourceSettingsForm, 'Data Source Settings')


@login_required
@user_passes_test(is_staff_or_superuser)
def tmdb_db_settings(request):
    return _settings_section_view(request, TMDBDBSettingsForm, 'TMDB Database Settings')


@login_required
@user_passes_test(is_staff_or_superuser)
def player_settings(request):
    return _settings_section_view(request, PlayerSettingsForm, 'Player Settings')


@login_required
@user_passes_test(is_staff_or_superuser)
def url_blocking_settings(request):
    return _settings_section_view(request, URLBlockingSettingsForm, 'URL Blocking Settings')


def _safe_smtp_port(raw_value, default=587):
    """Parse an SMTP port from POST data; fall back to default on empty/garbage."""
    try:
        port = int(str(raw_value or '').strip() or default)
    except (TypeError, ValueError):
        return default
    if port < 1 or port > 65535:
        return default
    return port



@login_required
@user_passes_test(is_staff_or_superuser)
def email_settings(request):
    """Email management page — SMTP addresses, purpose assignment, and templates."""
    from .models import EmailAddress, EmailTemplate, PURPOSE_CHOICES

    # Handle AJAX actions
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            action = request.POST.get('action') or request.GET.get('action')

            # Add / Edit email address
            if action == 'save_email':
                eid = request.POST.get('id')
                email_obj = EmailAddress.objects.filter(pk=eid).first() if eid else None
                new_email = request.POST.get('email', '').strip()
                if not new_email:
                    return JsonResponse({'success': False, 'message': 'Email address is required.'}, status=400)
                try:
                    if email_obj:
                        # Check uniqueness against other records
                        if EmailAddress.objects.filter(email=new_email).exclude(pk=email_obj.pk).exists():
                            return JsonResponse({'success': False, 'message': 'This email address is already in use.'}, status=400)
                        email_obj.email = new_email
                        email_obj.display_name = request.POST.get('display_name', '')
                        email_obj.smtp_host = request.POST.get('smtp_host', 'smtp.gmail.com')
                        email_obj.smtp_port = _safe_smtp_port(request.POST.get('smtp_port', 587))
                        email_obj.smtp_username = request.POST.get('smtp_username', '')
                        pwd = request.POST.get('smtp_password', '')
                        if pwd:
                            email_obj.smtp_password = pwd
                        email_obj.smtp_use_tls = request.POST.get('smtp_use_tls') == 'on'
                        email_obj.purpose = request.POST.get('purpose', 'notification')
                        email_obj.is_active = request.POST.get('is_active') == 'on'
                        email_obj.is_default = request.POST.get('is_default') == 'on'
                        email_obj.save()
                    else:
                        if EmailAddress.objects.filter(email=new_email).exists():
                            return JsonResponse({'success': False, 'message': 'This email address is already in use.'}, status=400)
                        email_obj = EmailAddress.objects.create(
                            email=new_email,
                            display_name=request.POST.get('display_name', ''),
                            smtp_host=request.POST.get('smtp_host', 'smtp.gmail.com'),
                            smtp_port=_safe_smtp_port(request.POST.get('smtp_port', 587)),
                            smtp_username=request.POST.get('smtp_username', ''),
                            smtp_password=request.POST.get('smtp_password', ''),
                            smtp_use_tls=request.POST.get('smtp_use_tls') == 'on',
                            purpose=request.POST.get('purpose', 'notification'),
                            is_active=request.POST.get('is_active') == 'on',
                            is_default=request.POST.get('is_default') == 'on',
                        )
                except Exception as e:
                    return JsonResponse({'success': False, 'message': f'Save failed: {str(e)}'}, status=500)
                return JsonResponse({'success': True, 'id': email_obj.id})

            # Delete email address
            if action == 'delete_email':
                eid = request.POST.get('id')
                EmailAddress.objects.filter(pk=eid).delete()
                return JsonResponse({'success': True})

            # Toggle email active
            if action == 'toggle_email':
                eid = request.POST.get('id')
                obj = EmailAddress.objects.filter(pk=eid).first()
                if obj:
                    obj.is_active = not obj.is_active
                    obj.save(update_fields=['is_active'])
                return JsonResponse({'success': True, 'is_active': obj.is_active if obj else False})

            # Save template
            if action == 'save_template':
                tid = request.POST.get('id')
                tmpl = EmailTemplate.objects.filter(pk=tid).first() if tid else None
                if tmpl:
                    tmpl.name = request.POST.get('name', tmpl.name)
                    tmpl.purpose = request.POST.get('purpose', tmpl.purpose)
                    tmpl.subject = request.POST.get('subject', tmpl.subject)
                    tmpl.body = request.POST.get('body', tmpl.body)
                    tmpl.is_active = request.POST.get('is_active') == 'on'
                    tmpl.save()
                else:
                    tmpl = EmailTemplate.objects.create(
                        name=request.POST.get('name', ''),
                        purpose=request.POST.get('purpose', 'notification'),
                        subject=request.POST.get('subject', ''),
                        body=request.POST.get('body', ''),
                        is_active=request.POST.get('is_active') == 'on',
                    )
                return JsonResponse({'success': True, 'id': tmpl.id})

            # Delete template
            if action == 'delete_template':
                tid = request.POST.get('id')
                EmailTemplate.objects.filter(pk=tid).delete()
                return JsonResponse({'success': True})

            # Test SMTP connection
            if action == 'test_smtp':
                import smtplib
                from email.mime.text import MIMEText
                host = request.POST.get('smtp_host', '').strip()
                port = _safe_smtp_port(request.POST.get('smtp_port', 587))
                username = request.POST.get('smtp_username', '').strip()
                password = request.POST.get('smtp_password', '').strip()

                use_tls = request.POST.get('smtp_use_tls') == 'on'
                test_email = request.POST.get('email', '').strip()
                if not host or not test_email:
                    return JsonResponse({'success': False, 'message': 'Email and SMTP host are required for testing.'})
                if not username:
                    username = test_email
                try:
                    msg = MIMEText('This is a test email from your NewMovies email settings.\n\nIf you see this, SMTP is working correctly!')
                    msg['Subject'] = 'NewMovies SMTP Test'
                    msg['From'] = f'{request.POST.get("display_name", "NewMovies")} <{test_email}>'
                    msg['To'] = test_email
                    server = smtplib.SMTP(host, port, timeout=10)
                    server.ehlo()
                    if use_tls:
                        server.starttls()
                        server.ehlo()
                    server.login(username, password)
                    server.sendmail(test_email, [test_email], msg.as_string())
                    server.quit()
                    return JsonResponse({'success': True, 'message': f'Test email sent successfully to {test_email}! Check your inbox.'})
                except smtplib.SMTPAuthenticationError as e:
                    return JsonResponse({'success': False, 'message': f'Authentication failed: {str(e)}. Check your email and password/app password.'})
                except smtplib.SMTPConnectError as e:
                    return JsonResponse({'success': False, 'message': f'Cannot connect to {host}:{port}. Check host and port.'})
                except smtplib.SMTPException as e:
                    return JsonResponse({'success': False, 'message': f'SMTP error: {str(e)}'})
                except Exception as e:
                    return JsonResponse({'success': False, 'message': f'Connection failed: {str(e)}'})

            # Test saved SMTP address
            if action == 'test_saved_smtp':
                import smtplib
                from email.mime.text import MIMEText
                eid = request.POST.get('id')
                addr_obj = EmailAddress.objects.filter(pk=eid).first()
                if not addr_obj:
                    return JsonResponse({'success': False, 'message': 'Email address not found.'})
                try:
                    msg = MIMEText('This is a test email from your NewMovies email settings.\n\nIf you see this, SMTP is working correctly!')
                    msg['Subject'] = 'NewMovies SMTP Test'
                    msg['From'] = f'{addr_obj.display_name or "NewMovies"} <{addr_obj.email}>'
                    msg['To'] = addr_obj.email
                    server = smtplib.SMTP(addr_obj.smtp_host, addr_obj.smtp_port, timeout=10)
                    server.ehlo()
                    if addr_obj.smtp_use_tls:
                        server.starttls()
                        server.ehlo()
                    server.login(addr_obj.smtp_username or addr_obj.email, addr_obj.smtp_password)
                    server.sendmail(addr_obj.email, [addr_obj.email], msg.as_string())
                    server.quit()
                    return JsonResponse({'success': True, 'message': f'Test email sent successfully to {addr_obj.email}! Check your inbox.'})
                except smtplib.SMTPAuthenticationError as e:
                    return JsonResponse({'success': False, 'message': f'Authentication failed: {str(e)}. Check your email and password/app password.'})
                except smtplib.SMTPConnectError as e:
                    return JsonResponse({'success': False, 'message': f'Cannot connect to {addr_obj.smtp_host}:{addr_obj.smtp_port}. Check host and port.'})
                except smtplib.SMTPException as e:
                    return JsonResponse({'success': False, 'message': f'SMTP error: {str(e)}'})
                except Exception as e:
                    return JsonResponse({'success': False, 'message': f'Connection failed: {str(e)}'})

            # Send test mail to any recipient using a saved SMTP address
            if action == 'send_test_mail':
                import smtplib
                from email.mime.text import MIMEText
                from email.mime.multipart import MIMEMultipart
                from .models import EmailSendLog
                addr_id = request.POST.get('address_id')
                to_email = request.POST.get('to_email', '').strip()
                subject = request.POST.get('subject', 'NewMovies - Test Email').strip()
                body = request.POST.get('body', '').strip()
                if not addr_id or not to_email:
                    return JsonResponse({'success': False, 'message': 'Select an SMTP address and enter a recipient email.'})
                addr_obj = EmailAddress.objects.filter(pk=addr_id).first()
                if not addr_obj:
                    return JsonResponse({'success': False, 'message': 'SMTP address not found.'})
                if not addr_obj.smtp_host:
                    return JsonResponse({'success': False, 'message': 'SMTP host is not configured for this address.'})
                try:
                    msg = MIMEMultipart('alternative')
                    msg['Subject'] = subject
                    msg['From'] = f'{addr_obj.display_name or "NewMovies"} <{addr_obj.email}>'
                    msg['To'] = to_email
                    # Plain text version
                    text_part = MIMEText(body, 'plain')
                    msg.attach(text_part)
                    # HTML version
                    html_body = '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;background:#f8fafc;border-radius:12px;"><div style="background:linear-gradient(135deg,#1a1a2e,#16213e);padding:24px;border-radius:12px 12px 0 0;text-align:center;"><h2 style="color:#fff;margin:0;">NewMovies</h2><p style="color:#94a3b8;margin:8px 0 0;font-size:14px;">Test Email</p></div><div style="padding:24px;background:#fff;border-radius:0 0 12px 12px;"><p style="color:#334155;font-size:15px;line-height:1.6;">' + body.replace(chr(10), '<br>') + '</p><hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;"><p style="color:#94a3b8;font-size:12px;text-align:center;">Sent from NewMovies Admin Panel - ' + addr_obj.email + '</p></div></div>'
                    html_part = MIMEText(html_body, 'html')
                    msg.attach(html_part)
                    smtp_user = addr_obj.smtp_username or addr_obj.email
                    smtp_pass = addr_obj.smtp_password
                    if not smtp_pass:
                        EmailSendLog.objects.create(address_id=addr_obj.pk, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message='SMTP password not set', sent_by=request.user, source='test_mail')
                        return JsonResponse({'success': False, 'message': 'SMTP password is not set for this address. Edit and save the password first.'})
                    server = smtplib.SMTP(addr_obj.smtp_host, addr_obj.smtp_port, timeout=15)
                    server.ehlo()
                    if addr_obj.smtp_use_tls:
                        server.starttls()
                        server.ehlo()
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(addr_obj.email, [to_email], msg.as_string())
                    server.quit()
                    EmailSendLog.objects.create(address_id=addr_obj.pk, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='sent', sent_by=request.user, source='test_mail')
                    return JsonResponse({'success': True, 'message': f'Test email sent successfully to {to_email}! Check inbox.'})
                except smtplib.SMTPAuthenticationError as e:
                    EmailSendLog.objects.create(address_id=addr_obj.pk, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message=f'Authentication failed: {str(e)}', sent_by=request.user, source='test_mail')
                    return JsonResponse({'success': False, 'message': f'Authentication failed: {str(e)}. Check email and app password.'})
                except smtplib.SMTPConnectError as e:
                    EmailSendLog.objects.create(address_id=addr_obj.pk, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message=f'Connection failed: {str(e)}', sent_by=request.user, source='test_mail')
                    return JsonResponse({'success': False, 'message': f'Cannot connect to {addr_obj.smtp_host}:{addr_obj.smtp_port}. Check host and port.'})
                except smtplib.SMTPException as e:
                    EmailSendLog.objects.create(address_id=addr_obj.pk, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message=f'SMTP error: {str(e)}', sent_by=request.user, source='test_mail')
                    return JsonResponse({'success': False, 'message': f'SMTP error: {str(e)}'})
                except Exception as e:
                    EmailSendLog.objects.create(address_id=addr_obj.pk, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message=str(e), sent_by=request.user, source='test_mail')
                    return JsonResponse({'success': False, 'message': f'Failed to send: {str(e)}'})

            return JsonResponse({'success': False, 'message': 'Unknown action'}, status=400)

        except Exception as e:
            logger.exception('email_settings AJAX error: %s', e)
            return JsonResponse({'success': False, 'message': 'Server error: %s' % e}, status=500)
    # GET — render the page
    addresses = EmailAddress.objects.all()
    templates = EmailTemplate.objects.all()
    purposes = dict(PURPOSE_CHOICES)

    # Group addresses by purpose (only non-empty groups)
    grouped_addresses = []
    purpose_order = ['verification', 'password_reset', 'notification', 'newsletter', 'marketing', 'transactional']
    for pcode in purpose_order:
        addrs = addresses.filter(purpose=pcode)
        if addrs.exists():
            grouped_addresses.append({
                'code': pcode,
                'label': purposes.get(pcode, pcode.title()),
                'addresses': addrs,
            })
    # Catch any addresses with non-standard purpose
    known = set(purpose_order)
    for pcode in set(a.purpose for a in addresses):
        if pcode not in known:
            addrs = addresses.filter(purpose=pcode)
            if addrs.exists():
                grouped_addresses.append({
                    'code': pcode,
                    'label': purposes.get(pcode, pcode.title()),
                    'addresses': addrs,
                })

    # Group templates by purpose (only non-empty groups)
    grouped_templates = []
    for pcode in purpose_order:
        tmpls = templates.filter(purpose=pcode)
        if tmpls.exists():
            grouped_templates.append({
                'code': pcode,
                'label': purposes.get(pcode, pcode.title()),
                'templates': tmpls,
            })
    for pcode in set(t.purpose for t in templates):
        if pcode not in known:
            tmpls = templates.filter(purpose=pcode)
            if tmpls.exists():
                grouped_templates.append({
                    'code': pcode,
                    'label': purposes.get(pcode, pcode.title()),
                    'templates': tmpls,
                })

    from .models import EmailSendLog
    send_logs = EmailSendLog.objects.select_related('address', 'sent_by').all()[:50]
    log_stats = {
        'total': EmailSendLog.objects.count(),
        'sent': EmailSendLog.objects.filter(status='sent').count(),
        'failed': EmailSendLog.objects.filter(status='failed').count(),
        'today': EmailSendLog.objects.filter(created_at__date__gte=timezone.localdate()).count(),
    }
    log_by_purpose = _email_purpose_stats()

    return render(request, 'core/email_management.html', {
        'addresses': addresses,
        'email_templates': templates,
        'grouped_addresses': grouped_addresses,
        'grouped_templates': grouped_templates,
        'purposes': purposes,
        'purpose_choices': PURPOSE_CHOICES,
        'back_url': 'admin_dashboard',
        'send_logs': send_logs,
        'log_stats': log_stats,
        'log_by_purpose': log_by_purpose,
    })


def _safe_sent_by(log):
    """Return the sender name from an EmailSendLog, handling missing User."""
    if not log.sent_by_id:
        return 'System'
    try:
        return str(log.sent_by)
    except Exception:
        return f'User #{log.sent_by_id}'


def _email_purpose_stats():
    """Per-purpose delivery summary for the email dashboard. Mirrors the SMTP
    lookup chain in core/auth.send_configured_email so each card shows exactly
    which SMTP box a purpose sends through (including the any-purpose default
    fallback), plus sent/failed/today counts that make delivery problems visible
    at a glance."""
    from .models import EmailAddress, EmailSendLog, PURPOSE_CHOICES

    purpose_labels = dict(PURPOSE_CHOICES)
    purpose_order = ['verification', 'password_reset', 'notification', 'newsletter', 'marketing', 'transactional']
    today = timezone.localdate()

    def serving_address(pcode):
        addr = EmailAddress.objects.filter(purpose=pcode, is_active=True, is_default=True).first()
        if not addr:
            addr = EmailAddress.objects.filter(purpose=pcode, is_active=True).first()
        if not addr:
            addr = EmailAddress.objects.filter(is_active=True, is_default=True).first()
        if not addr:
            addr = EmailAddress.objects.filter(is_active=True).first()
        return addr

    log_purposes = set(EmailSendLog.objects.values_list('purpose', flat=True))
    cfg_purposes = set(EmailAddress.objects.values_list('purpose', flat=True))
    present = sorted(log_purposes | cfg_purposes, key=lambda p: (purpose_order.index(p) if p in purpose_order else 99, p))

    groups = []
    for pcode in present:
        base = EmailSendLog.objects.filter(purpose=pcode)
        addr = serving_address(pcode)
        groups.append({
            'code': pcode,
            'label': purpose_labels.get(pcode, pcode.title()),
            'total': base.count(),
            'sent': base.filter(status='sent').count(),
            'failed': base.filter(status='failed').count(),
            'today': base.filter(created_at__date=today).count(),
            'served_by': addr.email if addr else None,
            'is_fallback': bool(addr and addr.purpose != pcode),
            'configured': EmailAddress.objects.filter(purpose=pcode, is_active=True).exists(),
        })
    return groups


@login_required
@user_passes_test(is_staff_or_superuser)
def ajax_email_logs(request):
    """JSON feed for the Send Logs tab — per-purpose delivery stats, overall
    counters, and the most recent log rows. The dashboard polls this so delivery
    problems are visible without reloading the page or digging into the DB."""
    from .models import EmailSendLog, PURPOSE_CHOICES

    purpose_labels = dict(PURPOSE_CHOICES)
    recent = []
    for log in EmailSendLog.objects.select_related('address').all()[:50]:
        recent.append({
            'id': log.pk,
            'status': log.status,
            'recipient': log.recipient,
            'subject': (log.subject or '')[:120],
            'purpose': log.purpose,
            'purpose_label': purpose_labels.get(log.purpose, log.purpose.title()),
            'source': log.get_source_display(),
            'address': (log.address.email if log.address_id and log.address else None),
            'error': log.error_message or '',
            'sent_by': _safe_sent_by(log),
            'time': timezone.localtime(log.created_at).strftime('%b %d, %H:%M:%S'),
        })

    today = timezone.localdate()
    qs = EmailSendLog.objects.all()
    return JsonResponse({
        'ok': True,
        'stats': {
            'total': qs.count(),
            'sent': qs.filter(status='sent').count(),
            'failed': qs.filter(status='failed').count(),
            'today': qs.filter(created_at__date=today).count(),
        },
        'groups': _email_purpose_stats(),
        'recent': recent,
        'updated_at': timezone.localtime().strftime('%H:%M:%S'),
    })


@login_required
@user_passes_test(is_staff_or_superuser)
@login_required
@user_passes_test(is_staff_or_superuser)
def footer_settings(request):
    return _settings_section_view(
        request, FooterSettingsForm, 'Footer Settings',
        description='Edit all footer sections including links, genres, countries, subscribe block, logo area, copyright, and disclaimer.'
    )


@login_required
@user_passes_test(is_staff_or_superuser)
def data_source_usage_stats(request):
    api_keys = TMDBApiKey.objects.all().order_by('-usage_count', '-is_active', '-created_at')
    usage_logs = DataSourceUsageLog.objects.all().order_by('-last_used_at', '-usage_count')
    summary = {
        'db': DataSourceUsageLog.objects.filter(source='db').aggregate(total=Sum('usage_count'))['total'] or 0,
        'api': DataSourceUsageLog.objects.filter(source='api').aggregate(total=Sum('usage_count'))['total'] or 0,
        'api_fallback': DataSourceUsageLog.objects.filter(source='api_fallback').aggregate(total=Sum('usage_count'))['total'] or 0,
    }
    return render(request, 'core/data_source_usage_stats.html', {
        'site_settings': SiteSettings.get_settings(),
        'api_keys': api_keys,
        'usage_logs': usage_logs,
        'summary': summary,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def data_source_config(request):
    site_settings = SiteSettings.get_settings()
    api_keys = TMDBApiKey.objects.all().order_by('-is_active', 'last_used_at', '-created_at')
    return render(request, 'core/data_source_config.html', {
        'site_settings': site_settings,
        'api_keys': api_keys,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def test_db_connection(request):
    """AJAX view to test the TMDB DB connection"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    
    try:
        conn = get_tmdb_db_connection()
        conn.close()
        return JsonResponse({'success': True, 'message': 'Connection successful!'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Connection failed: {str(e)}'})


@login_required
@user_passes_test(is_staff_or_superuser)
def add_api_key(request):
    """AJAX view to add a new TMDB API key"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    
    form = TMDBApiKeyForm(request.POST)
    if form.is_valid():
        api_key = form.save()
        return JsonResponse({
            'success': True,
            'message': 'API key added successfully!',
            'api_key': {
                'id': api_key.id,
                'key': api_key.key,
                'is_active': api_key.is_active,
                'created_at': timezone.localtime(api_key.created_at).strftime('%Y-%m-%d %H:%M'),
                'last_used_at': None,
            }
        })
    return JsonResponse({'success': False, 'message': 'Invalid form data', 'errors': form.errors})


@login_required
@user_passes_test(is_staff_or_superuser)
def update_api_key(request, key_id):
    """AJAX view to update an existing TMDB API key"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})

    api_key = get_object_or_404(TMDBApiKey, id=key_id)
    form = TMDBApiKeyEditForm(request.POST, instance=api_key)
    if form.is_valid():
        api_key = form.save()
        return JsonResponse({
            'success': True,
            'message': 'API key updated successfully!',
            'api_key': {
                'id': api_key.id,
                'key': api_key.key,
                'is_active': api_key.is_active,
                'created_at': timezone.localtime(api_key.created_at).strftime('%Y-%m-%d %H:%M'),
                'last_used_at': timezone.localtime(api_key.last_used_at).strftime('%Y-%m-%d %H:%M') if api_key.last_used_at else None,
            }
        })
    return JsonResponse({'success': False, 'message': 'Invalid form data', 'errors': form.errors})


@login_required
@user_passes_test(is_staff_or_superuser)
def delete_api_key(request, key_id):
    """AJAX view to delete a TMDB API key"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    
    try:
        api_key = get_object_or_404(TMDBApiKey, id=key_id)
        api_key.delete()
        return JsonResponse({'success': True, 'message': 'API key deleted successfully!'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error deleting key: {str(e)}'})


@login_required
@user_passes_test(is_staff_or_superuser)
def toggle_api_key(request, key_id):
    """AJAX view to toggle the active status of a TMDB API key"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    
    try:
        api_key = get_object_or_404(TMDBApiKey, id=key_id)
        api_key.is_active = not api_key.is_active
        api_key.save()
        return JsonResponse({'success': True, 'message': 'API key status updated!', 'is_active': api_key.is_active})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error updating key: {str(e)}'})


@login_required
@user_passes_test(is_staff_or_superuser)
def toggle_hide_live_tv(request):
    # AJAX view to toggle hide_live_tv setting
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    try:
        settings_obj = SiteSettings.get_settings()
        settings_obj.hide_live_tv = not settings_obj.hide_live_tv
        settings_obj.save()
        return JsonResponse({'success': True, 'message': 'Live TV setting updated!', 'hide_live_tv': settings_obj.hide_live_tv})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error updating setting: {str(e)}'})


@login_required
@user_passes_test(is_staff_or_superuser)
def toggle_footer(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    try:
        settings_obj = SiteSettings.get_settings()
        enabled_value = request.POST.get('enabled')
        if enabled_value is None:
            return JsonResponse({'success': False, 'message': 'Missing enabled state'})
        settings_obj.footer_enabled = str(enabled_value).lower() in ['true', '1', 'yes', 'on']
        settings_obj.save()
        return JsonResponse({'success': True, 'message': 'Footer setting updated!', 'footer_enabled': settings_obj.footer_enabled})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error updating footer setting: {str(e)}'})


@login_required
@user_passes_test(is_staff_or_superuser)
def set_data_source(request):
    # AJAX view to set data source
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    try:
        data = json.loads(request.body)
        data_source = data.get('data_source', 'tmdb')
        
        if data_source not in ['tmdb', 'local', 'tmdb_db']:
            return JsonResponse({'success': False, 'message': 'Invalid data source'})
        
        settings_obj = SiteSettings.get_settings()
        settings_obj.data_source = data_source
        settings_obj.save()
        
        return JsonResponse({'success': True, 'message': 'Data source updated'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error updating data source: {str(e)}'})


@login_required
@user_passes_test(is_staff_or_superuser)
def test_tmdb_api(request):
    # AJAX view to test TMDB API keys
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    
    try:
        # Get active API keys
        active_keys = TMDBApiKey.objects.filter(is_active=True).order_by('last_used_at', 'created_at')
        if not active_keys.exists():
            return JsonResponse({'success': False, 'message': 'No active API keys found'})
        
        # Test each API key until we find one that works
        for key in active_keys:
            try:
                response = requests.get(f"{settings.TMDB_BASE_URL}/genre/movie/list", params={'api_key': key.key})
                if response.status_code == 200:
                    # Update last used
                    key.last_used_at = timezone.now()
                    key.save()
                    return JsonResponse({'success': True, 'message': 'TMDB API key is valid'})
            except:
                continue
        
        return JsonResponse({'success': False, 'message': 'All API keys are invalid'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error testing API: {str(e)}'})


@login_required
@user_passes_test(is_staff_or_superuser)
def save_db_config(request):
    # AJAX view to save DB configuration
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    try:
        data = json.loads(request.body)
        settings_obj = SiteSettings.get_settings()
        
        if 'tmdb_db_host' in data:
            settings_obj.tmdb_db_host = data.get('tmdb_db_host')
        if 'tmdb_db_port' in data:
            settings_obj.tmdb_db_port = data.get('tmdb_db_port')
        if 'tmdb_db_name' in data:
            settings_obj.tmdb_db_name = data.get('tmdb_db_name')
        if 'tmdb_db_user' in data:
            settings_obj.tmdb_db_user = data.get('tmdb_db_user')
        if 'tmdb_db_password' in data:
            settings_obj.tmdb_db_password = data.get('tmdb_db_password')
        if 'tmdb_db_enabled' in data:
            settings_obj.tmdb_db_enabled = data.get('tmdb_db_enabled')
        if 'tmdb_db_enable_api_fallback' in data:
            settings_obj.tmdb_db_enable_api_fallback = data.get('tmdb_db_enable_api_fallback')
        
        settings_obj.save()
        return JsonResponse({'success': True, 'message': 'DB configuration saved'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error saving DB config: {str(e)}'})


@login_required
@user_passes_test(is_staff_or_superuser)
def content_row_list(request):
    content_rows = ContentRow.objects.all().order_by('order')
    return render(request, 'core/content_row_list.html', {'content_rows': content_rows})


@login_required
@user_passes_test(is_staff_or_superuser)
def content_row_create(request):
    if request.method == 'POST':
        form = ContentRowForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('content_row_list')
    else:
        form = ContentRowForm()
    return render(request, 'core/content_row_form.html', {'form': form, 'action': 'Create'})


@login_required
@user_passes_test(is_staff_or_superuser)
def content_row_edit(request, row_id):
    content_row = get_object_or_404(ContentRow, id=row_id)
    if request.method == 'POST':
        form = ContentRowForm(request.POST, instance=content_row)
        if form.is_valid():
            form.save()
            return redirect('content_row_list')
    else:
        form = ContentRowForm(instance=content_row)
    return render(request, 'core/content_row_form.html', {'form': form, 'action': 'Edit'})


@login_required
@user_passes_test(is_staff_or_superuser)
def content_row_delete(request, row_id):
    content_row = get_object_or_404(ContentRow, id=row_id)
    if request.method == 'POST':
        content_row.delete()
        return redirect('content_row_list')
    return render(request, 'core/object_delete_confirm.html', {
        'page_title': 'Delete Content Row',
        'object_label': 'the content row',
        'object_name': content_row.title,
        'cancel_url_name': 'content_row_list',
    })



@login_required
@user_passes_test(is_staff_or_superuser)
def provider_item_list(request):
    from django.core.paginator import Paginator
    providers = ProviderItem.objects.all().order_by('display_priority', 'name')
    search = request.GET.get('q', '').strip()
    if search:
        providers = providers.filter(
            models.Q(name__icontains=search) |
            models.Q(slug__icontains=search) |
            models.Q(tmdb_provider_id=search) if search.isdigit() else models.Q(name__icontains=search)
        )
    total = ProviderItem.objects.count()
    enabled_count = ProviderItem.objects.filter(is_enabled=True).count()
    disabled_count = total - enabled_count
    movies_count = ProviderItem.objects.filter(supports_movies=True).count()
    tv_count = ProviderItem.objects.filter(supports_tv=True).count()
    paginator = Paginator(providers, 50)
    page = request.GET.get('page')
    providers = paginator.get_page(page)
    return render(request, 'core/provider_item_list.html', {
        'providers': providers,
        'total': total,
        'enabled_count': enabled_count,
        'disabled_count': disabled_count,
        'movies_count': movies_count,
        'tv_count': tv_count,
        'search': search,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def provider_item_create(request):
    form = ProviderItemForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('provider_item_list')
    return render(request, 'core/provider_item_form.html', {'form': form, 'provider': None})


@login_required
@user_passes_test(is_staff_or_superuser)
def provider_item_edit(request, provider_id):
    provider = get_object_or_404(ProviderItem, id=provider_id)
    if request.method == 'POST':
        form = ProviderItemForm(request.POST, instance=provider)
        if form.is_valid():
            form.save()
            return redirect('provider_item_list')
    else:
        form = ProviderItemForm(instance=provider)
    return render(request, 'core/provider_item_form.html', {'form': form, 'provider': provider})


@login_required
@user_passes_test(is_staff_or_superuser)
def provider_item_delete(request, provider_id):
    provider = get_object_or_404(ProviderItem, id=provider_id)
    if request.method == 'POST':
        provider.delete()
        return redirect('provider_item_list')
    return render(request, 'core/catalog_filter_delete.html', {'object': provider, 'object_type': 'provider', 'cancel_url_name': 'provider_item_list'})


@login_required
@user_passes_test(is_staff_or_superuser)
def provider_item_toggle(request, provider_id):
    if request.method == 'POST':
        provider = get_object_or_404(ProviderItem, id=provider_id)
        provider.is_enabled = not provider.is_enabled
        provider.save(update_fields=['is_enabled', 'updated_at'])
    return redirect('provider_item_list')


@login_required
@user_passes_test(is_staff_or_superuser)
def provider_item_sync(request):
    if request.method == 'POST':
        from .provider_sync import sync_provider_items_once
        result = sync_provider_items_once()
        messages.success(request, f"Provider sync completed from {result['source']}.")
    return redirect('provider_item_list')


@login_required
@user_passes_test(is_staff_or_superuser)
def watch_region_list(request):
    return render(request, 'core/watch_region_list.html', {'regions': WatchRegion.objects.all()})


@login_required
@user_passes_test(is_staff_or_superuser)
def watch_region_create(request):
    form = WatchRegionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('watch_region_list')
    return render(request, 'core/watch_region_form.html', {'form': form, 'region': None})


@login_required
@user_passes_test(is_staff_or_superuser)
def watch_region_edit(request, region_id):
    region = get_object_or_404(WatchRegion, id=region_id)
    form = WatchRegionForm(request.POST or None, instance=region)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('watch_region_list')
    return render(request, 'core/watch_region_form.html', {'form': form, 'region': region})


@login_required
@user_passes_test(is_staff_or_superuser)
def watch_region_delete(request, region_id):
    region = get_object_or_404(WatchRegion, id=region_id)
    if request.method == 'POST':
        region.delete()
        return redirect('watch_region_list')
    return render(request, 'core/catalog_filter_delete.html', {'object': region, 'object_type': 'region', 'cancel_url_name': 'watch_region_list'})


@login_required
@user_passes_test(is_staff_or_superuser)
def watch_region_toggle(request, region_id):
    if request.method == 'POST':
        region = get_object_or_404(WatchRegion, id=region_id)
        region.is_enabled = not region.is_enabled
        region.save(update_fields=['is_enabled', 'updated_at'])
    return redirect('watch_region_list')


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_list(request):
    apps = AndroidApp.objects.all().order_by('name')
    totals = AndroidAppAccessLog.objects.values('android_app').annotate(total=models.Sum('connection_count'))
    totals_map = {item['android_app']: item['total'] or 0 for item in totals}
    return render(request, 'core/android_app_list.html', {
        'apps': apps,
        'totals_map': totals_map,
    })


def _normalize_ip(value):
    """Return a normalized IP string, or None for blank/invalid input."""
    import ipaddress
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def _mark_apk_opened(ip_address, user_id='', device_model='', os_version='',
                     build_identifier='', days=90):
    """Flag the newest recent download from this IP as 'app opened'.

    Called when the app hits its API, which means it was launched on the
    device that (from that IP) previously downloaded the APK. Best-effort.
    """
    from datetime import timedelta

    ip = _normalize_ip(ip_address)
    if not ip:
        return None
    try:
        cutoff = timezone.now() - timedelta(days=days)
        download = (ApkDownload.objects
                    .filter(ip_address=ip, opened_app=False,
                            downloaded_at__gte=cutoff)
                    .order_by('-downloaded_at')
                    .first())
        if download is None:
            return None
        download.opened_app = True
        download.opened_at = timezone.now()
        if user_id:
            download.device_user_id = user_id[:255]
        if device_model:
            download.device_model = device_model[:255]
        if os_version:
            download.os_version = os_version[:50]
        if build_identifier:
            download.build_identifier = build_identifier[:255]
        download.save(update_fields=[
            'opened_app', 'opened_at', 'device_user_id', 'device_model',
            'os_version', 'build_identifier',
        ])
        return download
    except Exception:
        logger.debug('APK open-match failed', exc_info=True)
        return None


def apk_download(request, app_id):
    """Serve an app's APK and record who downloaded it (IP + user agent).

    Public: the app itself calls this URL when an update is required, and
    visitors may download the APK directly. The download row's IP is later
    matched against the app's API hits to mark it as opened.
    """
    import os
    from django.http import FileResponse, Http404

    android_app = get_object_or_404(AndroidApp, id=app_id)
    if not android_app.apk_file:
        raise Http404('No APK configured for this app')

    try:
        ApkDownload.objects.create(
            android_app=android_app,
            app_slug=(android_app.slug or '')[:255],
            file_name=os.path.basename(android_app.apk_file.name or '')[:500],
            ip_address=_normalize_ip(_get_client_ip(request)),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:500],
            referer=(request.META.get('HTTP_REFERER', '') or '')[:500],
        )
    except Exception:
        logger.debug('Could not record APK download', exc_info=True)

    try:
        handle = android_app.apk_file.open('rb')
    except Exception:
        raise Http404('APK file is missing')
    filename = os.path.basename(android_app.apk_file.name or 'app.apk')
    return FileResponse(
        handle, as_attachment=True, filename=filename,
        content_type='application/vnd.android.package-archive',
    )


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_downloads(request):
    """Admin page: APK downloads with IP, app-opened match and date-wise totals."""
    import csv
    from django.http import HttpResponse

    apps = AndroidApp.objects.all().order_by('name')

    app_id = (request.GET.get('app') or '').strip()
    status = (request.GET.get('status') or '').strip()
    q = (request.GET.get('q') or '').strip()[:200]
    date_from = (request.GET.get('date_from') or '').strip()
    date_to = (request.GET.get('date_to') or '').strip()

    def _valid_date(value):
        try:
            datetime.datetime.strptime(value, '%Y-%m-%d')
            return True
        except (ValueError, TypeError):
            return False

    if not _valid_date(date_from):
        date_from = ''
    if not _valid_date(date_to):
        date_to = ''

    qs = ApkDownload.objects.select_related('android_app')
    if app_id.isdigit():
        qs = qs.filter(android_app_id=int(app_id))
    else:
        app_id = ''
    if status == 'opened':
        qs = qs.filter(opened_app=True)
    elif status == 'not_opened':
        qs = qs.filter(opened_app=False)
    else:
        status = ''
    if q:
        qs = qs.filter(
            Q(ip_address__icontains=q) | Q(user_agent__icontains=q)
            | Q(file_name__icontains=q) | Q(device_user_id__icontains=q)
            | Q(device_model__icontains=q)
        )
    if date_from:
        qs = qs.filter(downloaded_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(downloaded_at__date__lte=date_to)

    total = qs.count()
    opened = qs.filter(opened_app=True).count()
    unique_ips = (qs.exclude(ip_address__isnull=True)
                    .values('ip_address').distinct().count())
    conversion = round((opened / total) * 100, 1) if total else 0

    daily = list(
        qs.annotate(day=TruncDate('downloaded_at'))
          .values('day')
          .annotate(downloads=Count('id'),
                    opened=Count('id', filter=Q(opened_app=True)),
                    unique_ips=Count('ip_address', distinct=True))
          .order_by('-day')
    )

    records = list(qs.order_by('-downloaded_at')[:1000])

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="apk_downloads.csv"'
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow([
            'Downloaded at', 'App', 'IP address', 'File', 'Opened app',
            'Opened at', 'Device user ID', 'Device model', 'OS', 'Build',
            'User-Agent', 'Referer',
        ])
        for r in records:
            writer.writerow([
                timezone.localtime(r.downloaded_at).strftime('%Y-%m-%d %H:%M:%S'),
                r.android_app.name if r.android_app else r.app_slug,
                r.ip_address or '', r.file_name, 'yes' if r.opened_app else 'no',
                timezone.localtime(r.opened_at).strftime('%Y-%m-%d %H:%M:%S') if r.opened_at else '',
                r.device_user_id, r.device_model, r.os_version, r.build_identifier,
                r.user_agent, r.referer,
            ])
        return response

    return render(request, 'core/android_app_downloads.html', {
        'apps': apps,
        'records': records,
        'daily': daily,
        'total': total,
        'opened': opened,
        'unique_ips': unique_ips,
        'conversion': conversion,
        'filters': {
            'app': app_id, 'status': status, 'q': q,
            'date_from': date_from, 'date_to': date_to,
        },
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def toggle_android_app(request, app_id):
    if request.method == 'POST':
        android_app = get_object_or_404(AndroidApp, id=app_id)
        android_app.is_active = not android_app.is_active
        android_app.save(update_fields=['is_active', 'updated_at'])
        return redirect('android_app_list')
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_create(request):
    if request.method == 'POST':
        form = AndroidAppForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            return redirect('android_app_list')
    else:
        form = AndroidAppForm()
    return render(request, 'core/android_app_form.html', {'form': form, 'action': 'Create'})


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_edit(request, app_id):
    android_app = get_object_or_404(AndroidApp, id=app_id)
    if request.method == 'POST':
        form = AndroidAppForm(request.POST, request.FILES, instance=android_app)
        if form.is_valid():
            form.save()
            return redirect('android_app_list')
    else:
        form = AndroidAppForm(instance=android_app)
    return render(request, 'core/android_app_form.html', {
        'form': form,
        'action': 'Edit',
        'android_app': android_app,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_delete(request, app_id):
    android_app = get_object_or_404(AndroidApp, id=app_id)
    if request.method == 'POST':
        android_app.delete()
        return redirect('android_app_list')
    return render(request, 'core/android_app_delete.html', {'android_app': android_app})


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_dashboard(request, app_id=None):
    apps = AndroidApp.objects.all().order_by('name')
    selected_app = None

    # Handle POST to update settings (data retention, log collection, log retention)
    if request.method == 'POST' and app_id:
        selected_app = get_object_or_404(AndroidApp, id=app_id)
        try:
            update_fields = ['updated_at']

            # Data retention
            retention_days = request.POST.get('retention_days')
            if retention_days is not None:
                retention_days = max(1, min(3650, int(retention_days)))
                selected_app.data_retention_days = retention_days
                update_fields.append('data_retention_days')

            # Log collection enabled toggle
            log_collection_enabled = request.POST.get('log_collection_enabled')
            if log_collection_enabled is not None:
                selected_app.log_collection_enabled = log_collection_enabled == 'true' or log_collection_enabled == 'on'
                update_fields.append('log_collection_enabled')

            # Log retention days
            log_retention_days = request.POST.get('log_retention_days')
            if log_retention_days is not None:
                log_retention_days = max(1, min(3650, int(log_retention_days)))
                selected_app.log_retention_days = log_retention_days
                update_fields.append('log_retention_days')

            selected_app.save(update_fields=update_fields)
            deleted = selected_app.clean_old_analytics_data()
            return JsonResponse({
                'success': True,
                'data_retention_days': selected_app.data_retention_days,
                'log_collection_enabled': selected_app.log_collection_enabled,
                'log_retention_days': selected_app.log_retention_days,
                'deleted': deleted,
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    if app_id is not None:
        selected_app = get_object_or_404(AndroidApp, id=app_id)
    elif apps:
        selected_app = apps.first()

    chart_labels = []
    chart_values = []
    unique_chart_labels = []
    unique_chart_values = []
    app_endpoint = None
    build_summary = []
    build_chart_labels = []
    build_chart_values = []
    failed_attempts = []
    recent_logs = []
    total_unique_visitors = 0
    if selected_app:
        # Auto-clean old analytics data based on retention setting
        selected_app.clean_old_analytics_data()

        logs = selected_app.access_logs.order_by('access_date')
        chart_labels = [log.access_date.strftime('%Y-%m-%d') for log in logs]
        chart_values = [log.connection_count for log in logs]
        app_endpoint = request.build_absolute_uri(f"/api/android-apps/{selected_app.slug}/")
        build_summary = list(
            selected_app.build_logs.values('build_identifier').annotate(
                total_connections=models.Sum('connection_count')
            ).order_by('-total_connections', 'build_identifier')
        )
        build_chart_labels = [item['build_identifier'] for item in build_summary[:10]]
        build_chart_values = [item['total_connections'] or 0 for item in build_summary[:10]]
        failed_attempts = selected_app.failed_attempts.all()[:5]
        
        # Get unique visitor data
        unique_logs = selected_app.daily_unique_visitors.order_by('access_date')
        unique_chart_labels = [log.access_date.strftime('%Y-%m-%d') for log in unique_logs]
        unique_chart_values = [log.unique_visitor_count for log in unique_logs]
        
        # Recent devices and visits moved to the full-screen user/device page,
        # so this dashboard no longer loads either list.
        # Last 20 app logs so Users / Android / Logs live on this one page.
        recent_logs = selected_app.app_logs.select_related('device').all()[:20]
        
        # Calculate total unique visitors
        total_unique_visitors = selected_app.devices.count()

    # Use denormalized total_connections field instead of N+1 aggregate query
    summary_rows = [
        {'app': app, 'total_connections': app.total_connections, 'last_accessed_at': app.last_accessed_at}
        for app in apps
    ]

    # Synced users (single query with annotate instead of 3 separate queries)
    from .models import SyncedUser, UserHeartbeat
    synced_qs = SyncedUser.objects.all()
    synced_users = synced_qs.order_by('-last_synced_at')[:10]
    synced_total = synced_qs.count()
    synced_subscribed = synced_qs.filter(is_subscribed=True).count()
    synced_free = synced_total - synced_subscribed

    # Latest heartbeat per email — powers the Online/Offline badge and the live
    # device / app-version columns in the inline Users & Blocking table.
    hb_map = {h.email: h for h in UserHeartbeat.objects.all()}

    # ---- Build health -----------------------------------------------------
    # AndroidAppBuildLog keeps one row per build per day, so a week of them
    # shows a release that never propagated as plainly as one that did. The
    # gate's refusals are counted from the failed attempts, which is the number
    # that says "raise allowed_build_id" or "this build is being turned away".
    from datetime import timedelta as _timedelta

    build_health = []
    oldest_build = None
    oldest_share = 0
    if selected_app is not None:
        try:
            from .models import AndroidAppBuildLog, AndroidAppFailedAttempt
            week_ago = timezone.localdate() - _timedelta(days=7)
            logs = list(
                AndroidAppBuildLog.objects
                .filter(android_app=selected_app, access_date__gte=week_ago)
                .values('build_identifier')
                .annotate(reqs=Sum('connection_count'))
                .order_by('-reqs')[:12])
            rejected = {
                row['build_identifier']: row['n']
                for row in AndroidAppFailedAttempt.objects
                .filter(android_app=selected_app, attempted_at__date__gte=week_ago,
                        failure_reason='identity_invalid')
                .values('build_identifier')
                .annotate(n=Count('id'))
            }
            total_reqs = sum((row['reqs'] or 0) for row in logs) or 1
            for row in logs:
                build_health.append({
                    'build': row['build_identifier'] or '(unknown)',
                    'requests': row['reqs'] or 0,
                    'share': round(100.0 * (row['reqs'] or 0) / total_reqs, 1),
                    'rejected': rejected.get(row['build_identifier'], 0),
                })
        except Exception:
            logger.debug('build health: could not read the build logs', exc_info=True)

        try:
            from .models import SyncedUser
            builds = list(SyncedUser.objects.filter(build_number__gt=0)
                          .values('build_number').annotate(n=Count('id'))
                          .order_by('build_number'))
            if builds:
                oldest_build = builds[0]['build_number']
                total_accounts = sum(b['n'] for b in builds) or 1
                oldest_share = round(100.0 * builds[0]['n'] / total_accounts, 1)
        except Exception:
            logger.debug('build health: could not read the build split', exc_info=True)

    return render(request, 'core/android_app_dashboard.html', {
        'apps': apps,
        'selected_app': selected_app,
        'summary_rows': summary_rows,
        'chart_labels_json': json.dumps(chart_labels),
        'chart_values_json': json.dumps(chart_values),
        'unique_chart_labels_json': json.dumps(unique_chart_labels),
        'unique_chart_values_json': json.dumps(unique_chart_values),
        'build_chart_labels_json': json.dumps(build_chart_labels),
        'build_chart_values_json': json.dumps(build_chart_values),
        'build_summary': build_summary,
        'app_endpoint': app_endpoint,
        'failed_attempts': failed_attempts,
        'total_unique_visitors': total_unique_visitors,
        'recent_logs': recent_logs,
        'hb_map': hb_map,
        'synced_users': synced_users,
        'synced_total': synced_total,
        'synced_subscribed': synced_subscribed,
        'synced_free': synced_free,
        'build_health': build_health,
        'oldest_build': oldest_build,
        'oldest_share': oldest_share,
    })


# How many visits / log lines a device row shows when it is expanded.
DRILLDOWN_LIMIT = 10


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_users_full(request, app_id):
    """Full-screen user + device list behind the "Users & Blocking" card.

    The analytics page only renders the first 10 synced users inline, so it
    carries an "Open full list" button to this view: the complete list with
    every detail the server holds (account, heartbeat, device, subscription,
    cloud counts) where the admin can block, restore, wipe or resend for any
    row. It also lists the devices that are using the app WITHOUT an account —
    `AndroidAppDevice` rows whose device id matches no synced profile or
    heartbeat — plus heartbeats whose session has been signed out, so nobody
    using the app is invisible.

    The template extends base.html (not admin_base) deliberately: no admin
    side nav, just the list. The device table and the recent-visits table
    (with the full-request expander) live here too, so the analytics page can
    stay a dashboard instead of carrying four more lists.
    """
    from django.core.paginator import Paginator
    from .models import (PlayHistory, UserActivity, UserCloudData,  # noqa: F401
                         UserHeartbeat, WatchList)

    selected_app = get_object_or_404(AndroidApp, id=app_id)

    # ---------------------------------------------------------------- filters
    q = (request.GET.get('q') or '').strip()
    view = (request.GET.get('f') or 'all').strip().lower()

    users_qs = SyncedUser.objects.all().order_by('-last_synced_at')
    if q:
        users_qs = users_qs.filter(
            Q(email__icontains=q) | Q(display_name__icontains=q)
            | Q(device_id__icontains=q) | Q(device_model__icontains=q)
            | Q(plan__icontains=q)
        )
    if view == 'blocked':
        users_qs = users_qs.filter(block_mode__gt='')
    elif view == 'subscribed':
        users_qs = users_qs.filter(is_subscribed=True)
    elif view == 'free':
        users_qs = users_qs.filter(is_subscribed=False)
    elif view in ('online', 'never'):
        # Heartbeats live on the users DB, so the online filter is matched on
        # the email set first (small table) instead of a cross-DB join.
        online_emails = {
            e for e in UserHeartbeat.objects.filter(
                last_seen_at__gte=timezone.now() - timezone.timedelta(minutes=5)
            ).values_list('email', flat=True)
        }
        if view == 'online':
            users_qs = users_qs.filter(email__in=list(online_emails)) if online_emails else users_qs.none()
        else:
            users_qs = users_qs.exclude(email__in=list(online_emails))

    # ---------------------------------------------------------------- tabs
    # One list per tab. Only the open tab's rows are built and only its markup
    # is sent: this page used to render the accounts table, the devices table
    # with its per-device drill-downs, the whole recent-visits dump and the
    # signed-out list at the same time, which is what made it heavy. The tab
    # bar asks for ?partial=1 when the admin switches, so that swap needs no
    # page load — and with JS off the links still work as plain ?tab=… URLs.
    TABS = ('accounts', 'uniquedevices', 'signedout', 'visits')
    tab = (request.GET.get('tab') or '').strip().lower()
    legacy_devices = tab == 'devices'
    if legacy_devices:
        tab = 'uniquedevices'
    if tab not in TABS:
        tab = 'accounts'
    partial = request.GET.get('partial') == '1'


    # ------------------------------------------------------------ headline counts
    all_users = SyncedUser.objects.all()
    total_users = all_users.count()
    counts = {
        'total': total_users,
        'blocked': all_users.filter(block_mode__gt='').count(),
        'subscribed': all_users.filter(is_subscribed=True).count(),
        'free': all_users.filter(is_subscribed=False).count(),
    }

    # ---------------------------------------------------------- shared bits
    # Built for every tab: the six header badges, plus which device id belongs
    # to which account (the badge counts devices with none, and the devices /
    # visits tabs label each device with its account).

    email_by_device = {}
    for email, dev_id in SyncedUser.objects.exclude(device_id='').values_list('email', 'device_id'):
        if dev_id:
            email_by_device.setdefault(dev_id, email)
    for email, dev_id in UserHeartbeat.objects.exclude(device_id='').values_list('email', 'device_id'):
        if dev_id:
            email_by_device.setdefault(dev_id, email)
    linked_device_ids = list(email_by_device)

    all_devices = AndroidAppDevice.objects.filter(android_app=selected_app)
    counts['devices'] = all_devices.count()
    counts['unique_devices'] = len(
        set(SyncedUser.objects.exclude(device_id='').values_list('device_id', flat=True))
        | set(UserHeartbeat.objects.exclude(device_id='').values_list('device_id', flat=True))
        | set(all_devices.values_list('user_id', flat=True)))
    _today = timezone.localdate()
    counts['expiring'] = SyncedUser.objects.filter(
        is_subscribed=True, valid_until__gte=_today,
        valid_until__lte=_today + datetime.timedelta(days=7)).count()
    counts['guests'] = UserHeartbeat.objects.filter(email__startswith='guest_').count()
    counts['anonymous_devices'] = all_devices.exclude(
        user_id__in=linked_device_ids if linked_device_ids else ['']
    ).count()


    # Latest visit per device is not needed here; only the counts are.
    visits_count_qs = AndroidAppDeviceVisit.objects.filter(android_app=selected_app)
    if q:
        visits_count_qs = visits_count_qs.filter(
            Q(device__user_id__icontains=q) | Q(device_model__icontains=q)
            | Q(os_version__icontains=q) | Q(ip_address__icontains=q)
            | Q(build_identifier__icontains=q))
    counts['visits'] = visits_count_qs.count()
    counts['signed_out'] = UserHeartbeat.objects.filter(is_logged_in=False).count()

    ctx = {
        'selected_app': selected_app,
        'q': q,
        'view': view,
        'counts': counts,
        'active_tab': tab,
        'email_by_device': email_by_device,
    }

    if tab == 'accounts':
        # ------------------------------------------------------------- user rows
        paginator = Paginator(users_qs, 25)
        page_obj = paginator.get_page(request.GET.get('page') or 1)

        page_ids = [u.user_id for u in page_obj.object_list if u.user_id]
        page_emails = [u.email for u in page_obj.object_list if u.email]
        page_device_ids = [u.device_id for u in page_obj.object_list if u.device_id]

        hb_map = {h.email: h for h in UserHeartbeat.objects.filter(email__in=page_emails)}
        account_map = {u.pk: u for u in User.objects.filter(pk__in=page_ids)} if page_ids else {}

        def _counts_for(model):
            if not page_ids:
                return {}
            try:
                return dict(model.objects.filter(user_id__in=page_ids)
                            .values_list('user_id').annotate(n=Count('id')))
            except Exception:
                return {}

        history_counts = _counts_for(PlayHistory)
        watchlist_counts = _counts_for(WatchList)
        activity_counts = _counts_for(UserActivity)

        cloud_map = {}
        if page_ids:
            for c in UserCloudData.objects.filter(user_id__in=page_ids):
                cloud_map[c.user_id] = {
                    'history': len(c.watch_history or []),
                    'watchlist': len(c.watchlist_ids or []),
                    'favorites': len(c.favorites or []),
                    'playlists': len(c.playlists or []),
                    'downloads': len(c.downloads or []),
                    'progress': len(c.playback_progress or {}),
                    'version': c.data_version,
                }

        device_map = {}
        if page_device_ids:
            device_map = {d.user_id: d for d in AndroidAppDevice.objects.filter(user_id__in=page_device_ids)}

        rows = []
        for u in page_obj.object_list:
            hb = hb_map.get(u.email)
            acct = account_map.get(u.user_id)
            rows.append({
                'obj': u,
                'hb': hb,
                'account': acct,
                'device': device_map.get(u.device_id),
                'history': history_counts.get(u.user_id, 0),
                'watchlist': watchlist_counts.get(u.user_id, 0),
                'activity': activity_counts.get(u.user_id, 0),
                'cloud': cloud_map.get(u.user_id),
            })

        ctx.update({
            'rows': rows,
            'page_obj': page_obj,
        })

    elif tab == 'devices':
        devices_qs = all_devices.order_by('-last_seen_at')
        # The tab IS "devices with no account", so that is what shows unless
        # the "All devices" chip asks for everything (dn=0).
        no_account_only = request.GET.get('dn') != '0'
        if no_account_only and linked_device_ids:
            devices_qs = devices_qs.exclude(user_id__in=linked_device_ids)
        if q:
            devices_qs = devices_qs.filter(Q(user_id__icontains=q) | Q(device_model__icontains=q)
                                           | Q(os_version__icontains=q))

        device_page = Paginator(devices_qs, 25).get_page(request.GET.get('dpage') or 1)
        page_devices = list(device_page.object_list)
        last_visit = {}
        if page_devices:
            # Latest visit per device (one query) for IP + build + exact last hit.
            for v in (AndroidAppDeviceVisit.objects
                      .filter(device__in=page_devices).order_by('-visited_at')
                      .values('device_id', 'ip_address', 'build_identifier', 'visited_at')[:5000]):
                last_visit.setdefault(v['device_id'], v)

        # Everything each row expands into: the account behind the device, its
        # last visits (with the full request) and its last app-log lines. Two
        # queries pull the freshest few hundred visits/logs for the devices on
        # this page and they are grouped in Python — no window functions, so it
        # behaves the same on SQLite, MySQL and Oracle.
        def _drill_lists(devices):
            device_pks = [d.pk for d in devices]
            visits, logs = {}, {}
            if not device_pks:
                return visits, logs
            for v in (AndroidAppDeviceVisit.objects.filter(device_id__in=device_pks)
                      .order_by('-visited_at')[:DRILLDOWN_LIMIT * len(device_pks)]):
                bucket = visits.setdefault(v.device_id, [])
                if len(bucket) < DRILLDOWN_LIMIT:
                    bucket.append(v)
            for entry in (AndroidAppLog.objects.filter(device_id__in=device_pks)
                          .order_by('-timestamp')[:DRILLDOWN_LIMIT * len(device_pks)]):
                bucket = logs.setdefault(entry.device_id, [])
                if len(bucket) < DRILLDOWN_LIMIT:
                    bucket.append(entry)
            return visits, logs

        drill_visits, drill_logs = _drill_lists(page_devices)

        # The synced profile behind each device (plan, block state, last sync) and
        # the Django login it belongs to, keyed by the device id the app sends.
        synced_by_device = {}
        for su in SyncedUser.objects.exclude(device_id='').order_by('-last_synced_at'):
            synced_by_device.setdefault(su.device_id, su)
        account_ids = [su.user_id for su in synced_by_device.values() if su.user_id]
        account_by_id = ({u.pk: u for u in User.objects.filter(pk__in=account_ids)}
                         if account_ids else {})

        device_rows = []
        for d in page_devices:
            su = synced_by_device.get(d.user_id)
            device_rows.append({
                'device': d,
                'email': email_by_device.get(d.user_id),
                'visit': last_visit.get(d.id),
                'synced': su,
                'account': account_by_id.get(su.user_id) if su else None,
                'visits': drill_visits.get(d.pk, []),
                'logs': drill_logs.get(d.pk, []),
                'log_url': reverse('android_device_logs', args=[selected_app.id, d.user_id]),
            })

        ctx.update({
            'device_rows': device_rows,
            'device_page': device_page,
            'no_account_only': no_account_only,
        })

    elif tab == 'uniquedevices':
        # ------------------------------------------------------- unique devices
        # One row per device, merged from the account's sync and heartbeat (those
        # share an id space) and the install record that carries the visit
        # counters (a different id, joined through account_key). Rows are plain
        # primitives so the merged list can be cached for a minute and then
        # filtered, searched and paginated without touching the database again.
        from .models import HeartbeatActivity

        dview = (request.GET.get('ds') or '').strip().lower()
        if dview not in ('', 'all', 'noacc', 'online', 'paid', 'free', 'expiring'):
            dview = ''
        if not dview:
            dview = 'noacc' if legacy_devices else 'all'

        cache_key = 'app_unique_devices_v2_%s' % selected_app.id
        merged = cache.get(cache_key)
        if merged is None:
            profiles = list(SyncedUser.objects.all())
            heartbeats = list(UserHeartbeat.objects.all())
            synced_by_email = {}
            for prof in profiles:
                synced_by_email.setdefault(prof.email, prof)

            entries = {}
            for prof in profiles:
                if prof.device_id:
                    entries.setdefault(prof.device_id, {})['profile'] = prof
            for beat in heartbeats:
                if beat.device_id:
                    entries.setdefault(beat.device_id, {})['heartbeat'] = beat
            for install in all_devices:
                entries.setdefault(install.account_key or install.user_id, {}).setdefault(
                    'installs', []).append(install)

            account_ids = [prof.user_id for prof in profiles if prof.user_id]
            account_by_id = ({u.pk: u for u in User.objects.filter(pk__in=account_ids)}
                             if account_ids else {})

            install_ids = [d.pk for d in all_devices]
            last_ip, today_counts = {}, {}
            if install_ids:
                for row in (AndroidAppDeviceVisit.objects.filter(device_id__in=install_ids)
                            .order_by('-visited_at')
                            .values('device_id', 'ip_address')[:5000]):
                    last_ip.setdefault(row['device_id'], row['ip_address'])
                day_start = timezone.make_aware(
                    datetime.datetime.combine(timezone.localdate(), datetime.time.min))
                for row in (AndroidAppDeviceVisit.objects
                            .filter(device_id__in=install_ids, visited_at__gte=day_start)
                            .values('device_id').annotate(n=Count('id'))):
                    today_counts[row['device_id']] = row['n']

            merged = []
            for device_key, item in entries.items():
                beat = item.get('heartbeat')
                prof = item.get('profile') or (synced_by_email.get(beat.email) if beat else None)
                installs = item.get('installs') or []
                account = account_by_id.get(prof.user_id) if prof and prof.user_id else None
                online = beat.is_online if beat else any(
                    i.last_seen_at and (timezone.now() - i.last_seen_at).total_seconds()
                    <= UserHeartbeat.ONLINE_WINDOW_SECONDS for i in installs)
                seen = [i.last_seen_at for i in installs if i.last_seen_at]
                last_seen = (beat.last_seen_at if beat else
                             (max(seen) if seen else (prof.last_synced_at if prof else None)))
                visit_ip = next((last_ip.get(i.pk) for i in installs if last_ip.get(i.pk)), '')
                first_seen = min([i.first_seen_at for i in installs if i.first_seen_at],
                                 default=None)
                merged.append({
                    'id': device_key,
                    'model': ((beat.device_model if beat else '')
                              or next((i.device_model for i in installs if i.device_model), '')
                              or (prof.device_model if prof else '')),
                    'os': ((beat.os_version if beat else '')
                           or next((i.os_version for i in installs if i.os_version), '')
                           or (prof.os_version if prof else '')),
                    'email': (prof.email if prof else (beat.email if beat else '')),
                    'display_name': prof.display_name if prof else '',
                    'profile_pk': prof.pk if prof else None,
                    'verified': bool(prof.is_email_verified) if prof else False,
                    'block_display': (prof.get_block_mode_display()
                                      if prof and prof.block_mode else ''),
                    'is_subscribed': bool(prof.is_subscribed) if prof else False,
                    'plan': (prof.plan or '') if prof else '',
                    'valid_until': prof.valid_until if prof else None,
                    'days_left': prof.days_remaining if prof else 0,
                    'account_pk': account.pk if account else None,
                    'online': online,
                    'screen': beat.current_screen if beat else '',
                    'ip': ((beat.ip_address if beat and beat.ip_address else '') or visit_ip or ''),
                    'today': (sum(today_counts.get(i.pk, 0) for i in installs)
                              if installs else None),
                    'total': sum(i.total_visits for i in installs) if installs else None,
                    'last_seen': last_seen,
                    'registered': (prof.created_at if prof else first_seen),
                    'history_url': (reverse('admin_user_analytics_detail',
                                            args=[account.pk]) if account
                                    else (reverse('synced_user_detail',
                                                  args=[prof.pk]) if prof else '')),
                    'kind': 'account' if prof else ('guest' if beat else 'install'),
                    'linked_by_ip': any(i.account_key and i.account_key != i.user_id
                                        for i in installs),
                })
            cache.set(cache_key, merged, 60)

        rows = merged
        if dview == 'noacc':
            rows = [r for r in rows if r['kind'] != 'account']
        elif dview == 'online':
            rows = [r for r in rows if r['online']]
        elif dview == 'paid':
            rows = [r for r in rows if r['is_subscribed']]
        elif dview == 'free':
            rows = [r for r in rows if r['profile_pk'] and not r['is_subscribed']]
        elif dview == 'expiring':
            limit = timezone.localdate() + datetime.timedelta(days=7)
            rows = [r for r in rows if r['is_subscribed'] and r['valid_until']
                    and r['valid_until'] <= limit]

        if q:
            needle = q.lower()
            rows = [r for r in rows
                    if needle in (r['id'] or '').lower()
                    or needle in (r['email'] or '').lower()
                    or needle in (r['model'] or '').lower()
                    or needle in (r['os'] or '').lower()]

        epoch = timezone.make_aware(datetime.datetime(1970, 1, 1))
        rows.sort(key=lambda r: (r['last_seen'] or epoch), reverse=True)

        expiring_limit = timezone.localdate() + datetime.timedelta(days=7)

        def _expiring(r):
            return r['is_subscribed'] and r['valid_until'] and r['valid_until'] <= expiring_limit

        chips = {
            'all': len(merged),
            'online': sum(1 for r in merged if r['online']),
            'paid': sum(1 for r in merged if r['is_subscribed']),
            'free': sum(1 for r in merged if r['profile_pk'] and not r['is_subscribed']),
            'noacc': sum(1 for r in merged if r['kind'] != 'account'),
            'expiring': sum(1 for r in merged if _expiring(r)),
        }

        if request.GET.get('export') == 'csv':
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = (
                'attachment; filename="devices-app-%s.csv"' % selected_app.id)
            writer = csv.writer(response)
            writer.writerow(['device id', 'model', 'os', 'account', 'plan', 'valid until',
                             'online', 'ip', 'first registered', 'last seen',
                             'total visits', 'visits today'])
            for r in rows:
                writer.writerow([
                    r['id'], r['model'], r['os'], r['email'], r['plan'],
                    r['valid_until'] or '', 'yes' if r['online'] else 'no', r['ip'],
                    r['registered'] or '', r['last_seen'] or '',
                    '' if r['total'] is None else r['total'],
                    '' if r['today'] is None else r['today']])
            return response

        unique_page = Paginator(rows, 25).get_page(request.GET.get('upage') or 1)
        page_rows = list(unique_page.object_list)

        # The newest few things each account did, from the heartbeat history.
        recent = {}
        page_emails = [r['email'] for r in page_rows if r['email']]
        if page_emails:
            for entry in (HeartbeatActivity.objects.filter(email__in=page_emails)
                          .order_by('-created_at')[:25 * 4]):
                bucket = recent.setdefault(entry.email, [])
                if len(bucket) < 3:
                    bucket.append(entry)
        for row in page_rows:
            row['recent'] = recent.get(row['email'], [])

        ctx.update({'unique_rows': page_rows, 'unique_page': unique_page,
                    'devices_view': dview, 'device_chips': chips})


    elif tab == 'visits':
        # ---------------------------------------------------------------- visits
        # The analytics page's "Recent Visits" plus its "Full Request Debug"
        # expander, both moved here so the endpoint page stays a dashboard.
        # visits_count_qs (built in the shared block above, for the header badge)
        # already carries the search filter.
        visits_qs = visits_count_qs.select_related('device')
        visits_page = Paginator(visits_qs.order_by('-visited_at'), 25).get_page(
            request.GET.get('vpage') or 1)

        ctx['visits_page'] = visits_page

    else:  # signed-out sessions
        # Still using the app as a guest (heartbeat says is_logged_in=False).
        # ------------------------------------------------- sessions signed out of the app
        # Each heartbeat row already carries the whole live picture (device,
        # IP, screen, the title being played, its position, the app's recent
        # actions), so the panel reads the row and derives the few display
        # values here rather than storing another copy of them.
        def _pct(position, duration):
            try:
                if not duration:
                    return 0
                return max(0, min(100, int(position * 100 / duration)))
            except (TypeError, ValueError, ZeroDivisionError):
                return 0

        signed_out_qs = UserHeartbeat.objects.filter(is_logged_in=False)
        signed_out_online = signed_out_qs.filter(
            last_seen_at__gte=timezone.now()
            - timezone.timedelta(seconds=UserHeartbeat.ONLINE_WINDOW_SECONDS)).count()

        signed_out = []
        for h in signed_out_qs.order_by('-last_seen_at')[:500]:
            actions = [str(a) for a in (h.recent_actions or []) if str(a).strip()]
            signed_out.append({
                'hb': h,
                'email': h.email,
                'online': h.is_online,
                'ip': h.ip_address or '',
                'pct': _pct(h.playback_position_ms, h.duration_ms),
                'watched_min': int((h.playback_position_ms or 0) // 60000),
                'total_min': int((h.duration_ms or 0) // 60000),
                'actions': actions,
                'last_action': actions[0] if actions else '',
            })

        signed_out_page = Paginator(signed_out, 25).get_page(request.GET.get('spage') or 1)
        ctx['signed_out'] = list(signed_out_page.object_list)
        ctx['signed_out_page'] = signed_out_page
        ctx['signed_out_total'] = signed_out_qs.count()
        ctx['signed_out_online'] = signed_out_online

    if partial:
        # The tab bar asked for this one list: send the markup and nothing else.
        return HttpResponse(
            render_to_string('core/_users_full_panels.html', ctx, request=request))
    return render(request, 'core/android_app_users_full.html', ctx)


def _p95_latency_ms(hours=24, limit=5000):
    """95th-percentile response time, when the request log records one."""
    from .models import RequestLog

    candidate = next((f.name for f in RequestLog._meta.fields
                      if f.name in ('duration_ms', 'response_ms', 'elapsed_ms',
                                    'response_time_ms', 'duration')), '')
    if not candidate:
        return None
    since = timezone.now() - timezone.timedelta(hours=hours)
    values = [v for v in RequestLog.objects.filter(created_at__gte=since)
              .values_list(candidate, flat=True)[:limit]
              if isinstance(v, (int, float))]
    if not values:
        return 0
    values.sort()
    index = max(0, min(len(values) - 1, int(len(values) * 0.95) - 1))
    return int(values[index])


@login_required
@user_passes_test(is_staff_or_superuser)
def data_health(request):
    """Data health: the checks an operator would otherwise run by hand.

    Each card is one query against data that is already stored, with the command
    that repairs it where a repair exists. Nothing runs automatically from here —
    the buttons call the same management commands the cron jobs use.
    """
    import io

    from django.core.management import call_command
    from django.db.models import Count

    from .models import (AccountDevice, AndroidApp, AndroidAppDevice,
                         AndroidAppDeviceVisit, AndroidAppLog, ErrorLog,
                         SyncedUser, UserHeartbeat)
    from .health import ACK_WINDOW, acknowledged_slugs, check_rows

    actions = {
        'expire_subscriptions': ('Lapsed subscriptions cleared', ()),
        'link_devices': ('Install records linked to accounts', ('--apply',)),
        'prune_app_data': ('Old visits, logs and guests pruned', ('--apply',)),
        'sync_verification': ('Verification flags repaired from the login accounts', ('--apply',)),
    }

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip()
        slug = (request.POST.get('slug') or '').strip()
        if action in ('acknowledge', 'unacknowledge'):
            from .models import HealthCheckAck
            if slug not in {row[0] for row in check_rows()}:
                messages.error(request, 'Unknown check.')
            elif action == 'acknowledge':
                HealthCheckAck.objects.update_or_create(
                    slug=slug,
                    defaults={'acknowledged_at': timezone.now(),
                              'acknowledged_by': (request.user.get_username() or '')[:150]})
                cache.delete('data_health_alert_v1')
                messages.success(request, 'Dismissed for 24 hours; the badge ignores it until then.')
            else:
                HealthCheckAck.objects.filter(slug=slug).delete()
                cache.delete('data_health_alert_v1')
                messages.success(request, 'Dismissal cleared.')
        elif action in actions:
            label, flags = actions[action]
            try:
                call_command(action, *flags, stdout=io.StringIO())
                cache.delete('data_health_alert_v1')
                messages.success(request, label + '.')
            except Exception as exc:
                messages.error(request, 'Could not run %s: %s' % (action, exc))
        else:
            messages.error(request, 'Unknown action.')
        return redirect('data_health')

    now = timezone.now()
    today = timezone.localdate()
    day_ago = now - timezone.timedelta(hours=24)
    month_ago = now - timezone.timedelta(days=90)

    recent_views = set(ErrorLog.objects.filter(created_at__gte=day_ago)
                       .values_list('view_name', flat=True).distinct())
    earlier_views = set(ErrorLog.objects.filter(created_at__lt=day_ago)
                        .values_list('view_name', flat=True).distinct())


    meta = {
        'expired': ('expire_subscriptions',
                    'The app keeps granting paid access until these are cleared.'),
        'unverified': ('sync_verification',
                       'Playback gating reads this flag; it is rebuilt from the login account.'),
        'nodevice': ('', 'They cannot be joined to an install record.'),
        'shared': ('', 'Usually a reinstall that reused an id.'),
        'unlinked': ('link_devices',
                     'Tries the id first, then the recent addresses of each install.'),
        'guests': ('prune_app_data', 'Pruned after 30 days of inactivity.'),
        'visits': ('prune_app_data', 'Retention keeps the tables small.'),
        'logs': ('prune_app_data', ''),
        'errors': ('', '%d view(s) failing now that were not failing before.'
                       % len(recent_views - earlier_views)),
    }
    acknowledged = acknowledged_slugs()
    checks = []
    for slug, label, count in check_rows():
        action, hint = meta.get(slug, ('', ''))
        dismissed_at = acknowledged.get(slug)
        checks.append({'slug': slug, 'label': label, 'count': count,
                       'action': action, 'hint': hint,
                       'acknowledged_at': dismissed_at,
                       'ack_expires': (dismissed_at + ACK_WINDOW) if dismissed_at else None})

    builds = []
    for app_obj in AndroidApp.objects.all():
        versions = list(UserHeartbeat.objects.exclude(app_version='')
                        .values('app_version').annotate(n=Count('id')).order_by('-n')[:5])
        builds.append({'app': app_obj, 'versions': versions,
                       'required': app_obj.allowed_build_id})

    groups = list(ErrorLog.objects.filter(created_at__gte=day_ago)
                  .values('kind', 'view_name', 'status_code')
                  .annotate(n=Count('id')).order_by('-n')[:8])

    silent = list(UserHeartbeat.objects.filter(
        is_playing=True,
        last_seen_at__lt=now - timezone.timedelta(minutes=30),
        last_seen_at__gte=now - timezone.timedelta(hours=24)).order_by('-last_seen_at')[:20])

    multi_device = []
    for row in (AccountDevice.objects.values('email').order_by()
                .annotate(devices=Count('id')).filter(devices__gt=1)
                .order_by('-devices')[:15]):
        multi_device.append({
            'email': row['email'],
            'devices': list(AccountDevice.objects.filter(email=row['email'])
                            .order_by('-last_seen_at')[:4]),
        })

    return render(request, 'core/data_health.html', {
        'checks': checks, 'builds': builds, 'groups': groups, 'silent': silent,
        'account_devices': AccountDevice.objects.count(),
        'latency_ms': _p95_latency_ms(),
        'multi_device': multi_device,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_failed_attempts(request, app_id):
    android_app = get_object_or_404(AndroidApp, id=app_id)
    failed_attempts = android_app.failed_attempts.all()
    return render(request, 'core/android_app_failed_attempts.html', {
        'android_app': android_app,
        'failed_attempts': failed_attempts,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def api_documentation_view(request):
    """Admin page: documentation of every public API endpoint + connection guide."""
    apps = AndroidApp.objects.all().order_by('name')
    return render(request, 'core/api_documentation.html', {
        'apps': apps,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def android_user_sync_reference(request):
    """Display registered Android apps for the user sync endpoint."""
    apps = AndroidApp.objects.all().order_by('name')
    totals = AndroidAppAccessLog.objects.values('android_app').annotate(total=models.Sum('connection_count'))
    totals_map = {item['android_app']: item['total'] or 0 for item in totals}

    return render(request, 'core/android_user_sync_reference.html', {
        'apps': apps,
        'totals_map': totals_map,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def ajax_android_app_dashboard(request, app_id):
    selected_app = get_object_or_404(AndroidApp, id=app_id)
    # Skip auto-clean on AJAX polls — only run on full page loads to avoid heavy delete on every 5s poll
    # selected_app.clean_old_analytics_data()
    logs = selected_app.access_logs.order_by('access_date')
    chart_labels = [log.access_date.strftime('%Y-%m-%d') for log in logs]
    chart_values = [log.connection_count for log in logs]
    build_summary = list(
        selected_app.build_logs.values('build_identifier').annotate(
            total_connections=models.Sum('connection_count')
        ).order_by('-total_connections', 'build_identifier')
    )
    build_chart_labels = [item['build_identifier'] for item in build_summary[:10]]
    build_chart_values = [item['total_connections'] or 0 for item in build_summary[:10]]
    failed_attempts = list(selected_app.failed_attempts.all()[:5].values(
        'attempted_at', 'failure_reason', 'ip_address', 'request_identity', 'build_identifier'
    ))
    # Convert failed_attempts datetime to iso string
    for attempt in failed_attempts:
        attempt['attempted_at'] = timezone.localtime(attempt['attempted_at']).isoformat()
        attempt['failure_reason_display'] = dict(AndroidAppFailedAttempt.FAILURE_REASON_CHOICES).get(attempt['failure_reason'])

    # The devices / visits lists these polls used to carry are gone from the
    # page (and their full_request dumps are the bulkiest part of the payload),
    # so the poll only sends what the dashboard still renders.
    unique_logs = selected_app.daily_unique_visitors.order_by('access_date')
    unique_chart_labels = [log.access_date.strftime('%Y-%m-%d') for log in unique_logs]
    unique_chart_values = [log.unique_visitor_count for log in unique_logs]

    return JsonResponse({
        'total_connections': selected_app.total_connections,
        'total_unique_visitors': selected_app.devices.count(),
        'chart_labels': chart_labels,
        'chart_values': chart_values,
        'unique_chart_labels': unique_chart_labels,
        'unique_chart_values': unique_chart_values,
        'build_chart_labels': build_chart_labels,
        'build_chart_values': build_chart_values,
        'failed_attempts': failed_attempts,
        'last_accessed_at': timezone.localtime(selected_app.last_accessed_at).isoformat() if selected_app.last_accessed_at else None
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def android_device_logs(request, app_id, user_id):
    """Full page showing all request logs for a specific device."""
    selected_app = get_object_or_404(AndroidApp, id=app_id)
    device = get_object_or_404(AndroidAppDevice, android_app=selected_app, user_id=user_id)
    visits = device.visits.select_related('android_app').order_by('-visited_at')
    
    # Format full_request for each visit
    visit_list = []
    for v in visits:
        full_request = v.full_request or ''
        if full_request:
            try:
                full_request = json.dumps(json.loads(full_request), indent=2, default=str)
            except (json.JSONDecodeError, TypeError):
                pass
        visit_list.append({
            'visit': v,
            'full_request': full_request,
        })
    
    return render(request, 'core/android_device_logs.html', {
        'selected_app': selected_app,
        'device': device,
        'visit_list': visit_list,
    })


@csrf_exempt
@require_http_methods(['POST'])
def android_app_log_endpoint(request, app_slug):
    """
    POST endpoint for Android apps to send logs.
    Auth: Basic auth with the app's access_username / access_password.
    Body: JSON with at minimum { "message": "..." }
    Optional: level, tag, data (JSON object), user_id, build_identifier,
              device_model, os_version
    """
    try:
        android_app = AndroidApp.objects.get(slug=app_slug, is_active=True)
    except AndroidApp.DoesNotExist:
        try:
            inactive_app = AndroidApp.objects.get(slug=app_slug)
            return JsonResponse({'error': 'App is inactive'}, status=403)
        except AndroidApp.DoesNotExist:
            return JsonResponse({'error': 'App not found'}, status=404)

    # Check if log collection is enabled for this app
    if not android_app.log_collection_enabled:
        return JsonResponse({'error': 'Log collection is disabled for this app'}, status=403)

    # Basic auth
    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth_header.startswith('Basic '):
        return JsonResponse({'error': 'Authentication required'}, status=403,
                            headers={'WWW-Authenticate': 'Basic realm="Android Log API"'})
    try:
        decoded = base64.b64decode(auth_header.split(' ', 1)[1]).decode('utf-8')
        username, password = decoded.split(':', 1)
    except Exception:
        return JsonResponse({'error': 'Invalid credentials format'}, status=403)
    if username != android_app.access_username or password != android_app.access_password:
        return JsonResponse({'error': 'Invalid credentials'}, status=403)

    # Parse body
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    message = payload.get('message', '')
    if not message:
        return JsonResponse({'error': 'message is required'}, status=400)

    level = payload.get('level', 'info')
    tag = payload.get('tag', '')
    log_data = payload.get('data', '')
    if isinstance(log_data, (dict, list)):
        log_data = json.dumps(log_data, default=str)
    user_id = (payload.get('user_id') or request.headers.get('X-Android-User-ID', '')).strip()
    build_identifier = (payload.get('build_identifier') or request.headers.get('X-Android-Build', '')).strip()
    device_model = (payload.get('device_model') or request.headers.get('X-Android-Device', '')).strip()
    os_version = (payload.get('os_version') or request.headers.get('X-Android-OS-Version', '')).strip()
    ip_address = request.META.get('REMOTE_ADDR', None)

    # Resolve device
    device_obj = None
    if user_id:
        device_obj, _ = AndroidAppDevice.objects.get_or_create(
            android_app=android_app, user_id=user_id,
            defaults={'device_model': device_model, 'os_version': os_version}
        )

    AndroidAppLog.objects.create(
        android_app=android_app,
        device=device_obj,
        user_id=user_id,
        level=level,
        tag=tag,
        message=message,
        data=log_data,
        build_identifier=build_identifier,
        device_model=device_model,
        os_version=os_version,
        ip_address=ip_address,
    )
    return JsonResponse({'ok': True}, status=201)


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_logs_page(request, app_id):
    """Admin page to view all logs for an Android app."""
    selected_app = get_object_or_404(AndroidApp, id=app_id)

    # Filters
    level_filter = request.GET.get('level', '')
    tag_filter = request.GET.get('tag', '').strip()
    search = request.GET.get('q', '').strip()
    user_filter = request.GET.get('user_id', '').strip()

    logs = selected_app.app_logs.all()
    if level_filter:
        logs = logs.filter(level=level_filter)
    if tag_filter:
        logs = logs.filter(tag__icontains=tag_filter)
    if search:
        logs = logs.filter(models.Q(message__icontains=search) | models.Q(data__icontains=search))
    if user_filter:
        logs = logs.filter(user_id=user_filter)

    # Get unique tags for the filter dropdown
    all_tags = (selected_app.app_logs
                .values_list('tag', flat=True)
                .distinct()
                .order_by('tag'))

    # Format full_request for each log
    log_list = logs[:200]  # limit for performance

    return render(request, 'core/android_app_logs.html', {
        'selected_app': selected_app,
        'logs': log_list,
        'all_tags': [t for t in all_tags if t],
        'level_filter': level_filter,
        'tag_filter': tag_filter,
        'search': search,
        'user_filter': user_filter,
        'total_count': logs.count(),
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_log_delete(request, app_id, log_id):
    """Delete a single log entry."""
    selected_app = get_object_or_404(AndroidApp, id=app_id)
    log = get_object_or_404(AndroidAppLog, id=log_id, android_app=selected_app)
    log.delete()
    messages.success(request, 'Log entry deleted.')
    return redirect('android_app_logs_page', app_id=app_id)


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_logs_clear(request, app_id):
    """Delete all logs for an Android app."""
    selected_app = get_object_or_404(AndroidApp, id=app_id)
    count = selected_app.app_logs.count()
    selected_app.app_logs.all().delete()
    messages.success(request, f'Deleted {count} log entries.')
    return redirect('android_app_logs_page', app_id=app_id)


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_clear_data(request, app_id, data_type):
    """Clear analytics data for an Android app."""
    selected_app = get_object_or_404(AndroidApp, id=app_id)
    if data_type == 'failed_attempts':
        count = selected_app.failed_attempts.count()
        selected_app.failed_attempts.all().delete()
        messages.success(request, f'Deleted {count} failed attempt records.')
    elif data_type == 'device_visits':
        count = selected_app.device_visits.count()
        selected_app.device_visits.all().delete()
        messages.success(request, f'Deleted {count} device visit records.')
    elif data_type == 'devices':
        count = selected_app.devices.count()
        selected_app.devices.all().delete()
        messages.success(request, f'Deleted {count} device records.')
    elif data_type == 'access_logs':
        count = selected_app.access_logs.count()
        selected_app.access_logs.all().delete()
        messages.success(request, f'Deleted {count} access log records.')
    elif data_type == 'build_logs':
        count = selected_app.build_logs.count()
        selected_app.build_logs.all().delete()
        messages.success(request, f'Deleted {count} build log records.')
    elif data_type == 'daily_unique':
        count = selected_app.daily_unique_visitors.count()
        selected_app.daily_unique_visitors.all().delete()
        messages.success(request, f'Deleted {count} daily visitor records.')
    elif data_type == 'all_analytics':
        counts = {
            'logs': selected_app.app_logs.count(),
            'failed_attempts': selected_app.failed_attempts.count(),
            'device_visits': selected_app.device_visits.count(),
            'devices': selected_app.devices.count(),
            'access_logs': selected_app.access_logs.count(),
            'build_logs': selected_app.build_logs.count(),
            'daily_unique': selected_app.daily_unique_visitors.count(),
        }
        selected_app.app_logs.all().delete()
        selected_app.failed_attempts.all().delete()
        selected_app.device_visits.all().delete()
        selected_app.devices.all().delete()
        selected_app.access_logs.all().delete()
        selected_app.build_logs.all().delete()
        selected_app.daily_unique_visitors.all().delete()
        total = sum(counts.values())
        messages.success(request, f'Deleted all analytics data ({total} total records).')
    else:
        messages.error(request, 'Unknown data type.')
    return redirect('android_app_dashboard_detail', app_id=app_id)


@csrf_exempt
@require_http_methods(['GET'])
def android_app_endpoint(request, app_slug):
    # Helper function to parse allowed values (supports comma-separated lists and ranges like 225-250)
    def parse_allowed_values(allowed_str):
        allowed = set()
        if not allowed_str:
            return allowed
        
        # Split by commas and clean up
        parts = [p.strip() for p in allowed_str.split(',') if p.strip()]
        
        for part in parts:
            # Check if it's a range (like 225-250 or #225-#250)
            if '-' in part:
                # Strip any # prefixes
                range_parts = [p.strip().lstrip('#') for p in part.split('-', 1) if p.strip()]
                if len(range_parts) == 2:
                    try:
                        start = int(range_parts[0])
                        end = int(range_parts[1])
                        for num in range(start, end + 1):
                            allowed.add(str(num))
                            allowed.add(f"#{num}")
                    except ValueError:
                        # If not numeric, just add as-is
                        allowed.add(part)
            else:
                # Single value, add both with and without #
                stripped = part.lstrip('#')
                allowed.add(part)
                if stripped != part:
                    allowed.add(stripped)
        
        return allowed
    
    # Helper function to log failed attempts
    def log_failed_attempt(reason, android_app_obj=None, req_identity='', build_id=''):
        ip = request.META.get('REMOTE_ADDR', '')
        user_agent = request.META.get('HTTP_USER_AGENT', '')
        AndroidAppFailedAttempt.objects.create(
            android_app=android_app_obj,
            app_slug=app_slug,
            failure_reason=reason,
            request_identity=req_identity,
            build_identifier=build_id,
            ip_address=ip,
            user_agent=user_agent
        )
    
    # Try to get the app first
    try:
        android_app = AndroidApp.objects.get(slug=app_slug, is_active=True)
    except AndroidApp.DoesNotExist:
        # Check if app exists but is inactive
        try:
            inactive_app = AndroidApp.objects.get(slug=app_slug)
            log_failed_attempt('app_inactive', android_app_obj=inactive_app)
            return HttpResponseForbidden('App is inactive')
        except AndroidApp.DoesNotExist:
            log_failed_attempt('app_not_found')
            return HttpResponseNotFound('App not found')

    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth_header.startswith('Basic '):
        log_failed_attempt('auth_missing', android_app_obj=android_app)
        response = HttpResponseForbidden('Authentication required')
        response['WWW-Authenticate'] = 'Basic realm="Android App Endpoint"'
        return response

    try:
        decoded = base64.b64decode(auth_header.split(' ', 1)[1]).decode('utf-8')
        username, password = decoded.split(':', 1)
    except Exception:
        log_failed_attempt('auth_invalid_format', android_app_obj=android_app)
        return HttpResponseForbidden('Invalid credentials format')

    if username != android_app.access_username or password != android_app.access_password:
        log_failed_attempt('auth_invalid_creds', android_app_obj=android_app)
        return HttpResponseForbidden('Invalid credentials')

    request_identity = (
        request.headers.get('X-Android-App')
        or request.headers.get('X-Android-Package')
        or request.GET.get('app')
        or request.GET.get('package')
        or ''
    ).strip()
    
    allowed_endpoints = parse_allowed_values(android_app.allowed_endpoint)
    # If allowed_endpoint is not empty, check if request_identity is in allowed_endpoints
    if allowed_endpoints and request_identity not in allowed_endpoints:
        log_failed_attempt('identity_invalid', android_app_obj=android_app, req_identity=request_identity)
        return HttpResponseForbidden('Endpoint identity is not allowed for this app')
    
    # Also handle the case where allowed_endpoint is empty (allow all?) Wait, original code checked if not request_identity OR not equal!
    if not allowed_endpoints:
        # Original behavior: if allowed_endpoint is empty, require request_identity to be empty? Or let's keep original behavior but allow the parsing?
        # Wait let's check original code: "if not request_identity or request_identity != (android_app.allowed_endpoint or '').strip():"
        # So let's preserve that: if allowed_endpoint is empty, then only allow request_identity to be empty?
        if request_identity:
            log_failed_attempt('identity_invalid', android_app_obj=android_app, req_identity=request_identity)
            return HttpResponseForbidden('Endpoint identity is not allowed for this app')

    build_identifier = (
        request.headers.get('X-Android-Build')
        or request.GET.get('build')
        or 'unknown-build'
    ).strip() or 'unknown-build'

    allowed_build_ids = parse_allowed_values(android_app.allowed_build_id)
    if allowed_build_ids and build_identifier not in allowed_build_ids:
        response_payload = {
            'status': 'update_required',
            'message': 'Update the app with the latest APK URL.',
            'expected_build_id': android_app.allowed_build_id,
        }
        if android_app.apk_file:
            # Route through the logging download view (not raw /media/) so the
            # download is recorded and can later be matched to this app.
            response_payload['apk_url'] = request.build_absolute_uri(
                reverse('apk_download', args=[android_app.id])
            )
        return JsonResponse(response_payload, status=426)

    today = timezone.localdate()
    log_entry, _ = AndroidAppAccessLog.objects.get_or_create(
        android_app=android_app,
        access_date=today,
        defaults={'connection_count': 0},
    )
    log_entry.connection_count += 1
    log_entry.save(update_fields=['connection_count', 'last_accessed_at'])

    build_log_entry, _ = AndroidAppBuildLog.objects.get_or_create(
        android_app=android_app,
        build_identifier=build_identifier,
        access_date=today,
        defaults={'connection_count': 0},
    )
    build_log_entry.connection_count += 1
    build_log_entry.save(update_fields=['connection_count', 'last_accessed_at'])

    android_app.total_connections = (android_app.total_connections or 0) + 1
    android_app.last_accessed_at = timezone.now()
    android_app.save(update_fields=['total_connections', 'last_accessed_at', 'updated_at'])
    
    # Handle Android ID tracking for unique visitors
    # Read user_id from headers first, then query params
    user_id = (
        request.headers.get('X-Android-User-ID') or 
        request.GET.get('user_id') or 
        ''
    ).strip()
    
    # Read device and os from headers or query params
    device_model = (
        request.headers.get('X-Android-Device') or 
        request.GET.get('device') or 
        ''
    ).strip()
    
    os_version = (
        request.headers.get('X-Android-OS-Version') or 
        request.GET.get('os') or 
        ''
    ).strip()
    
    # Behind nginx REMOTE_ADDR is the proxy; use the forwarded client IP so
    # a download and the app's later API hit can be matched by one address.
    ip_address = _normalize_ip(_get_client_ip(request))

    # If this IP downloaded the APK earlier, flag it as opened in the app.
    _mark_apk_opened(
        ip_address, user_id=user_id, device_model=device_model,
        os_version=os_version, build_identifier=build_identifier,
    )

    if user_id:
        # Get or create device
        device, created = AndroidAppDevice.objects.get_or_create(
            android_app=android_app,
            user_id=user_id
        )
        # Update device model and os version if they changed or are new
        if device_model and device.device_model != device_model:
            device.device_model = device_model
            device.save(update_fields=['device_model', 'last_seen_at'])
        if os_version and device.os_version != os_version:
            device.os_version = os_version
            device.save(update_fields=['os_version', 'last_seen_at'])
        # Increment total visits for device
        device.total_visits += 1
        device.save(update_fields=['total_visits', 'last_seen_at'])

        # Keep the install record joined to the account that owns it, so the
        # admin list can show one device with its account and its visits.
        account_key = _resolve_account_key(user_id, ip_address)
        if account_key and device.account_key != account_key:
            device.account_key = account_key
            device.save(update_fields=['account_key', 'last_seen_at'])
        
        # Build full request details for debugging
        full_request_data = {
            'method': request.method,
            'url': request.build_absolute_uri(),
            'path': request.path,
            'query_params': dict(request.GET),
            'headers': {
                k: v for k, v in request.headers.items()
                if not k.lower().startswith('cookie')
            },
            'user_id': user_id,
            'device_model': device_model,
            'os_version': os_version,
            'build_identifier': build_identifier,
            'request_identity': request_identity,
            'ip_address': ip_address,
            'timestamp': timezone.localtime().isoformat(),
        }
        
        # Record individual visit
        AndroidAppDeviceVisit.objects.create(
            device=device,
            android_app=android_app,
            build_identifier=build_identifier,
            request_identity=request_identity,
            ip_address=ip_address,
            device_model=device_model,
            os_version=os_version,
            full_request=json.dumps(full_request_data, indent=2, default=str)
        )
        
        # Check if this is a new unique visitor for today
        today_visits = AndroidAppDeviceVisit.objects.filter(
            android_app=android_app,
            visited_at__date=today,
            device=device
        ).count()
        
        if today_visits == 1:
            # First visit today, increment unique count
            unique_visitor_log, _ = AndroidAppDailyUniqueVisitor.objects.get_or_create(
                android_app=android_app,
                access_date=today,
                defaults={'unique_visitor_count': 0}
            )
            unique_visitor_log.unique_visitor_count += 1
            unique_visitor_log.save(update_fields=['unique_visitor_count', 'updated_at'])

    # Build movie_servers and series_servers from PlayerConfiguration (cached for 5 min)
    cache_key = f'android_endpoint_payload_{android_app.slug}'
    cached_payload = cache.get(cache_key)
    if cached_payload:
        # Still track device visit but serve cached config
        return JsonResponse(cached_payload, safe=isinstance(cached_payload, dict))
    
    android_players = PlayerConfiguration.objects.filter(use_for_android=True, is_active=True).order_by('order', 'name')
    movie_servers = []
    series_servers = []
    for idx, player in enumerate(android_players, start=1):
        # Get movie URL
        if player.media_type in ['movie', 'both']:
            movie_url = player.custom_movie_iframe_url or player.custom_iframe_url
            if not movie_url:
                # Use default Vidking movie URL as fallback
                movie_url = "https://www.vidking.net/embed/movie/{id}"
                if player.player_color:
                    movie_url += f"?color={player.player_color}"
            # Replace all relevant placeholders with {id}
            movie_url = movie_url.replace("{tmdb_id}", "{id}").replace("{content_id}", "{id}").replace("{imdb_id}", "{id}")
            movie_servers.append({
                "name": f"Player {idx}",
                "url_template": movie_url
            })
        # Get series URL
        if player.media_type in ['tv', 'both']:
            tv_url = player.custom_tv_iframe_url or player.custom_iframe_url
            if not tv_url:
                # Use default Vidking TV URL as fallback
                tv_url = "https://www.vidking.net/embed/tv/{id}/{season}/{episode}"
                if player.player_color:
                    tv_url += f"?color={player.player_color}"
            # Replace all relevant placeholders with {id}
            tv_url = tv_url.replace("{tmdb_id}", "{id}").replace("{content_id}", "{id}").replace("{imdb_id}", "{id}")
            series_servers.append({
                "name": f"Player {len(series_servers) + 1}",  # separate counter for series
                "url_template": tv_url
            })
    
    # Get Android ads
    android_ads = Ad.objects.filter(use_for_android=True, is_active=True).order_by('order', 'name')
    ads_list = []
    for ad in android_ads:
        ads_list.append({
            'name': ad.name,
            'provider': ad.provider,
            'script': ad.script,
            'image_url': ad.image_url,
            'link_url': ad.link_url,
            'alt_text': ad.alt_text,
            'clicks_required': ad.clicks_required,
            'order': ad.order
        })
    
    # Update the payload
    response_payload = android_app.json_payload.copy() if isinstance(android_app.json_payload, dict) else android_app.json_payload
    if isinstance(response_payload, dict):
        response_payload['movie_servers'] = movie_servers
        response_payload['series_servers'] = series_servers
        response_payload['ads'] = ads_list
    # Cache the response payload for 5 minutes
    cache.set(cache_key, response_payload, 300)
    return JsonResponse(response_payload, safe=isinstance(response_payload, dict))


def ajax_login(request):
    if request.method == 'POST':
        username = request.POST.get('username', '')
        password = request.POST.get('password', '')
        # Try standard auth first
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            _record_session(user, 'web', request)
            _lift_block_after_auth(request)
            return JsonResponse({'success': True, 'message': 'Login successful'})
        # If no password provided, try email-based login for synced users
        if not password and '@' in username:
            try:
                user = User.objects.get(email__iexact=username)
                if not user.has_usable_password():
                    # First web login: set a password
                    new_pass = request.POST.get('new_password', '')
                    if new_pass and len(new_pass) >= 6:
                        user.set_password(new_pass)
                        user.save()
                        login(request, user)
                        _record_session(user, 'web', request)
                        _lift_block_after_auth(request)
                        return JsonResponse({'success': True, 'message': 'Password set and login successful'})
                    return JsonResponse({
                        'success': False,
                        'message': 'first_login',
                        'detail': 'This account was created from Android. Please set a password.',
                        'email': user.email,
                    })
                else:
                    return JsonResponse({'success': False, 'message': 'Invalid password'})
            except User.DoesNotExist:
                pass
        return JsonResponse({'success': False, 'message': 'Invalid username or password'})
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


def _issue_verification_email(django_user, request, display_name=''):
    """Create an EmailVerification row (6-digit OTP) and email just the code.

    Used by web + Android registration and by resend-verification. The email
    carries the OTP only — no site link — and both clients finish registration
    by typing that code (POST /ajax/verify-email-otp/ on the web,
    POST /api/user/verify-email/ {email, otp} in the app). A link token is still
    stored for backward compatibility, it is simply never mailed.
    """
    from .models import EmailVerification, SiteSettings
    token = secrets.token_hex(32)
    otp = EmailVerification.generate_otp()
    EmailVerification.objects.create(user=django_user, token=token, otp=otp)
    brand = SiteSettings.get_settings().brand_name if hasattr(SiteSettings, 'get_settings') else 'NewMovies'
    try:
        send_configured_email(
            subject=f'Verify your email - {brand}',
            message=(
                f'Hi {display_name or django_user.first_name or django_user.username},\r\n\r\n'
                f'Your verification code is: {otp}\r\n\r\n'
                f'This code is valid for 10 minutes. Enter it on the verification screen to finish creating your account.\r\n\r\n'
                f'If you did not register with {brand}, please ignore this email.'
            ),
            recipient_list=[django_user.email],
            purpose='verification',
            fail_silently=True,
        )
    except Exception as e:
        logger.warning(f'Failed to send verification email: {e}', exc_info=True)
    return token, otp


def _issue_password_reset_email(django_user, request):
    """Issue a 6-digit reset OTP and email just the code (no site link).

    Shared by the website forgot-password form and the Android API
    (forgot-password) so every trigger mails the same content and the
    OTP is always stored (PasswordResetOTP). Both clients reset with the
    code alone (POST /ajax/reset-password-otp/ on the web,
    /api/user/reset-password/ {email, otp, newPassword} in the app).

    Returns a dict {otp, uid, token, resetUrl} so the Android API can still
    hand resetUid/resetToken straight to the app; those values are simply
    never put in the email any more.
    """
    from .models import PasswordResetOTP, SiteSettings
    otp_code = PasswordResetOTP.generate()
    PasswordResetOTP.objects.create(user=django_user, otp=otp_code)

    # Link-based token for browser users (backward compat)
    token = default_token_generator.make_token(django_user)
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode
    uid = urlsafe_base64_encode(force_bytes(django_user.pk))
    reset_url = f"{request.scheme}://{request.get_host()}/reset-password/{uid}/{token}/"

    brand = SiteSettings.get_settings().brand_name if hasattr(SiteSettings, 'get_settings') else 'NewMovies'
    try:
        send_configured_email(
            subject=f'Reset your password - {brand}',
            message=(
                f'Hi {django_user.first_name or django_user.username},\r\n\r\n'
                f'Your password reset code is: {otp_code}\r\n\r\n'
                f'This code is valid for 10 minutes. Enter it on the "Forgot password" screen in the app or website to set a new password.\r\n\r\n'
                f'If you did not request this, please ignore this email.'
            ),
            recipient_list=[django_user.email],
            purpose='password_reset',
            fail_silently=True,
        )
    except Exception as e:
        logger.warning(f'Failed to send reset email: {e}', exc_info=True)
    return {
        'otp': otp_code,
        'uid': uid,
        'token': token,
        'resetUrl': reset_url,
    }


def ajax_register(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')
        
        if not username or not email or not password:
            return JsonResponse({'success': False, 'message': 'All fields are required'})
        
        if password != confirm_password:
            return JsonResponse({'success': False, 'message': 'Passwords do not match'})
        
        if len(password) < 6:
            return JsonResponse({'success': False, 'message': 'Password must be at least 6 characters'})
        
        # Only allow Gmail and Yahoo emails
        allowed_domains = ['gmail.com', 'yahoo.com', 'yahoo.co.in']
        email_domain = email.split('@')[-1] if '@' in email else ''
        if email_domain not in allowed_domains:
            return JsonResponse({'success': False, 'message': 'We don\'t support this email. Please use Gmail or Yahoo.'})
        
        # Check if email already exists (web or Android-synced user, case-insensitive)
        existing_user = User.objects.filter(email__iexact=email).first()
        if existing_user:
            # If this user came from Android sync, tell them to use email login
            if existing_user.has_usable_password():
                return JsonResponse({'success': False, 'message': 'Email already registered. Please login instead.'})
            else:
                # Android-synced user registering — set their password and send verification
                existing_user.set_password(password)
                existing_user.first_name = username
                existing_user.is_active = False
                existing_user.save()
                _issue_verification_email(existing_user, request, display_name=username)
                _lift_block_after_auth(request)
                return JsonResponse({'success': True, 'message': 'Verification code sent. Please check your inbox.', 'requires_verification': True})
        
        if User.objects.filter(username=username).exists():
            return JsonResponse({'success': False, 'message': 'Username already taken'})
        
        # Create new user (inactive until email verified)
        user = User.objects.create_user(username=username, email=email, password=password)
        user.is_active = False
        user.save()
        _issue_verification_email(user, request, display_name=username)
        _lift_block_after_auth(request)

        return JsonResponse({'success': True, 'message': 'Verification code sent. Please check your inbox.', 'requires_verification': True})
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


def verify_email(request):
    """GET /ajax/verify-email/?token=... — verify email within 10 minutes."""
    token = request.GET.get('token', '')
    if not token:
        return render(request, 'core/email_verified.html', {'success': False, 'message': 'Invalid verification link.'})
    from .models import EmailVerification
    try:
        ev = EmailVerification.objects.select_related('user').get(token=token)
    except EmailVerification.DoesNotExist:
        return render(request, 'core/email_verified.html', {'success': False, 'message': 'Invalid verification link.'})
    if ev.verified:
        return render(request, 'core/email_verified.html', {'success': True, 'message': 'Email already verified. You can now login.'})
    if ev.is_expired:
        return render(request, 'core/email_verified.html', {'success': False, 'message': 'Verification link has expired. Please register again.'})
    ev.verified = True
    ev.save(update_fields=['verified'])
    try:
        ev.user.is_active = True
        ev.user.save(update_fields=['is_active'])
    except Exception:
        pass  # user may have been deleted
    return render(request, 'core/email_verified.html', {'success': True, 'message': 'Email verified successfully! You can now login.'})


def ajax_logout(request):
    # Flush the session completely before calling Django's logout
    try:
        request.session.flush()
    except Exception:
        pass
    logout(request)
    # If the request came from a form POST (not AJAX), redirect to homepage
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        response = redirect('/')
        response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response
    # AJAX request — return JSON
    response = JsonResponse({'success': True, 'message': 'Logged out successfully'})
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


def ajax_verify_email_otp(request):
    """POST /ajax/verify-email-otp/ — verify email with 6-digit OTP from the web.

    The registration form shows an OTP input after sending the code; this
    endpoint validates it and activates the user.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    email = request.POST.get('email', '').strip().lower()
    otp_code = request.POST.get('otp', '').strip()
    if not email or not otp_code:
        return JsonResponse({'success': False, 'message': 'Email and verification code are required'})
    from .models import EmailVerification
    rate_err = _otp_rate_limit_error(request, email, 'web_verify_otp')
    if rate_err:
        return JsonResponse({'success': False, 'message': 'Too many attempts. Please wait a few minutes and try again.'})
    django_user = User.objects.filter(email__iexact=email).first()
    if not django_user:
        _record_otp_attempt(request, email, 'web_verify_otp')
        return JsonResponse({'success': False, 'message': 'Invalid verification code.'})
    ev = EmailVerification.objects.filter(
        user=django_user, otp=otp_code, verified=False,
    ).order_by('-created_at').first()
    if not ev:
        _record_otp_attempt(request, email, 'web_verify_otp')
        return JsonResponse({'success': False, 'message': 'Invalid verification code.'})
    if ev.is_expired:
        _record_otp_attempt(request, email, 'web_verify_otp')
        return JsonResponse({'success': False, 'message': 'Verification code has expired. Please request a new code.'})
    ev.verified = True
    ev.save(update_fields=['verified'])
    django_user.is_active = True
    django_user.save(update_fields=['is_active'])
    return JsonResponse({'success': True, 'message': 'Email verified successfully! You can now login.'})


def ajax_resend_verification(request):
    """POST /ajax/resend-verification/ — resend the verification OTP to the given email."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    email = request.POST.get('email', '').strip().lower()
    if not email:
        return JsonResponse({'success': False, 'message': 'Email is required'})
    django_user = User.objects.filter(email__iexact=email, is_active=False).first()
    if not django_user:
        return JsonResponse({'success': False, 'message': 'No pending account found for this email.'})
    _issue_verification_email(django_user, request, display_name=django_user.first_name or django_user.username)
    return JsonResponse({'success': True, 'message': 'Verification code resent. Check your inbox.'})


def ajax_forgot_password(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        if not email:
            return JsonResponse({'success': False, 'message': 'Email is required'})
        try:
            user = User.objects.get(email__iexact=email)
            # Email contains both the 6-digit reset code (app/OTP use) and the browser link
            _issue_password_reset_email(user, request)
            return JsonResponse({'success': True, 'message': 'Password reset code sent to your email. Check your inbox or spam folder.'})
        except User.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Email not found'})
        except Exception as e:
            return JsonResponse({'success': False, 'message': f'Error sending email: {str(e)}'})
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


def ajax_reset_password_otp(request):
    """POST /ajax/reset-password-otp/ — reset password on the website with the
    6-digit code from the forgot-password email (no browser link needed)."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    email = request.POST.get('email', '').strip().lower()
    otp_code = request.POST.get('otp', '').strip()
    new_password = request.POST.get('new_password', '')
    confirm_password = request.POST.get('confirm_password', '')

    if not email or not otp_code or not new_password:
        return JsonResponse({'success': False, 'message': 'All fields are required'})
    if new_password != confirm_password:
        return JsonResponse({'success': False, 'message': 'Passwords do not match'})
    if len(new_password) < 6:
        return JsonResponse({'success': False, 'message': 'Password must be at least 6 characters'})

    # Rate-limit code attempts per IP and per email (10 tries / 15 min) so a
    # wrong-code flood can't brute-force the 6-digit code from the web form.
    rate_err = _otp_rate_limit_error(request, email, 'web_reset_otp')
    if rate_err:
        return JsonResponse(
            {'success': False, 'message': 'Too many attempts. Please wait a few minutes and try again.'},
            status=429,
        )

    from .models import PasswordResetOTP
    user = User.objects.filter(email__iexact=email).first()
    if not user:
        _record_otp_attempt(request, email, 'web_reset_otp')
        return JsonResponse({'success': False, 'message': 'Invalid code or email. Please request a new code.'})
    otp_obj = PasswordResetOTP.objects.filter(
        user=user, otp=otp_code, used=False,
    ).order_by('-created_at').first()
    if not otp_obj or otp_obj.is_expired:
        _record_otp_attempt(request, email, 'web_reset_otp')
        return JsonResponse({'success': False, 'message': 'Invalid or expired code. Please request a new one.'})

    _reset_otp_rate_limit(request, email, 'web_reset_otp')
    otp_obj.used = True
    otp_obj.save(update_fields=['used'])
    # Any other outstanding reset codes for this user are now stale
    PasswordResetOTP.objects.filter(user=user, used=False).update(used=True)
    user.set_password(new_password)
    user.is_active = True
    user.save(update_fields=['password', 'is_active'])
    return JsonResponse({'success': True, 'message': 'Password reset successful! Please sign in with your new password.'})


def reset_password(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is not None and default_token_generator.check_token(user, token):
        if request.method == 'POST':
            password = request.POST.get('password')
            confirm_password = request.POST.get('confirm_password')
            if password != confirm_password:
                return render(request, 'core/reset_password.html', {'error': 'Passwords do not match'})
            user.set_password(password)
            user.save()
            return redirect('index')  # Redirect to home after reset
        return render(request, 'core/reset_password.html')
    else:
        return render(request, 'core/reset_password.html', {'error': 'Invalid or expired reset link'})


def ajax_toggle_watchlist(request):
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'You need to login to add to the watch list'})

    if request.method == 'POST':
        tmdb_id = int(request.POST.get('tmdb_id'))
        media_type = request.POST.get('media_type')
        title = request.POST.get('title', '')
        poster_path = request.POST.get('poster_path', '')

        existing = WatchList.objects.filter(
            user=request.user,
            tmdb_id=tmdb_id,
            media_type=media_type
        )

        if existing.exists():
            existing.delete()
            # Remove from cloud data too
            from .models import UserCloudData
            cloud, _ = UserCloudData.objects.get_or_create(user=request.user)
            is_tv = media_type == 'tv'
            cloud.watchlist_ids = [
                w for w in (cloud.watchlist_ids or [])
                if not (w.get('mediaId') == tmdb_id and w.get('isTv') == is_tv)
            ]
            cloud.save(update_fields=['watchlist_ids'])
            return JsonResponse({'success': True, 'action': 'removed', 'message': 'Removed from watchlist'})

        from .models import UserCloudData, normalize_poster_path
        poster_path = normalize_poster_path(poster_path)
        WatchList.objects.create(
            user=request.user,
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            poster_path=poster_path
        )
        # Add to cloud data too
        cloud, _ = UserCloudData.objects.get_or_create(user=request.user)
        is_tv = media_type == 'tv'
        existing_cloud = [w for w in (cloud.watchlist_ids or []) if w.get('mediaId') == tmdb_id and w.get('isTv') == is_tv]
        if not existing_cloud:
            cloud.watchlist_ids = (cloud.watchlist_ids or []) + [{
                'mediaId': tmdb_id,
                'isTv': is_tv,
                'title': title,
                'posterPath': poster_path,
            }]
            cloud.save(update_fields=['watchlist_ids'])
        return JsonResponse({'success': True, 'action': 'added', 'message': 'Added to watchlist'})

    return JsonResponse({'success': False, 'message': 'Invalid request'})


def ajax_check_watchlist(request):
    if not request.user.is_authenticated:
        return JsonResponse({'in_watchlist': False})
    
    tmdb_id = int(request.GET.get('tmdb_id'))
    media_type = request.GET.get('media_type')
    in_watchlist = WatchList.objects.filter(
        user=request.user,
        tmdb_id=tmdb_id,
        media_type=media_type
    ).exists()
    # Also check cloud data (Android-synced watchlist)
    if not in_watchlist:
        from .models import UserCloudData
        cloud = UserCloudData.objects.filter(user=request.user).first()
        if cloud:
            is_tv = media_type == 'tv'
            in_watchlist = any(
                w.get('mediaId') == tmdb_id and w.get('isTv') == is_tv
                for w in (cloud.watchlist_ids or [])
            )
    return JsonResponse({'in_watchlist': in_watchlist})


@login_required
def watchlist(request):
    from .models import normalize_poster_path

    def _cover_url(poster_path):
        # Handle both relative paths ('/abc.jpg') and full URLs
        # ('https://image.tmdb.org/t/p/w500/abc.jpg').
        p = (poster_path or '').strip()
        if not p:
            return None
        return p if p.startswith('http') else f"{settings.TMDB_IMAGE_BASE_URL}{p}"

    watchlist_items = WatchList.objects.filter(user=request.user)
    
    movie_items = []
    series_items = []
    existing_tmdb_ids = set()
    
    for item in watchlist_items:
        existing_tmdb_ids.add((item.tmdb_id, item.media_type))
        processed_item = {
            'title': item.title,
            'slug': slugify(item.title),
            'cover_url': _cover_url(item.poster_path),
            'id': item.tmdb_id,
            'poster_path': item.poster_path,
            'vote_average': 0,
            'year': '',
            'first_air_date': '',
            'release_date': '',
            'overview': ''
        }
        if not processed_item['slug']:
            processed_item['slug'] = f"{item.media_type}-{item.tmdb_id}"
        if item.media_type == 'movie':
            movie_items.append(processed_item)
        else:
            series_items.append(processed_item)

    # Also pull items from UserCloudData (Android-synced watchlist)
    from .models import UserCloudData
    cloud, _ = UserCloudData.objects.get_or_create(user=request.user)
    for cloud_item in (cloud.watchlist_ids or []):
        mid = cloud_item.get('mediaId')
        is_tv = cloud_item.get('isTv', False)
        mtype = 'tv' if is_tv else 'movie'
        if mid and (mid, mtype) not in existing_tmdb_ids:
            # Also create the WatchList entry so it persists
            cloud_poster = normalize_poster_path(cloud_item.get('posterUrl') or cloud_item.get('posterPath') or '')
            WatchList.objects.get_or_create(
                user=request.user, tmdb_id=mid, media_type=mtype,
                defaults={
                    'title': cloud_item.get('title', ''),
                    'poster_path': cloud_poster,
                },
            )
            processed_item = {
                'title': cloud_item.get('title', ''),
                'slug': slugify(cloud_item.get('title', str(mid))),
                'cover_url': _cover_url(cloud_poster),
                'id': mid,
                'poster_path': cloud_poster,
                'vote_average': 0,
                'year': '',
                'first_air_date': '',
                'release_date': '',
                'overview': ''
            }
            if mtype == 'movie':
                movie_items.append(processed_item)
            else:
                series_items.append(processed_item)

    return render(request, 'core/watchlist.html', {
        'watchlist_items': watchlist_items,
        'movies': movie_items,
        'series': series_items
    })


def get_wikipedia_details(title, year=None):
    """Helper function to fetch Wikipedia summary and link"""
    cache_key = f"wikipedia_{title}_{year}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        # First search for the page - try multiple search terms
        search_url = "https://en.wikipedia.org/w/api.php"
        
        # Try different search combinations
        search_terms = [
            f"{title} {year} film" if year else f"{title} film",
            f"{title} {year} TV series" if year else f"{title} TV series",
            f"{title} {year}" if year else title,
            title
        ]
        
        page_title = None
        
        # Set a User-Agent header (required by Wikipedia API)
        headers = {
            "User-Agent": "MovieStreamingApp/1.0 (https://github.com/yourusername/yourrepo; your@email.com)"
        }
        
        for search_term in search_terms:
            print(f"Trying Wikipedia search for: {search_term}")
            search_params = {
                "action": "query",
                "list": "search",
                "srsearch": search_term,
                "format": "json",
                "srlimit": 10  # Get even more results to choose from
            }
            search_response = requests.get(search_url, params=search_params, headers=headers, timeout=10)
            print(f"Wikipedia API status code: {search_response.status_code}")
            print(f"Wikipedia API response text (first 500 chars): {repr(search_response.text[:500])}")
            
            try:
                search_data = search_response.json()
                search_results = search_data.get("query", {}).get("search", [])
            except Exception as e:
                print(f"Failed to parse JSON from Wikipedia API: {e}")
                print(f"Full response text: {repr(search_response.text)}")
                search_results = []
            
            if search_results:
                # Look for the best match in the search results
                best_match = None
                for result in search_results:
                    result_title = result.get("title", "")
                    result_snippet = result.get("snippet", "").lower()
                    
                    # Check if the result title contains the original title (case-insensitive)
                    title_lower = title.lower()
                    result_title_lower = result_title.lower()
                    
                    # Score the result
                    score = 0
                    if title_lower in result_title_lower:
                        score += 10
                    if "(film)" in result_title or "(TV series)" in result_title or "(TV program)" in result_title:
                        score += 8
                    if year and year in result_title:
                        score += 5
                    if title_lower in result_snippet:
                        score += 3
                    
                    print(f"Result: {result_title}, score: {score}")
                    
                    # If we have a good score, use this one
                    if score > 10:
                        best_match = result_title
                        break
                
                if not best_match:
                    best_match = search_results[0]["title"]
                
                page_title = best_match
                print(f"Found page: {page_title}")
                break
        
        if not page_title:
            print("No Wikipedia page found after trying all search terms")
            cache.set(cache_key, None, 3600)
            return None
        
        # Now get the extract for that page
        extract_params = {
            "action": "query",
            "titles": page_title,
            "prop": "extracts",
            "exintro": "true",
            "explaintext": "true",
            "format": "json"
        }
        extract_response = requests.get(search_url, params=extract_params, headers=headers, timeout=10)
        extract_data = extract_response.json()
        
        pages = extract_data.get("query", {}).get("pages", {})
        page_id = next(iter(pages.keys())) if pages else None
        
        if page_id and page_id != "-1":
            result = {
                "title": page_title,
                "summary": pages[page_id].get("extract", ""),
                "url": f"https://en.wikipedia.org/wiki/{page_title.replace(' ', '_')}"
            }
            cache.set(cache_key, result, 3600 * 24)
            print(f"Successfully found Wikipedia page: {result['url']}")
            return result
        else:
            print(f"Wikipedia page found but no extract available: {page_title}")
        
    except Exception as e:
        logger.error("fetching Wikipedia details: %s", e)
        import traceback
        print(traceback.format_exc())
    
    cache.set(cache_key, None, 3600)
    return None

def fetch_wikipedia(request):
    """AJAX endpoint to fetch Wikipedia details"""
    title = request.GET.get('title', '')
    year = request.GET.get('year', '')
    
    if not title:
        return JsonResponse({'success': False, 'error': 'No title provided'})
    
    # Clear cache for testing
    cache_key = f"wikipedia_{title}_{year}"
    cache.delete(cache_key)
    
    details = get_wikipedia_details(title, year)
    if details:
        return JsonResponse({'success': True, 'details': details})
    else:
        return JsonResponse({'success': False, 'error': 'No Wikipedia page found'})

@cache_control(public=True, max_age=86400, stale_while_revalidate=604800)
def collection_detail(request, collection_id):
    """Show all movies in a collection/franchise."""
    site_settings = SiteSettings.get_settings()
    cache_key = f"collection_{collection_id}"
    cached = cache.get(cache_key)
    if cached:
        collection_info, movies = cached
    else:
        collection_info = None
        movies = []
        try:
            api_key = TMDBApiKey.objects.filter(is_active=True).first()
            if api_key:
                client = get_data_client()
                session = getattr(client, 'session', requests)
                resp = session.get(
                    f'https://api.themoviedb.org/3/collection/{collection_id}',
                    params={'api_key': api_key.key},
                    timeout=(5, 15)
                )
                if resp.status_code == 200:
                    data = resp.json()
                    collection_info = {
                        'id': data.get('id'),
                        'name': data.get('name', ''),
                        'overview': data.get('overview', ''),
                        'poster_path': data.get('poster_path', ''),
                        'backdrop_path': data.get('backdrop_path', ''),
                    }
                    parts = data.get('parts', [])
                    for p in parts:
                        movies.append({
                            'id': p.get('id'),
                            'title': p.get('title', ''),
                            'slug': slugify(p.get('title', '')) or f"movie-{p.get('id')}",
                            'cover_url': f"{settings.TMDB_IMAGE_BASE_URL}{p['poster_path']}" if p.get('poster_path') else None,
                            'vote_average': p.get('vote_average', 0),
                            'year': (p.get('release_date') or '')[:4],
                            'release_date': p.get('release_date', ''),
                            'overview': p.get('overview', ''),
                        })
                    # Sort by release date
                    movies.sort(key=lambda x: x.get('release_date') or '9999')
                    cache.set(cache_key, (collection_info, movies), 86400)
        except Exception as e:
            logger.error('fetching collection %s: %s', collection_id, e)

    if not collection_info:
        return render(request, '404.html', status=404)

    return render(request, 'core/collection_detail.html', {
        'collection': collection_info,
        'movies': movies,
    })


@cache_control(private=True, max_age=0, must_revalidate=True)
def person_detail(request, person_id):
    """Show person's profile and filmography (movies + TV credits)."""
    cache_key = f"person_{person_id}"
    cached = cache.get(cache_key)
    if cached:
        person_info, movies, series = cached
    else:
        person_info = None
        movies = []
        series = []
        try:
            api_key = TMDBApiKey.objects.filter(is_active=True).first()
            if api_key:
                client = get_data_client()
                session = getattr(client, 'session', requests)
                resp = session.get(
                    f'https://api.themoviedb.org/3/person/{person_id}',
                    params={'api_key': api_key.key, 'append_to_response': 'combined_credits'},
                    timeout=(5, 15)
                )
                if resp.status_code == 200:
                    data = resp.json()
                    person_info = {
                        'id': data.get('id'),
                        'name': data.get('name', ''),
                        'biography': data.get('biography', ''),
                        'birthday': data.get('birthday', ''),
                        'deathday': data.get('deathday', ''),
                        'place_of_birth': data.get('place_of_birth', ''),
                        'profile_path': data.get('profile_path', ''),
                        'known_for_department': data.get('known_for_department', ''),
                        'popularity': data.get('popularity', 0),
                        'also_known_as': data.get('also_known_as', []),
                    }
                    # Process combined credits
                    credits = data.get('combined_credits', {})
                    cast_credits = credits.get('cast', [])
                    crew_credits = credits.get('crew', [])

                    # Movies (cast)
                    seen_movie_ids = set()
                    for c in cast_credits:
                        if c.get('media_type') == 'movie' and c.get('id') not in seen_movie_ids:
                            seen_movie_ids.add(c['id'])
                            movies.append({
                                'id': c.get('id'),
                                'title': c.get('title', ''),
                                'character': c.get('character', ''),
                                'poster_path': c.get('poster_path', ''),
                                'release_date': c.get('release_date', ''),
                                'vote_average': c.get('vote_average', 0),
                                'department': 'Acting',
                            })
                    # Movies (crew)
                    for c in crew_credits:
                        if c.get('media_type') == 'movie' and c.get('id') not in seen_movie_ids:
                            seen_movie_ids.add(c['id'])
                            movies.append({
                                'id': c.get('id'),
                                'title': c.get('title', ''),
                                'character': c.get('job', ''),
                                'poster_path': c.get('poster_path', ''),
                                'release_date': c.get('release_date', ''),
                                'vote_average': c.get('vote_average', 0),
                                'department': c.get('department', ''),
                            })

                    # Series (cast)
                    seen_series_ids = set()
                    for c in cast_credits:
                        if c.get('media_type') == 'tv' and c.get('id') not in seen_series_ids:
                            seen_series_ids.add(c['id'])
                            series.append({
                                'id': c.get('id'),
                                'title': c.get('name', ''),
                                'character': c.get('character', ''),
                                'poster_path': c.get('poster_path', ''),
                                'first_air_date': c.get('first_air_date', ''),
                                'vote_average': c.get('vote_average', 0),
                                'department': 'Acting',
                            })
                    # Series (crew)
                    for c in crew_credits:
                        if c.get('media_type') == 'tv' and c.get('id') not in seen_series_ids:
                            seen_series_ids.add(c['id'])
                            series.append({
                                'id': c.get('id'),
                                'title': c.get('name', ''),
                                'character': c.get('job', ''),
                                'poster_path': c.get('poster_path', ''),
                                'first_air_date': c.get('first_air_date', ''),
                                'vote_average': c.get('vote_average', 0),
                                'department': c.get('department', ''),
                            })

                    # Sort by date (newest first)
                    movies.sort(key=lambda x: x.get('release_date') or '0000', reverse=True)
                    series.sort(key=lambda x: x.get('first_air_date') or '0000', reverse=True)
                    cache.set(cache_key, (person_info, movies, series), 86400)
        except Exception as e:
            logger.error('fetching person %s: %s', person_id, e)

    if not person_info:
        return render(request, '404.html', status=404)

    return render(request, 'core/person_detail.html', {
        'person': person_info,
        'movies': movies,
        'series': series,
    })


def search(request):
    client = get_data_client()
    search_client = TMDBClient()
    site_settings = SiteSettings.get_settings()
    search_query = request.GET.get('q', '')
    
    # Check cache for search results
    cache_key = f"search_{search_query}"
    cached_result = cache.get(cache_key)
    if cached_result:
        movie_items, series_items = cached_result
    else:
        movie_items = []
        series_items = []

        if search_query:
            # Search for movies
            movie_data = search_client.search_movies(search_query)
            movie_items = [normalize_movie_item(item) for item in movie_data.get('results', [])]

            # Search for series
            series_data = search_client.search_series(search_query)
            series_items = [normalize_series_item(item) for item in series_data.get('results', [])]
        
        # Cache search results for 30 minutes
        cache.set(cache_key, (movie_items, series_items), 1800)

    # Calculate column class
    base_col = 12 // site_settings.items_per_row
    col_class = f"col-{base_col} col-sm-{max(1, base_col-1)} col-md-{base_col} col-lg-{max(1, base_col-2)} col-xl-{max(1, base_col-3)}"
    image_heights = {'small': '200px', 'medium': '300px', 'large': '400px'}
    image_height = image_heights[site_settings.card_size]

    return render(request, 'core/search.html', {
        'search_query': search_query,
        'movies': movie_items,
        'series': series_items,
        'col_class': col_class,
        'image_height': image_height
    })


# ---------------------------------------------------------------------------
# Public Pages: Providers & Watch Regions
# ---------------------------------------------------------------------------


def public_providers(request):
    """Public page showing all streaming providers linked to TMDB data.
    Non-logged-in users see only top 3 providers with a login prompt.
    Logged-in users see all providers.
    """
    site_settings_obj = SiteSettings.get_settings()
    region_code = (request.GET.get('region') or site_settings_obj.watch_region or 'US').upper()
    media_type = request.GET.get('type', 'both')  # movies, tv, both
    is_logged_in = request.user.is_authenticated

    # Get enabled regions
    regions = WatchRegion.objects.filter(is_enabled=True).order_by('display_order')

    # Get providers available in the selected region
    region_providers_qs = ProviderItem.objects.filter(is_enabled=True).order_by('display_priority')

    # Filter by region availability if region is selected
    available_in_region = []
    try:
        region_obj = WatchRegion.objects.filter(code=region_code, is_enabled=True).first()
        if region_obj:
            avail_qs = ProviderRegionAvailability.objects.filter(region=region_obj)
            if media_type == 'movies':
                avail_qs = avail_qs.filter(media_type='movie')
            elif media_type == 'tv':
                avail_qs = avail_qs.filter(media_type='tv')
            available_in_region_ids = avail_qs.values_list('provider_id', flat=True).distinct()
            region_providers_qs = region_providers_qs.filter(id__in=available_in_region_ids)
    except Exception:
        pass

    all_providers = list(region_providers_qs)
    total_count = len(all_providers)

    # For non-logged-in: only show top 3
    top_providers = all_providers[:3]
    hidden_count = max(0, total_count - 3)

    # For logged-in: show all, but also provide toggle info
    # Try to get featured/top providers from TMDB
    tmdb_providers = []
    try:
        tmdb_client = TMDBClient()
        data = tmdb_client.get_available_watch_providers(
            'movie' if media_type == 'movies' else 'tv' if media_type == 'tv' else 'movie',
            region_code
        )
        tmdb_providers = data.get('results', [])[:50]  # top 50
    except Exception:
        pass

    return render(request, 'core/public_providers.html', {
        'regions': regions,
        'active_region': region_code,
        'all_providers': all_providers if is_logged_in else top_providers,
        'top_providers': top_providers,
        'total_count': total_count,
        'hidden_count': hidden_count,
        'is_logged_in': is_logged_in,
        'media_type': media_type,
        'tmdb_providers': tmdb_providers,
    })


def public_watch_regions(request):
    """Public page showing all available watch regions with provider counts."""
    site_settings_obj = SiteSettings.get_settings()
    is_logged_in = request.user.is_authenticated

    regions = WatchRegion.objects.filter(is_enabled=True).order_by('display_order')

    # Annotate each region with provider count
    regions_data = []
    for region in regions:
        movie_count = ProviderRegionAvailability.objects.filter(
            region=region, media_type='movie'
        ).count()
        tv_count = ProviderRegionAvailability.objects.filter(
            region=region, media_type='tv'
        ).count()
        regions_data.append({
            'region': region,
            'movie_providers_count': movie_count,
            'tv_providers_count': tv_count,
            'total_count': movie_count + tv_count,
        })

    # For non-logged-in: only show top 3 regions
    top_regions = regions_data[:3]
    hidden_count = max(0, len(regions_data) - 3)

    return render(request, 'core/public_watch_regions.html', {
        'regions_data': regions_data if is_logged_in else top_regions,
        'all_regions_data': regions_data,
        'total_regions': len(regions_data),
        'hidden_count': hidden_count,
        'is_logged_in': is_logged_in,
        'default_region': (site_settings_obj.watch_region or 'US').upper(),
    })


def ajax_toggle_user_providers(request):
    """AJAX: Let logged-in users enable/disable providers from their profile."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Login required'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    import json
    try:
        data = json.loads(request.body)
        provider_ids = data.get('provider_ids', [])
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    # Store user's preferred providers in session
    request.session['preferred_providers'] = provider_ids
    request.session.modified = True

    return JsonResponse({'status': 'success', 'count': len(provider_ids)})


def ajax_user_preferred_providers(request):
    """AJAX: Get user's preferred providers."""
    if not request.user.is_authenticated:
        return JsonResponse({'providers': []})

    preferred = request.session.get('preferred_providers', [])
    return JsonResponse({'providers': preferred})


def live_tv(request):
    site_settings = SiteSettings.get_settings()
    page = int(request.GET.get('page', 1))

    # Sample channel data for now
    all_channels = [
        {"name": "Fusball TV 1", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:5064"},
        {"name": "NBC UNIVERSO", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:5016"},
        {"name": "#Vamos Spain", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:521"},
        {"name": "20 Mediaset Italy", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:857"},
        {"name": "3 Schweiz", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:155"},
        {"name": "3sat DE", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:726"},
        {"name": "4seven UK", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:16"},
        {"name": "5 USA", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:360"},
        {"name": "6'eren Denmark", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:800"},
        {"name": "6ter France", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:963"},
        {"name": "8Sky Cinema Suspense Italy", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:868"},
        {"name": "A Spor Turkey", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:1011"},
        {"name": "A Sport PK", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:269"},
        {"name": "A&E USA", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:302"},
        {"name": "ABC NY USA", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:766"},
        {"name": "ABC USA", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:51"},
        {"name": "Abu Dhabi Sports 1 Premium", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:609"},
        {"name": "Abu Dhabi Sports 1 UAE", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:600"},
        {"name": "Abu Dhabi Sports 2 Premium", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:610"},
        {"name": "Abu Dhabi Sports 2 UAE", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:601"},
        {"name": "ACB DAZN Spain", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:110"},
        {"name": "ACC Network USA", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:664"},
        {"name": "ACCNX", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:1076"},
        {"name": "ACCNX USA", "watch_link": "https://sports.codespecters.com/watch/dl:tv.json:242"}
    ]

    # Pagination
    items_per_page = site_settings.items_per_row * 4  # Assume 4 rows
    start = (page - 1) * items_per_page
    end = start + items_per_page
    paginated_channels = all_channels[start:end]
    total_pages = (len(all_channels) + items_per_page - 1) // items_per_page
    has_next = page < total_pages

    # Calculate column class
    base_col = 12 // site_settings.items_per_row
    col_class = f"col-{base_col} col-sm-{max(1, base_col-1)} col-md-{base_col} col-lg-{max(1, base_col-2)} col-xl-{max(1, base_col-3)}"
    image_heights = {'small': '200px', 'medium': '300px', 'large': '400px'}
    image_height = image_heights[site_settings.card_size]

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        html = render_to_string('core/_channel_cards.html', {
            'channels': paginated_channels,
            'col_class': col_class,
            'image_height': image_height
        })
        return JsonResponse({'html': html, 'has_next': has_next})

    return render(request, 'core/live_tv.html', {
        'channels': paginated_channels,
        'has_next': has_next,
        'col_class': col_class,
        'image_height': image_height
    })

def calendar_movies(request):
    """AJAX view to fetch movies released on a specific date or date range"""
    start_date = request.GET.get('start')
    end_date = request.GET.get('end')
    single_date = request.GET.get('date')
    
    cache_key = f"calendar_movies_{start_date}_{end_date}_{single_date}"
    cached = cache.get(cache_key)
    if cached:
        return JsonResponse(cached, safe=False)
    
    client = get_data_client()
    fallback_client = TMDBClient()
    movies = []
    
    try:
        params = {}
        
        if start_date and end_date:
            params['primary_release_date.gte'] = start_date
            params['primary_release_date.lte'] = end_date
        elif single_date:
            params['primary_release_date.gte'] = single_date
            params['primary_release_date.lte'] = single_date
        else:
            return JsonResponse([], safe=False)
        
        # Fetch multiple pages
        max_pages = 5  # Limit to 5 pages to prevent too many requests
        current_page = 1
        while current_page <= max_pages:
            params['page'] = current_page
            data = client.discover_movies(params)
            if not data.get('results') and client.__class__ is not TMDBClient:
                data = fallback_client.discover_movies(params)
            page_results = data.get('results', [])
            if not page_results:
                break
            movies.extend(page_results)
            total_pages = data.get('total_pages', 1)
            if current_page >= total_pages:
                break
            current_page += 1
    except Exception as e:
        logger.error("fetching calendar movies: %s", e)
    
    cache.set(cache_key, movies, 3600)  # Cache for 1 hour
    return JsonResponse(movies, safe=False)

def upcoming(request):
    """View to show upcoming movies and series grouped by date"""
    client = get_data_client()
    fallback_client = TMDBClient()
    
    # Get dates from query params or use default
    start_date = request.GET.get('start')
    if not start_date:
        start_date = timezone.localdate().isoformat()
    
    end_date = request.GET.get('end')
    if not end_date:
        end_date = (datetime.datetime.strptime(start_date, '%Y-%m-%d') + datetime.timedelta(days=60)).strftime('%Y-%m-%d')
    
    # Calculate prev/next dates
    current_start = datetime.datetime.strptime(start_date, '%Y-%m-%d')
    prev_start = (current_start - datetime.timedelta(days=60)).strftime('%Y-%m-%d')
    prev_end = (current_start - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    next_start = (datetime.datetime.strptime(end_date, '%Y-%m-%d') + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    next_end = (datetime.datetime.strptime(end_date, '%Y-%m-%d') + datetime.timedelta(days=60)).strftime('%Y-%m-%d')
    
    cache_key = f"upcoming_{start_date}_{end_date}"
    cached = cache.get(cache_key)
    
    if cached:
        grouped_releases = cached
    else:
        # Fetch movies
        movies = []
        try:
            params = {
                'sort_by': 'primary_release_date.asc',
                'primary_release_date.gte': start_date,
                'primary_release_date.lte': end_date,
                'page': 1
            }
            max_pages = 5
            current_page = 1
            while current_page <= max_pages:
                params['page'] = current_page
                data = client.discover_movies(params)
                if not data.get('results') and client.__class__ is not TMDBClient:
                    data = fallback_client.discover_movies(params)
                page_results = data.get('results', [])
                if not page_results:
                    break
                for item in page_results:
                    movies.append(normalize_movie_item(item))
                total_pages = data.get('total_pages', 1)
                if current_page >= total_pages:
                    break
                current_page += 1
        except Exception as e:
            logger.error("fetching movies: %s", e)
        
        # Fetch series
        series = []
        try:
            params = {
                'sort_by': 'first_air_date.asc',
                'air_date.gte': start_date,
                'air_date.lte': end_date,
                'page': 1
            }
            max_pages = 5
            current_page = 1
            while current_page <= max_pages:
                params['page'] = current_page
                data = client.discover_series(params)
                if not data.get('results') and client.__class__ is not TMDBClient:
                    data = fallback_client.discover_series(params)
                page_results = data.get('results', [])
                if not page_results:
                    break
                for item in page_results:
                    series.append(normalize_series_item(item))
                total_pages = data.get('total_pages', 1)
                if current_page >= total_pages:
                    break
                current_page += 1
        except Exception as e:
            logger.error("fetching series: %s", e)
        
        # Group by date
        grouped_releases = {}
        for movie in movies:
            date = movie.get('release_date')
            if date:
                if date not in grouped_releases:
                    grouped_releases[date] = {'movies': [], 'series': []}
                grouped_releases[date]['movies'].append(movie)
        
        for show in series:
            date = show.get('first_air_date')
            if date:
                if date not in grouped_releases:
                    grouped_releases[date] = {'movies': [], 'series': []}
                grouped_releases[date]['series'].append(show)
        
        # Sort dates
        sorted_dates = sorted(grouped_releases.keys())
        grouped_releases = {d: grouped_releases[d] for d in sorted_dates}
        
        # Cache for 1 hour
        cache.set(cache_key, grouped_releases, 3600)
    
    return render(request, 'core/upcoming.html', {
        'grouped_releases': grouped_releases,
        'start_date': start_date,
        'end_date': end_date,
        'prev_start': prev_start,
        'prev_end': prev_end,
        'next_start': next_start,
        'next_end': next_end,
    })

def calendar_series(request):
    """AJAX view to fetch TV shows that aired on a specific date or date range"""
    start_date = request.GET.get('start')
    end_date = request.GET.get('end')
    single_date = request.GET.get('date')
    
    cache_key = f"calendar_series_{start_date}_{end_date}_{single_date}"
    cached = cache.get(cache_key)
    if cached:
        return JsonResponse(cached, safe=False)
    
    client = get_data_client()
    fallback_client = TMDBClient()
    series = []
    
    try:
        params = {}
        
        if start_date and end_date:
            params['air_date.gte'] = start_date
            params['air_date.lte'] = end_date
        elif single_date:
            params['air_date.gte'] = single_date
            params['air_date.lte'] = single_date
        else:
            return JsonResponse([], safe=False)
        
        # Fetch multiple pages
        max_pages = 5  # Limit to 5 pages to prevent too many requests
        current_page = 1
        while current_page <= max_pages:
            params['page'] = current_page
            data = client.discover_series(params)
            if not data.get('results') and client.__class__ is not TMDBClient:
                data = fallback_client.discover_series(params)
            page_results = data.get('results', [])
            if not page_results:
                break
            series.extend(page_results)
            total_pages = data.get('total_pages', 1)
            if current_page >= total_pages:
                break
            current_page += 1
    except Exception as e:
        logger.error("fetching calendar series: %s", e)
    
    cache.set(cache_key, series, 3600)  # Cache for 1 hour
    return JsonResponse(series, safe=False)


def extract_video_url(request):
    """AJAX endpoint to extract direct video URL from embed URL.

    Returns backward-compat keys plus a strict-filtered `links[]` list and
    `logs[]` list for frontend rendering. Only URLs that are definitely
    playable video streams are kept in `links`.
    """
    from urllib.parse import urlparse, parse_qs, urljoin, unquote
    import re
    import base64

    # ---- NEW strict URL helpers -------------------------------------------------
    _PLAYABLE_EXTS = ('.m3u8', '.mp4', '.webm', '.m4v', '.mov', '.ts', '.flv', '.mkv', '.mpd', '.ismv')
    _PLAYABLE_PATH_HINTS = ('/playlist', '/manifest', '/hls/', '/stream/', '/video/', '/vod/',
                            '/watch/', '/media/', '/play/', '/live/', '/vodplay/', '/pl/',
                            '/edge/', '/cdn', '/video-playback', '/assets/videos/', '/streams/')
    _DROP_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.css', '.js',
                  '.json', '.txt', '.xml', '.woff', '.woff2', '.ttf', '.eot', '.pdf', '.zip')
    _DROP_PATH_HINTS = ('/search?', '/search/', '/favicon', '/apple-icon', '/android-icon',
                        '/icon/', '/icons/', '/assets/icon/', '/images/', '/img/',
                        '/assets/img/', '/static/', '/robots.txt', '/sitemap.xml')

    def _looks_like_playable_url(raw_url):
        if not raw_url or not isinstance(raw_url, str):
            return False
        u = raw_url.strip()
        if not u:
            return False
        if u.startswith('javascript:') or u.startswith('mailto:') or u.startswith('data:'):
            return False
        if not (u.startswith('http://') or u.startswith('https://') or u.startswith('/')):
            return False
        try:
            low = u.lower().split('#', 1)[0]
            low_nq = low.split('?', 1)[0]
            # Image / asset / page extensions -> never playable
            for ext in _DROP_EXTS:
                if low_nq.endswith(ext) or (ext + '?') in low:
                    return False
            for hint in _DROP_PATH_HINTS:
                if hint in low:
                    return False
            # Search placeholder URLs like https://site.com/search?q={search_term_string}
            if re.search(r'[?&](q|query|s)=\{[^}]*\}', low):
                return False
            # Site root or very short path
            parsed = urlparse(u)
            if not parsed.path or parsed.path == '/':
                return False
            # Positive signals: explicit playable extension
            for ext in _PLAYABLE_EXTS:
                if low_nq.endswith(ext) or (ext + '?') in low:
                    return True
            # Positive signals: path keywords
            for hint in _PLAYABLE_PATH_HINTS:
                if hint in low:
                    return True
        except Exception:
            return False
        return False

    def _link_type(raw_url):
        low = (raw_url or '').lower().split('?', 1)[0]
        if low.endswith('.m3u8'):
            return 'hls'
        if low.endswith('.mp4'):
            return 'mp4'
        if low.endswith('.webm'):
            return 'webm'
        if low.endswith('.mpd'):
            return 'dash'
        if low.endswith('.ts'):
            return 'ts'
        return 'stream'

    def _normalize_link(url, label=None, quality=None, resolution=None, size_mb=None, link_type=None):
        return {
            'url': url,
            'label': label or (resolution or quality or link_type or 'Source'),
            'quality': quality,
            'resolution': resolution,
            'type': link_type or _link_type(url),
            'size_mb': size_mb,
        }

    def _dedupe_urls(seq):
        seen = set()
        out = []
        for item in seq:
            u = item.get('url') if isinstance(item, dict) else item
            if not u:
                continue
            key = u.strip()
            if key in seen:
                continue
            seen.add(key)
            if isinstance(item, dict):
                out.append(item)
            else:
                out.append(_normalize_link(item))
        return out
    # -----------------------------------------------------------------------------

    embed_url = ''
    if request.method == 'POST':
        try:
            if request.content_type and 'application/json' in request.content_type:
                try:
                    import json as _json
                    payload = _json.loads(request.body)
                    embed_url = str(payload.get('url', '') or payload.get('embed_url', '') or '')
                except Exception:
                    pass
            if not embed_url:
                embed_url = str(request.POST.get('url', '') or request.POST.get('embed_url', '') or '')
        except Exception:
            pass
    if not embed_url:
        embed_url = str(request.GET.get('url', '') or request.GET.get('embed_url', '') or '')

    if not embed_url:
        return JsonResponse({'success': False, 'error': 'No URL provided',
                             'links': [], 'logs': ['Error: No URL provided']})

    cache_key = f"extract_url_v2_{hash(embed_url)}"
    try:
        cached_result = cache.get(cache_key)
        if cached_result:
            return JsonResponse(cached_result)
    except Exception:
        cached_result = None

    try:
        extraction_steps = []
        links_found = []
        extraction_steps.append("Starting IDM-style extraction...")
        extraction_steps.append("Step 1: Check for direct video URL in input")

        def _add_candidates(iterable, label_src=None):
            before = len(links_found)
            for item in iterable or []:
                if isinstance(item, dict) and item.get('url'):
                    if _looks_like_playable_url(item['url']):
                        links_found.append(_normalize_link(
                            item['url'],
                            label=item.get('label'),
                            quality=item.get('quality'),
                            resolution=item.get('resolution'),
                            size_mb=item.get('size_mb'),
                            link_type=item.get('type'),
                        ))
                elif isinstance(item, str):
                    if _looks_like_playable_url(item):
                        links_found.append(_normalize_link(item, label=label_src))
            return len(links_found) - before

        direct_playable = False
        low = embed_url.lower()
        low_nq = low.split('?', 1)[0]
        for ext in _PLAYABLE_EXTS:
            if low_nq.endswith(ext) or (ext + '?') in low:
                direct_playable = True
                break
        for hint in _PLAYABLE_PATH_HINTS:
            if hint in low:
                direct_playable = True
                break
        if direct_playable:
            links_found.append(_normalize_link(embed_url, label='Direct (input URL)', link_type=_link_type(embed_url)))
            extraction_steps.append(f"✓ Direct video pattern in input: {embed_url[:80]}")

        extraction_steps.append("Step 2: Try provider-specific APIs (Vidking, CodeSpecters)")

        fetched_html_cache = {}
        default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }

        def _safe_get(url, timeout=10):
            try:
                r = requests.get(url, headers=default_headers, timeout=timeout, allow_redirects=True)
                fetched_html_cache[url] = r
                return r
            except Exception as e:
                extraction_steps.append(f"  Fetch failed {url[:80]}: {e}")
                return None

        parsed_embed = urlparse(embed_url)
        parsed_query = parse_qs(parsed_embed.query)
        api_key = (parsed_query.get('apikey', [''])[0] or '').strip()
        parts = [p for p in parsed_embed.path.strip('/').split('/') if p]

        try:
            if ('vidking.net' in parsed_embed.netloc.lower()) and len(parts) >= 2 and parts[0] == 'embed':
                kind = parts[1]
                if kind == 'movie' and len(parts) >= 3:
                    mid = parts[2]
                    api_endpoint = f"{parsed_embed.scheme}://{parsed_embed.netloc}/api/movie/{mid}?apikey={api_key}"
                    extraction_steps.append(f"Trying Vidking Movie API: `{api_endpoint}` ")
                    api_resp = _safe_get(api_endpoint, timeout=12)
                    if api_resp is not None and 200 <= api_resp.status_code < 400:
                        try:
                            api_data = api_resp.json()
                            if api_data.get('success'):
                                sources = api_data.get('sources') or []
                                added = _add_candidates(sources)
                                extraction_steps.append(f"  Vidking Movie API returned {len(sources)} source(s); kept {added} playable.")
                        except Exception as e:
                            extraction_steps.append(f"  Vidking Movie API failed: {e}")
                elif kind == 'tv' and len(parts) >= 5:
                    sid, sn, ep = parts[2], parts[3], parts[4]
                    api_endpoint = f"{parsed_embed.scheme}://{parsed_embed.netloc}/api/tv/{sid}/{sn}/{ep}?apikey={api_key}"
                    extraction_steps.append(f"Trying Vidking TV API: `{api_endpoint}` ")
                    api_resp = _safe_get(api_endpoint, timeout=12)
                    if api_resp is not None and 200 <= api_resp.status_code < 400:
                        try:
                            api_data = api_resp.json()
                            if api_data.get('success'):
                                sources = api_data.get('sources') or []
                                added = _add_candidates(sources)
                                extraction_steps.append(f"  Vidking TV API returned {len(sources)} source(s); kept {added} playable.")
                        except Exception as e:
                            extraction_steps.append(f"  Vidking TV API failed: {e}")
        except Exception as e:
            extraction_steps.append(f"  Provider API skipped: {e}")

        extraction_steps.append("Step 3: Recursive page + iframe crawling")

        MAX_DEPTH = 2
        MAX_IFRAMES_PER_DEPTH = 4
        visited = set()

        def _crawl_page(crawl_url, depth=0):
            if depth > MAX_DEPTH:
                return
            if crawl_url in visited:
                return
            visited.add(crawl_url)

            resp = fetched_html_cache.get(crawl_url) or _safe_get(crawl_url)
            if resp is None:
                return
            try:
                final_url = resp.url or crawl_url
                status_txt = f"Status: {resp.status_code}, Length: {len(resp.text or '')}"
                extraction_steps.append(f"[Depth {depth}] Fetching: `{final_url[:120]}` ")
                extraction_steps.append(f"  {status_txt}")
                page = resp.text or ''
            except Exception:
                return

            # Direct src= patterns (.m3u8 / .mp4 / etc.)
            # 1. Any quoted string containing a playable URL
            url_quotes = re.findall(r'["\']([^"\']{8,})["\']', page)
            abs_or_rel = []
            for tok in url_quotes:
                t = tok.strip()
                if not t:
                    continue
                if t.startswith(('http://', 'https://')):
                    abs_or_rel.append(t)
                elif t.startswith('/'):
                    try:
                        abs_or_rel.append(urljoin(final_url, t))
                    except Exception:
                        pass
                elif re.match(r'^[a-zA-Z0-9_+/=-]+\s*$', t) and len(t) % 4 == 0 and len(t) >= 16:
                    try:
                        decoded = base64.b64decode(t + '===', validate=False).decode('utf-8', errors='ignore')
                        if decoded.startswith(('http://', 'https://')):
                            abs_or_rel.append(decoded)
                    except Exception:
                        pass
            # 2. Raw regex capture: common source / file / jwplayer / hls patterns
            raw_pats = [
                r"""(?:file|src|source|playlist|manifest|url)\s*[:=]\s*['"]([^'"]{8,})['"]""",
                r"""(?:sources|tracks)\s*[:=]\s*\[\s*\{([^}]+)\}""",
                r"""https?://[^\s"'<>]{10,}\.(?:m3u8|mp4|webm|m4v|mov|ts|mpd)(?:\?[^\s"'<>]*)?""",
                r"""/[^"'<> ]{3,}\.(?:m3u8|mp4|webm|m4v|mov|ts|mpd)(?:\?[^\s"'<>]*)?""",
            ]
            for pat in raw_pats:
                for m in re.findall(pat, page, re.IGNORECASE):
                    if isinstance(m, tuple):
                        for part in m:
                            if isinstance(part, str):
                                if part.startswith(('http://', 'https://')):
                                    abs_or_rel.append(part)
                                elif part.startswith('/'):
                                    try:
                                        abs_or_rel.append(urljoin(final_url, part))
                                    except Exception:
                                        pass
                    elif isinstance(m, str):
                        if m.startswith(('http://', 'https://')):
                            abs_or_rel.append(m)
                        elif m.startswith('/'):
                            try:
                                abs_or_rel.append(urljoin(final_url, m))
                            except Exception:
                                pass
            added = _add_candidates(abs_or_rel)
            extraction_steps.append(f"  Kept {added} direct URL matches in page.")

            # 3. JSON-LD / ld+json blocks — ONLY keep schema.org VideoObject contentUrl / embedUrl
            ld_blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>', page, re.IGNORECASE)
            import json as _json
            ld_kept = 0
            for block in ld_blocks:
                try:
                    data = _json.loads(block)
                except Exception:
                    continue
                stack = [data]
                while stack:
                    cur = stack.pop()
                    if isinstance(cur, dict):
                        t = cur.get('@type') or cur.get('type') or ''
                        if isinstance(t, list):
                            types_set = {str(x).lower() for x in t}
                        else:
                            types_set = {str(t).lower()}
                        if 'videoobject' in types_set or 'movie' in types_set or 'tvepisode' in types_set:
                            for key in ('contentUrl', 'content_url', 'url', 'embedUrl', 'embed_url', 'downloadUrl', 'download_url'):
                                val = cur.get(key)
                                if isinstance(val, str) and _looks_like_playable_url(val):
                                    abs_val = val if val.startswith(('http://', 'https://')) else urljoin(final_url, val)
                                    if _looks_like_playable_url(abs_val):
                                        links_found.append(_normalize_link(abs_val, label=f"ld+json {key}", quality=cur.get('videoQuality')))
                                        ld_kept += 1
                            src = cur.get('video') or cur.get('source')
                            if isinstance(src, str) and _looks_like_playable_url(src):
                                links_found.append(_normalize_link(src, label='ld+json video'))
                                ld_kept += 1
                            for sub in ('hasPart', 'parts', 'video', 'subjectOf', 'encodings'):
                                sval = cur.get(sub)
                                if isinstance(sval, (list, dict)):
                                    stack.append(sval)
                        for val in cur.values():
                            if isinstance(val, (dict, list)):
                                stack.append(val)
                    elif isinstance(cur, list):
                        stack.extend(cur)
            if ld_kept:
                extraction_steps.append(f"  Found {ld_kept} playable URLs in ld+json")
            # (Non-video JSON-LD URLs are deliberately dropped — they cause homepage/search/PNG garbage)

            # Recurse into iframes up to depth limit
            if depth < MAX_DEPTH:
                iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\'][^>]*>', page, re.IGNORECASE)
                iframes = iframes[:MAX_IFRAMES_PER_DEPTH]
                for iframe_src in iframes:
                    try:
                        if not iframe_src or iframe_src.startswith('about:') or iframe_src.startswith('javascript:'):
                            continue
                        if iframe_src.startswith(('http://', 'https://')):
                            abs_iframe = iframe_src
                        else:
                            abs_iframe = urljoin(final_url, iframe_src)
                        _crawl_page(abs_iframe, depth + 1)
                    except Exception:
                        pass

        try:
            _crawl_page(embed_url, depth=0)
        except Exception as e:
            extraction_steps.append(f"  Recursive crawl aborted: {e}")

        links_found = _dedupe_urls(links_found)
        extraction_steps.append(f"Final result: {len(links_found)} source(s) found")

        best_link = None
        if links_found:
            # Prefer HLS/MP4 explicitly labeled playable, then direct content
            priority = {'hls': 5, 'mp4': 4, 'webm': 3, 'dash': 2, 'ts': 1}
            def _score(lnk):
                t = (lnk.get('type') or 'stream').lower()
                base = priority.get(t, 0)
                if lnk.get('resolution'):
                    base += 0.5
                return base
            links_sorted = sorted(links_found, key=_score, reverse=True)
            best_link = links_sorted[0].get('url')
            # Keep sorted order in response
            links_found = links_sorted

        # Fallback: keep backward compat for old consumers that only read `extracted_url`
        extracted_url = best_link or embed_url

        result = {
            'success': True,
            'original_url': embed_url,
            'extracted_url': extracted_url,
            'method': 'fallback' if extracted_url == embed_url else 'parsed',
            'steps': extraction_steps,
            'links': links_found,
            'logs': extraction_steps,
            'duration_ms': 0,
        }
        try:
            cache.set(cache_key, result, 1800)
        except Exception:
            pass
        return JsonResponse(result)

    except Exception as e:
        print(f"Extraction error: {e}")
        return JsonResponse({
            'success': False,
            'error': str(e),
            'extracted_url': embed_url,
            'steps': [f"Error: {e}"],
            'links': [],
            'logs': [f"Error: {e}"],
        })
def download_proxy(request):
    """Proxy a video download — streams the remote file through our server
    with proper Content-Disposition so the browser triggers a download.
    """
    target_url = request.GET.get('url', '')
    filename = request.GET.get('filename', 'download')
    if not target_url:
        return HttpResponse('No URL provided', status=400)

    # SSRF guard: never let a browser talk us into fetching loopback, LAN,
    # link-local / cloud-metadata or otherwise non-public addresses.
    from .url_guard import UnsafeURLError, ensure_public_url, guarded_get
    try:
        target_url = ensure_public_url(target_url)
    except UnsafeURLError as exc:
        return HttpResponse(str(exc), status=400)

    # Block obviously non-media URLs for safety. Images are allowed so the
    # media gallery can offer full-size poster/wallpaper downloads (TMDB
    # blocks direct cross-origin fetches, the browser would refuse to save).
    low = target_url.lower().split('?')[0]
    safe_exts = ('.mp4', '.webm', '.m4v', '.mkv', '.mov', '.ts', '.flv',
                 '.m3u8', '.mpd', '.avi', '.wmv', '.ogg',
                 '.jpg', '.jpeg', '.png', '.webp')
    image_hosts = ('image.tmdb.org',)
    is_image = any(low.endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.webp')) \
        and any(host in low for host in image_hosts)
    if not is_image and not any(low.endswith(ext) for ext in safe_exts):
        # Also allow if path hints suggest video
        video_hints = ('/stream/', '/video/', '/vod/', '/play/', '/media/',
                       '/hls/', '/cdn/', '/playlist', '/manifest')
        if not any(h in low for h in video_hints):
            return HttpResponse('URL does not look like a video file', status=400)

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                          ' (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Referer': target_url,
            'Origin': '/'.join(target_url.split('/')[:3]),
        }
        remote = guarded_get(target_url, headers=headers, stream=True, timeout=30)
        if remote.status_code >= 400:
            return HttpResponse(f'Upstream returned {remote.status_code}', status=502)

        content_type = remote.headers.get('Content-Type', 'application/octet-stream')
        content_length = remote.headers.get('Content-Length')

        response = StreamingHttpResponse(
            remote.iter_content(chunk_size=64 * 1024),
            content_type=content_type,
        )
        # Force download
        safe_name = filename.replace('/', '_').replace('\\', '_')[:120]
        if not any(safe_name.endswith(ext) for ext in safe_exts):
            # Guess extension from content type
            ext_map = {
                'video/mp4': '.mp4', 'video/webm': '.webm',
                'video/x-matroska': '.mkv', 'video/quicktime': '.mov',
                'application/x-mpegURL': '.m3u8', 'application/dash+xml': '.mpd',
            }
            for ct, ext in ext_map.items():
                if ct in content_type:
                    safe_name += ext
                    break

        response['Content-Disposition'] = f'attachment; filename="{safe_name}"'
        if content_length:
            response['Content-Length'] = content_length
        response['Access-Control-Allow-Origin'] = '*'
        return response

    except requests.exceptions.Timeout:
        return HttpResponse('Download timed out — try again', status=504)
    except requests.exceptions.ConnectionError:
        return HttpResponse('Could not connect to source server', status=502)
    except Exception as e:
        logger.error("Download proxy error: %s", e)
        return HttpResponse(f'Download failed: {e}', status=500)




# ===== Vidking SpeedRacelight API source fetching + decryption =====
_VIDKING_SERVERS = {
    'Yoru': 'cdn/sources-with-title',
    'Cypher': 'downloader2/sources-with-title',
    'Breach': 'm4uhd/sources-with-title',
    'Neon': 'vsrc/sources-with-title',
    'Vyse': 'hdmovie/sources-with-title',
    'Killjoy': 'meine/sources-with-title',
    'Fade': 'hdmovie/sources-with-title',
    'Omen': 'lamovie/sources-with-title',
    'Raze': 'superflix/sources-with-title',
}
_VIDKING_API_BASE = 'https://api.speedracelight.com'
_VIDKING_DB_BASE = 'https://db.speedracelight.com/3'


def _vk_ci(l):
    """Hash mixing (32-bit unsigned)"""
    l = l & 0xFFFFFFFF
    l ^= l >> 16
    l = (l * 2246822507) & 0xFFFFFFFF
    l ^= l >> 13
    l = (l * 3266489909) & 0xFFFFFFFF
    l ^= l >> 16
    return l & 0xFFFFFFFF


def _vk_ps(l, o):
    """Rotate left 32-bit"""
    l = l & 0xFFFFFFFF
    o &= 31
    if o == 0:
        return l
    return ((l << o) | (l >> (32 - o))) & 0xFFFFFFFF


_SHA256_CONSTS = [1116352408, 1899447441, 3049323471, 3921009573, 961987163, 1508970993,
                  2453635748, 2870763221, 3624381080, 310598401, 607225278, 1426881987,
                  1925078388, 2162078206, 2614888103, 3248222580]
_IV = [1732584193, 4023233417, 2562383102, 271733878]
_JS_SIZE = 61
_JS_ROUNDS = 8
_MAGIC_CONST = 2654435769  # 0x9E3779B9
_MAGIC_BYTES = [109, 118, 109, 49]  # "mvm1"
_MAGIC_XOR = 2779096485


def _vk_af(s):
    """Hash string using SHA-256 constants"""
    o = _IV[0] & 0xFFFFFFFF
    for e in range(len(s)):
        ch = ord(s[e])
        o = _vk_ps((o ^ (ch * _SHA256_CONSTS[e & 15])) & 0xFFFFFFFF, 5)
    return _vk_ci(o)


def _vk_wf(s):
    """RC4 KSA (Key Scheduling Algorithm)"""
    o = list(range(256))
    e = 0
    for i in range(256):
        e = (e + o[i] + ord(s[i % len(s)])) & 0xFF
        o[i], o[e] = o[e], o[i]
    return o


def _vk_vf(s):
    """FNV-1a hash"""
    o = 2166136261
    for ch in s:
        o = (o ^ ord(ch)) & 0xFFFFFFFF
        o = (o * 16777619) & 0xFFFFFFFF
    return _vk_ci(o)


def _vk_nf(l, o, e):
    """Ternary hash"""
    return ((l ^ o) | (l & o & e)) & 0xFFFFFFFF


def _vk_bf(idx):
    return (idx * (idx + 1) & 1) == 0


def _vk_if(idx):
    return (idx * (idx + 1) & 1) == 1


def _vk_init_state(seed_str, tmdb_id):
    """Initialize PRNG state from seed string and tmdbId"""
    if _vk_if(len(seed_str)):
        return {'S': _vk_wf(seed_str), 'acc': _vk_af(seed_str)}

    e = [0] * _JS_SIZE
    i = _vk_ci(_vk_vf(seed_str) ^ _vk_ci((tmdb_id & 0xFFFFFFFF) ^ _MAGIC_CONST)) & 0xFFFFFFFF

    for r in range(_JS_ROUNDS):
        if _vk_bf(r):
            n = i % _JS_SIZE
            i = _vk_ps((i + _MAGIC_CONST) & 0xFFFFFFFF, 7 + (r & 7))
            e[n] = (i ^ _vk_ci(i)) & 0xFFFFFFFF
            i = _vk_ci((i + n) & 0xFFFFFFFF)
        else:
            e[r] = _SHA256_CONSTS[r & 15]

    return {'S': e, 'acc': _vk_ci((i ^ _MAGIC_XOR) & 0xFFFFFFFF)}


def _vk_step(state, counter):
    """PRNG step - generate next pseudo-random word"""
    e = state['S']
    i = state['acc']
    r = i % _JS_SIZE
    n = -(1 if r < len(e) and e[r] != 0 else 0)
    u = e[r] & 0xFFFFFFFF
    d = (_MAGIC_CONST * (counter + 1)) & 0xFFFFFFFF
    g = _vk_nf(i, (u ^ d) & 0xFFFFFFFF, n)
    g = (_vk_ps((g + i) & 0xFFFFFFFF, r & 31) ^ _vk_ps(i, (r * 7) & 31)) & 0xFFFFFFFF
    i = _vk_ci((g + _MAGIC_CONST) & 0xFFFFFFFF)
    e[r] = i & 0xFFFFFFFF
    state['acc'] = i
    return i & 0xFFFFFFFF


def _vk_generate_keystream(seed_str, tmdb_id, length):
    """Generate PRNG keystream of given length"""
    state = _vk_init_state(seed_str, tmdb_id)
    result = bytearray(length)
    n = 0
    pos = 0
    while pos < length:
        word = _vk_step(state, n)
        n += 1
        result[pos] = word & 0xFF
        pos += 1
        if pos < length:
            result[pos] = (word >> 8) & 0xFF
            pos += 1
        if pos < length:
            result[pos] = (word >> 16) & 0xFF
            pos += 1
        if pos < length:
            result[pos] = (word >> 24) & 0xFF
            pos += 1
    return result


def _vk_decrypt(payload_b64, seed_str, tmdb_id, max_payload_mb=10):
    """Decrypt Vidking encrypted response.

    Hardened against pathological upstream responses: validates the "mvm1"
    magic bytes using only the FIRST keystream block before doing any bulk
    work, so a large non-encrypted error page can't pin a worker in the
    pure-Python byte-by-byte loop for minutes (the cause of production 504s).
    """
    # Base64 decode (URL-safe)
    padded = payload_b64.replace('-', '+').replace('_', '/')
    if len(padded) % 4:
        padded += '=' * (4 - len(padded) % 4)
    data = bytearray(base64.b64decode(padded))

    if len(data) > max_payload_mb * 1024 * 1024:
        raise ValueError(f'encrypted payload too large ({len(data)} bytes)')
    if len(data) < len(_MAGIC_BYTES):
        raise ValueError('encrypted payload too short')

    # Cost guard: decrypt ONLY the first 4 bytes (they come from PRNG step 0,
    # ~1 byte per step) and require the magic bytes. A legit payload passes;
    # garbage fails fast without generating a full keystream.
    probe_state = _vk_init_state(seed_str, tmdb_id)
    probe_word = _vk_step(probe_state, 0)
    first = bytearray(data[:4])
    for k in range(4):
        first[k] ^= (probe_word >> (8 * k)) & 0xFF
    for k in range(4):
        if first[k] != _MAGIC_BYTES[k]:
            raise ValueError('decryption failed: bad seed or tampered payload (probe)')

    # Generate keystream and XOR
    keystream = _vk_generate_keystream(seed_str, tmdb_id, len(data))
    for i in range(len(data)):
        data[i] ^= keystream[i]

    # Verify magic bytes
    for i in range(len(_MAGIC_BYTES)):
        if data[i] != _MAGIC_BYTES[i]:
            raise ValueError("Decryption failed: bad seed or tampered payload")

    # Return decrypted JSON
    return bytes(data[len(_MAGIC_BYTES):]).decode('utf-8')


def _vk_get_seed(api_base, tmdb_id):
    """Fetch seed from speedracelight API"""
    seed_url = f"{api_base}/seed?mediaId={tmdb_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
    }
    resp = requests.get(seed_url, headers=headers, timeout=5)
    resp.raise_for_status()
    data = resp.json()
    return data.get('seed', '')


def _vk_fetch_sources_for_server(server_name, server_endpoint, tmdb_id, media_type,
                                  title='', year='', season_id='', episode_id='',
                                  imdb_id='', seed='', timestamp=''):
    """Fetch and decrypt sources from a specific server endpoint"""
    url = f"{_VIDKING_API_BASE}/{server_endpoint}"
    params = {
        'title': title,
        'mediaType': media_type,
        'year': str(year),
        'episodeId': str(episode_id or '1'),
        'seasonId': str(season_id or '1'),
        'tmdbId': str(tmdb_id),
        'imdbId': str(imdb_id or ''),
        'enc': '2',
        'seed': seed,
    }
    if timestamp:
        params['_t'] = str(timestamp)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0',
    }
    resp = requests.get(url, params=params, headers=headers, timeout=5)
    if resp.status_code == 401:
        raise ValueError("seed rejected")

    resp.raise_for_status()
    encrypted_text = resp.text

    # Decrypt
    decrypted_json = _vk_decrypt(encrypted_text, seed, int(tmdb_id))
    return json.loads(decrypted_json)


def _vk_fetch_tmdb_info(tmdb_id, media_type='movie'):
    """Fetch title, year, imdb_id from speedracelight DB"""
    url = f"{_VIDKING_DB_BASE}/{media_type}/{tmdb_id}?append_to_response=external_ids"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    resp = requests.get(url, headers=headers, timeout=5)
    resp.raise_for_status()
    data = resp.json()

    title = data.get('title') or data.get('name', '')
    if media_type == 'movie':
        rd = data.get('release_date', '')
        year = rd[:4] if rd else ''
    else:
        rd = data.get('first_air_date', '')
        year = rd[:4] if rd else ''

    imdb_id = ''
    ext_ids = data.get('external_ids', {})
    if isinstance(ext_ids, dict):
        imdb_id = ext_ids.get('imdb_id', '')

    return {'title': title, 'year': year, 'imdbId': imdb_id}


def fetch_embed_sources(request):
    """AJAX endpoint: fetch video sources by replicating the Vidking player's
    API calls to speedracelight.com. Returns decrypted video URLs.

    Params:
        tmdb_id - TMDB movie/show ID
        media_type - "movie" or "tv"
        season_id - season number (for TV)
        episode_id - episode number (for TV)
    """
    import time as _time

    tmdb_id = request.GET.get('tmdb_id', '') or request.POST.get('tmdb_id', '')
    media_type = request.GET.get('media_type', 'movie') or request.POST.get('media_type', 'movie')
    season_id = request.GET.get('season_id', '1') or request.POST.get('season_id', '1')
    episode_id = request.GET.get('episode_id', '1') or request.POST.get('episode_id', '1')

    if not tmdb_id:
        return JsonResponse({'success': False, 'error': 'tmdb_id is required', 'sources': []})

    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Invalid tmdb_id', 'sources': []})

    timestamp = str(int(_time.time() * 1000))

    # Step 1: Get TMDB info (title, year, imdb_id)
    try:
        tmdb_info = _vk_fetch_tmdb_info(tmdb_id, media_type)
    except Exception as e:
        tmdb_info = {'title': '', 'year': '', 'imdbId': ''}
        logger.warning("Could not fetch TMDB info: %s", e)

    title = tmdb_info.get('title', '')
    year = tmdb_info.get('year', '')
    imdb_id = tmdb_info.get('imdbId', '')

    all_sources = []
    errors = []

    # Step 2: Get seed
    for attempt in range(2):
        try:
            seed = _vk_get_seed(_VIDKING_API_BASE, tmdb_id)
            break
        except Exception as e:
            errors.append(f"Seed fetch attempt {attempt+1}: {e}")
            seed = ''
            if attempt == 0:
                _time.sleep(0.5)

    if not seed:
        return JsonResponse({
            'success': False,
            'error': 'Could not fetch seed',
            'sources': [],
            'errors': errors,
        })

    # Step 3: Try each server to get sources
    for server_name, endpoint in _VIDKING_SERVERS.items():
        try:
            result = _vk_fetch_sources_for_server(
                server_name, endpoint, tmdb_id, media_type,
                title=title, year=year, season_id=season_id,
                episode_id=episode_id, imdb_id=imdb_id,
                seed=seed, timestamp=timestamp,
            )

            sources = result.get('sources', [])
            if isinstance(sources, list):
                for src in sources:
                    if isinstance(src, dict) and src.get('url'):
                        all_sources.append({
                            'url': src['url'],
                            'quality': src.get('quality', ''),
                            'type': _guess_video_type(src.get('url', '')),
                            'server': server_name,
                        })
            elif isinstance(sources, dict):
                for key, val in sources.items():
                    if isinstance(val, dict) and val.get('url'):
                        all_sources.append({
                            'url': val['url'],
                            'quality': val.get('quality', ''),
                            'type': _guess_video_type(val.get('url', '')),
                            'server': server_name,
                        })

            # If we got sources from this server, try the rest too but don't block
            if all_sources:
                break

        except ValueError as ve:
            if 'seed rejected' in str(ve):
                # Re-fetch seed and retry
                try:
                    seed = _vk_get_seed(_VIDKING_API_BASE, tmdb_id)
                    result = _vk_fetch_sources_for_server(
                        server_name, endpoint, tmdb_id, media_type,
                        title=title, year=year, season_id=season_id,
                        episode_id=episode_id, imdb_id=imdb_id,
                        seed=seed, timestamp=timestamp,
                    )
                    sources = result.get('sources', [])
                    if isinstance(sources, list):
                        for src in sources:
                            if isinstance(src, dict) and src.get('url'):
                                all_sources.append({
                                    'url': src['url'],
                                    'quality': src.get('quality', ''),
                                    'type': _guess_video_type(src.get('url', '')),
                                    'server': server_name,
                                })
                    elif isinstance(sources, dict):
                        for key, val in sources.items():
                            if isinstance(val, dict) and val.get('url'):
                                all_sources.append({
                                    'url': val['url'],
                                    'quality': val.get('quality', ''),
                                    'type': _guess_video_type(val.get('url', '')),
                                    'server': server_name,
                                })
                    if all_sources:
                        break
                except Exception as e2:
                    errors.append(f"{server_name} retry: {e2}")
            else:
                errors.append(f"{server_name}: {ve}")
        except Exception as e:
            errors.append(f"{server_name}: {e}")

    # Deduplicate by URL
    seen = set()
    unique_sources = []
    for src in all_sources:
        if src['url'] not in seen:
            seen.add(src['url'])
            unique_sources.append(src)

    if unique_sources:
        # Sort: prefer mp4 > webm > m3u8 > mpd > ts > others
        type_priority = {'mp4': 6, 'webm': 5, 'm4v': 4, 'mkv': 3, 'm3u8': 2, 'mpd': 2, 'ts': 1}
        unique_sources.sort(key=lambda x: type_priority.get(x.get('type', ''), 0), reverse=True)
        return JsonResponse({
            'success': True,
            'sources': unique_sources,
            'title': title,
            'tmdb_id': tmdb_id,
            'media_type': media_type,
        })

    return JsonResponse({
        'success': False,
        'error': 'No sources found from any server',
        'sources': [],
        'errors': errors,
        'title': title,
    })


def _guess_video_type(url):
    """Guess video type from URL extension"""
    low = url.lower().split('?')[0]
    if low.endswith('.mp4'):
        return 'mp4'
    if low.endswith('.webm'):
        return 'webm'
    if low.endswith('.m4v'):
        return 'm4v'
    if low.endswith('.mkv'):
        return 'mkv'
    if low.endswith('.mov'):
        return 'mov'
    if low.endswith('.m3u8'):
        return 'hls'
    if low.endswith('.mpd'):
        return 'dash'
    if low.endswith('.ts'):
        return 'ts'
    return 'stream'
# ===== End Vidking SpeedRacelight API =====

def proxy_embed(request):
    """Proxy an embed page through our server and inject a video-URL
    interceptor script. The parent page opens this in a hidden iframe,
    listens for postMessage with the captured video source, and uses it
    for download.
    """
    from urllib.parse import urljoin

    target = request.GET.get('url', '').strip()
    if not target:
        return HttpResponse('No URL provided', status=400)

    # SSRF guard (see core/url_guard.py).
    from .url_guard import UnsafeURLError, ensure_public_url, guarded_get
    try:
        target = ensure_public_url(target)
    except UnsafeURLError as exc:
        return HttpResponse(str(exc), status=400)

    try:
        headers = {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/125.0.0.0 Safari/537.36'),
            'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': target,
        }
        resp = guarded_get(target, headers=headers, timeout=15)
        if resp.status_code >= 400:
            return HttpResponse(f'Upstream returned {resp.status_code}', status=502)

        html = resp.text or ''
        final_url = resp.url or target
        base = final_url.rsplit('/', 1)[0] + '/'

        # Rewrite relative URLs in src= and href= attributes
        import re as _re
        def _rewrite_url(m):
            quote = m.group(1)
            url = m.group(2)
            if url.startswith(('http://', 'https://', 'data:', 'javascript:', '#')):
                return m.group(0)
            abs_url = urljoin(base, url)
            return f'{quote}{abs_url}{quote}'
        html = _re.sub(r"""((?:src|href|action)\s*=\s*)(["'])([^"']*?)""", _rewrite_url, html, flags=_re.IGNORECASE)

        # Build the interceptor script
        interceptor_js = r"""
(function() {
  function postVideoUrl(url) {
    if (url && url !== window._lastPosted) {
      window._lastPosted = url;
      try { parent.postMessage({type:'_videoUrlCaptured', url:url}, '*'); } catch(e) {}
    }
  }
  var VIDEO_RE = /\.(mp4|m4v|webm|mkv|mov|ts|m3u8|mpd|flv|ogg)(\?|$)/i;
  function isVideo(u) { return u && VIDEO_RE.test(u.split('?')[0]); }

  // Hook HTMLMediaElement.src
  try {
    var _src = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
    if (_src && _src.set) {
      Object.defineProperty(HTMLMediaElement.prototype, 'src', {
        set: function(v) { _src.set.call(this, v); if(isVideo(v)) postVideoUrl(v); },
        get: function() { return _src.get.call(this); },
        configurable: true
      });
    }
  } catch(e) {}

  // Hook fetch
  try {
    var _fetch = window.fetch;
    window.fetch = function() {
      var url = arguments[0];
      if (typeof url === 'string' && isVideo(url)) postVideoUrl(url);
      if (url && url.url && isVideo(url.url)) postVideoUrl(url.url);
      return _fetch.apply(this, arguments);
    };
  } catch(e) {}

  // Hook XHR
  try {
    var _open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(m, url) {
      if (typeof url === 'string' && isVideo(url)) postVideoUrl(url);
      return _open.apply(this, arguments);
    };
  } catch(e) {}

  // Poll for <video>/<source> elements
  var tries = 0;
  var poll = setInterval(function() {
    tries++;
    document.querySelectorAll('video, audio, source').forEach(function(el) {
      var s = el.src || el.currentSrc || el.getAttribute('src') || '';
      if (isVideo(s)) postVideoUrl(s);
    });
    // Also scan script tags for inline video URLs
    document.querySelectorAll('script').forEach(function(el) {
      var txt = el.textContent || '';
      var m = txt.match(/(?:file|src|source|playlist|manifest|url|location)\s*[:=]\s*["']([^"']{10,})["']/);
      if (m && m[1] && isVideo(m[1])) postVideoUrl(m[1]);
      // Also look for base64-encoded URLs
      var b64 = txt.match(/atob\s*\(\s*["']([A-Za-z0-9+/=]{20,})["']\s*\)/);
      if (b64 && b64[1]) {
        try {
          var decoded = atob(b64[1]);
          if (isVideo(decoded)) postVideoUrl(decoded);
        } catch(x) {}
      }
    });
    if (tries > 90 || window._lastPosted) clearInterval(poll);
  }, 333);
})();
"""
        # Inject interceptor before </head> or </body> or at end
        interceptor_tag = '<script>' + interceptor_js + '</script>'
        if '</head>' in html.lower():
            html = html.replace('</head>', interceptor_tag + chr(10) + '</head>', 1)
        elif '</body>' in html.lower():
            html = html.replace('</body>', interceptor_tag + chr(10) + '</body>', 1)
        else:
            html += chr(10) + interceptor_tag

        resp_content = html.encode('utf-8', errors='replace')
        return HttpResponse(resp_content, content_type='text/html; charset=utf-8',
                            headers={
                                'X-Frame-Options': 'ALLOWALL',
                                'Content-Security-Policy': "frame-ancestors *",
                                'Cache-Control': 'no-cache, no-store, must-revalidate',
                            })

    except requests.exceptions.Timeout:
        return HttpResponse('Embed page timed out', status=504)
    except requests.exceptions.ConnectionError:
        return HttpResponse('Could not connect to embed server', status=502)
    except Exception as e:
        logger.error("proxy_embed error: %s", e)
        return HttpResponse(f'Proxy error: {e}', status=500)
@login_required
@user_passes_test(is_staff_or_superuser)
def player_list(request):
    players = PlayerConfiguration.objects.all().order_by('order', 'id')
    return render(request, 'core/player_list.html', {'players': players})


# ═══════════════════════════════════════════════════════════════════
# CinePlayer Admin Settings
# ═══════════════════════════════════════════════════════════════════

@login_required
@user_passes_test(is_staff_or_superuser)
def cineplayer_settings(request):
    """Admin settings page for CinePlayer configuration."""
    from django.contrib import messages
    from core.models import CinePlayerConfig
    config = CinePlayerConfig.get_config()

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'save':
            config.enabled = request.POST.get('enabled') == 'on'
            config.gateway_url = request.POST.get('gateway_url', config.gateway_url)
            config.invite_code = request.POST.get('invite_code', config.invite_code)
            config.share_url = request.POST.get('share_url', config.share_url)
            config.site_title = request.POST.get('site_title', config.site_title)
            config.site_subtitle = request.POST.get('site_subtitle', config.site_subtitle)
            config.poster_url = request.POST.get('poster_url', config.poster_url)
            config.auto_play = request.POST.get('auto_play') == 'on'
            config.show_next_episode = request.POST.get('show_next_episode') == 'on'
            config.description = request.POST.get('description', config.description)
            config.save()
            messages.success(request, 'CinePlayer settings saved.')
            return redirect('cineplayer_settings')

        elif action == 'add_referrer':
            new_url = request.POST.get('new_referrer', '').strip()
            if new_url:
                links = config.referrer_links or []
                if new_url not in links:
                    links.append(new_url)
                    config.referrer_links = links
                    config.save()
                    messages.success(request, f'Referrer added: {new_url}')
                else:
                    messages.warning(request, 'This referrer already exists.')
            return redirect('cineplayer_settings')

        elif action == 'remove_referrer':
            idx = int(request.POST.get('index', -1))
            links = config.referrer_links or []
            if 0 <= idx < len(links):
                removed = links.pop(idx)
                config.referrer_links = links
                config.save()
                messages.success(request, f'Referrer removed: {removed}')
            return redirect('cineplayer_settings')

    return render(request, 'core/cineplayer_settings.html', {'config': config})


@login_required
@user_passes_test(is_staff_or_superuser)
def cineplayer_toggle(request):
    """Quick toggle CinePlayer on/off."""
    from core.models import CinePlayerConfig
    config = CinePlayerConfig.get_config()
    config.enabled = not config.enabled
    config.save()
    status = 'enabled' if config.enabled else 'disabled'
    from django.contrib import messages
    messages.success(request, f'CinePlayer {status}.')
    return redirect('cineplayer_settings')


@login_required
@user_passes_test(is_staff_or_superuser)
def player_create(request):
    if request.method == 'POST':
        form = PlayerConfigurationForm(request.POST)
        if form.is_valid():
            player = form.save(commit=False)
            if not player.custom_iframe_id_type:
                player.custom_iframe_id_type = 'tmdb'
            player.save()
            return redirect('player_list')
    else:
        form = PlayerConfigurationForm(initial={'custom_iframe_id_type': 'tmdb'})
    return render(request, 'core/player_form.html', {'form': form, 'action': 'Create'})


@login_required
@user_passes_test(is_staff_or_superuser)
def player_edit(request, player_id):
    player = get_object_or_404(PlayerConfiguration, id=player_id)
    if request.method == 'POST':
        form = PlayerConfigurationForm(request.POST, instance=player)
        if form.is_valid():
            form.save()
            return redirect('player_list')
    else:
        form = PlayerConfigurationForm(instance=player)
    return render(request, 'core/player_form.html', {'form': form, 'action': 'Edit', 'player': player})


@login_required
@user_passes_test(is_staff_or_superuser)
def player_delete(request, player_id):
    player = get_object_or_404(PlayerConfiguration, id=player_id)
    if request.method == 'POST':
        player.delete()
        return redirect('player_list')
    return render(request, 'core/object_delete_confirm.html', {
        'page_title': 'Delete Player Configuration',
        'object_label': 'the player configuration',
        'object_name': player.name,
        'cancel_url_name': 'player_list',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def toggle_player(request, player_id):
    if request.method == 'POST':
        player = get_object_or_404(PlayerConfiguration, id=player_id)
        player.is_active = not player.is_active
        player.save()
        return redirect('player_list')
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


@login_required
@user_passes_test(is_staff_or_superuser)
def navbar_item_list(request):
    navbar_items = NavbarItem.objects.all().order_by('order')
    return render(request, 'core/navbar_item_list.html', {'navbar_items': navbar_items})


@login_required
@user_passes_test(is_staff_or_superuser)
def navbar_item_create(request):
    if request.method == 'POST':
        form = NavbarItemForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('navbar_item_list')
    else:
        form = NavbarItemForm()
    return render(request, 'core/navbar_item_form.html', {'form': form, 'action': 'Create'})


@login_required
@user_passes_test(is_staff_or_superuser)
def navbar_item_edit(request, item_id):
    navbar_item = get_object_or_404(NavbarItem, id=item_id)
    if request.method == 'POST':
        form = NavbarItemForm(request.POST, instance=navbar_item)
        if form.is_valid():
            form.save()
            return redirect('navbar_item_list')
    else:
        form = NavbarItemForm(instance=navbar_item)
    return render(request, 'core/navbar_item_form.html', {'form': form, 'action': 'Edit'})


@login_required
@user_passes_test(is_staff_or_superuser)
def navbar_item_delete(request, item_id):
    navbar_item = get_object_or_404(NavbarItem, id=item_id)
    if request.method == 'POST':
        navbar_item.delete()
        return redirect('navbar_item_list')
    return render(request, 'core/object_delete_confirm.html', {
        'page_title': 'Delete Navbar Item',
        'object_label': 'the navbar item',
        'object_name': navbar_item.name,
        'cancel_url_name': 'navbar_item_list',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def ajax_toggle_navbar_item(request):
    if request.method == 'POST':
        item_id = int(request.POST.get('item_id'))
        navbar_item = get_object_or_404(NavbarItem, id=item_id)
        navbar_item.is_active = not navbar_item.is_active
        navbar_item.save()
        return JsonResponse({'success': True, 'is_active': navbar_item.is_active})


def _get_web_management_dashboard_payload():
    today = timezone.localdate()
    now = timezone.now()
    one_hour_ago = now - datetime.timedelta(hours=1)

    human_visits = WebsiteVisitorVisit.objects.filter(is_bot=False)

    total_visitors = WebsiteVisitor.objects.filter(
        models.Q(visits__isnull=True) | models.Q(visits__is_bot=False)
    ).distinct().count()
    total_pageviews = human_visits.count()
    visitors_today = WebsiteVisitor.objects.filter(
        visits__is_bot=False,
        visits__visited_at__date=today
    ).distinct().count()
    pageviews_today = human_visits.filter(visited_at__date=today).count()

    # Bots: the visitor tracker never records bot visits (the middleware
    # skips them to keep the visitor tables human-only), so counting
    # WebsiteVisitorVisit rows with is_bot=True would always show 0. The
    # request log is the real source, and its scraper signals are the same
    # ones the Probes & Bots page uses, so every admin page agrees.
    from collections import Counter
    from .models import BlockedIP, RequestLog
    from .probes import is_probe_path, is_scraper_agent

    one_day_ago = now - datetime.timedelta(hours=24)
    bot_total_visits = bot_visits_today = bot_visits_last_hour = 0
    bot_ip_counts = Counter()
    probe_requests_24h = 0
    blocked_responses_24h = 0
    try:
        for rec in (RequestLog.objects.order_by('-created_at')
                    .values('client_ip', 'path', 'status_code', 'created_at',
                            'user_agent')[:5000]):
            ua = (rec['user_agent'] or '').strip()
            when = rec['created_at']
            ip = (rec['client_ip'] or '').strip()
            if not ua or is_scraper_agent(ua):
                bot_total_visits += 1
                if timezone.localtime(when).date() == today:
                    bot_visits_today += 1
                if when >= one_hour_ago:
                    bot_visits_last_hour += 1
                if ip:
                    bot_ip_counts[ip] += 1
            if when >= one_day_ago:
                status = rec['status_code']
                if status == 403:
                    blocked_responses_24h += 1
                if status == 404 or is_probe_path(rec['path']):
                    probe_requests_24h += 1
    except Exception:
        logger.debug('web management: request-log bot scan failed', exc_info=True)

    bot_unique_ips = sorted(bot_ip_counts)
    bot_top_ips = list(
        {'ip_address': ip, 'request_count': count}
        for ip, count in sorted(bot_ip_counts.items(),
                                key=lambda item: (-item[1], item[0]))[:10]
    )

    # Blocking totals — the same numbers the Blocked IPs page cards show.
    from .ip_block import is_bot_block
    active_block_rows = BlockedIP.objects.filter(is_active=True)
    active_blocks = active_block_rows.count()
    bot_blocks = sum(1 for row in active_block_rows if is_bot_block(row))

    daily_visits = (
        human_visits
        .annotate(day=TruncDate('visited_at'))
        .values('day')
        .annotate(unique_visitors=Count('visitor', distinct=True), pageviews=Count('id'))
        .order_by('day')
    )
    chart_labels = [entry['day'].strftime('%Y-%m-%d') for entry in daily_visits]
    unique_chart_values = [entry['unique_visitors'] for entry in daily_visits]
    pageview_chart_values = [entry['pageviews'] for entry in daily_visits]

    top_routes = list(
        human_visits
        .values('path')
        .annotate(pageviews=Count('id'))
        .order_by('-pageviews')[:10]
    )

    recent_activity_queryset = (
        WebsiteVisitorVisit.objects
        .select_related('visitor')
        .order_by('-visited_at')[:20]
    )

    recent_activity = []
    for activity in recent_activity_queryset:
        recent_activity.append({
            'visitor__visitor_id': str(activity.visitor.visitor_id),
            'path': activity.path,
            'visited_at': timezone.localtime(activity.visited_at).isoformat(),
            'ip_address': activity.ip_address,
            'is_bot': activity.is_bot,
        })

    return {
        'metrics': {
            'total_visitors': total_visitors,
            'total_pageviews': total_pageviews,
            'visitors_today': visitors_today,
            'pageviews_today': pageviews_today,
            'bot_total_visits': bot_total_visits,
            'bot_visits_today': bot_visits_today,
            'bot_visits_last_hour': bot_visits_last_hour,
            'bot_unique_ip_count': len(bot_unique_ips),
            'bot_blocks': bot_blocks,
            'active_blocks': active_blocks,
            'blocked_responses_24h': blocked_responses_24h,
            'probe_requests_24h': probe_requests_24h,
        },
        'charts': {
            'labels': chart_labels,
            'unique_visitors': unique_chart_values,
            'pageviews': pageview_chart_values,
        },
        'top_routes': top_routes,
        'recent_activity': recent_activity,
        'bot_top_ips': bot_top_ips,
        'bot_unique_ips': bot_unique_ips,
    }


@login_required
@user_passes_test(is_staff_or_superuser)
def web_management_dashboard(request):
    dashboard = _get_web_management_dashboard_payload()
    recent_activity_queryset = (
        WebsiteVisitorVisit.objects
        .select_related('visitor')
        .order_by('-visited_at')[:20]
    )

    return render(request, 'core/web_management_dashboard.html', {
        'total_visitors': dashboard['metrics']['total_visitors'],
        'total_pageviews': dashboard['metrics']['total_pageviews'],
        'visitors_today': dashboard['metrics']['visitors_today'],
        'pageviews_today': dashboard['metrics']['pageviews_today'],
        'bot_total_visits': dashboard['metrics']['bot_total_visits'],
        'bot_visits_today': dashboard['metrics']['bot_visits_today'],
        'bot_visits_last_hour': dashboard['metrics']['bot_visits_last_hour'],
        'bot_unique_ip_count': dashboard['metrics']['bot_unique_ip_count'],
        'bot_blocks': dashboard['metrics']['bot_blocks'],
        'active_blocks': dashboard['metrics']['active_blocks'],
        'blocked_responses_24h': dashboard['metrics']['blocked_responses_24h'],
        'probe_requests_24h': dashboard['metrics']['probe_requests_24h'],
        'chart_labels_json': json.dumps(dashboard['charts']['labels']),
        'unique_chart_values_json': json.dumps(dashboard['charts']['unique_visitors']),
        'pageview_chart_values_json': json.dumps(dashboard['charts']['pageviews']),
        'top_routes': dashboard['top_routes'],
        'recent_activity': recent_activity_queryset,
        'bot_top_ips': dashboard['bot_top_ips'],
        'bot_unique_ips': dashboard['bot_unique_ips'],
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def ajax_web_management_dashboard(request):
    dashboard = _get_web_management_dashboard_payload()
    return JsonResponse({
        'metrics': dashboard['metrics'],
        'charts': dashboard['charts'],
        'top_routes': dashboard['top_routes'],
        'recent_activity': dashboard['recent_activity'],
        'bot_top_ips': dashboard['bot_top_ips'],
        'bot_unique_ips': dashboard['bot_unique_ips'],
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def system_resource_dashboard(request):
    # Get initial system stats
    cpu_percent = psutil.cpu_percent(interval=1)
    cpu_count = psutil.cpu_count(logical=True)
    cpu_freq = psutil.cpu_freq()
    
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    
    disk = psutil.disk_usage('/')
    disk_io = psutil.disk_io_counters()
    
    network_io = psutil.net_io_counters()
    
    boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.datetime.now() - boot_time
    
    return render(request, 'core/system_resource_dashboard.html', {
        'cpu_percent': cpu_percent,
        'cpu_count': cpu_count,
        'cpu_freq_current': cpu_freq.current if cpu_freq else 0,
        'cpu_freq_max': cpu_freq.max if cpu_freq else 0,
        'memory_total': memory.total,
        'memory_used': memory.used,
        'memory_percent': memory.percent,
        'memory_available': memory.available,
        'swap_total': swap.total,
        'swap_used': swap.used,
        'swap_percent': swap.percent,
        'disk_total': disk.total,
        'disk_used': disk.used,
        'disk_percent': disk.percent,
        'disk_free': disk.free,
        'disk_read_bytes': disk_io.read_bytes if disk_io else 0,
        'disk_write_bytes': disk_io.write_bytes if disk_io else 0,
        'network_sent_bytes': network_io.bytes_sent if network_io else 0,
        'network_recv_bytes': network_io.bytes_recv if network_io else 0,
        'boot_time': boot_time,
        'uptime_days': uptime.days,
        'uptime_hours': uptime.seconds // 3600,
        'uptime_minutes': (uptime.seconds % 3600) // 60,
        'os_name': platform.system(),
        'os_version': platform.version(),
        'os_architecture': platform.architecture()[0],
        'python_version': platform.python_version()
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def ajax_system_resource_dashboard(request):
    # Get real-time system stats
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_count = psutil.cpu_count(logical=True)
    cpu_freq = psutil.cpu_freq()
    
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    
    disk = psutil.disk_usage('/')
    disk_io = psutil.disk_io_counters()
    
    network_io = psutil.net_io_counters()
    
    boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.datetime.now() - boot_time
    
    return JsonResponse({
        'cpu': {
            'percent': cpu_percent,
            'count': cpu_count,
            'freq_current': cpu_freq.current if cpu_freq else 0,
            'freq_max': cpu_freq.max if cpu_freq else 0
        },
        'memory': {
            'total': memory.total,
            'used': memory.used,
            'percent': memory.percent,
            'available': memory.available
        },
        'swap': {
            'total': swap.total,
            'used': swap.used,
            'percent': swap.percent
        },
        'disk': {
            'total': disk.total,
            'used': disk.used,
            'percent': disk.percent,
            'free': disk.free,
            'read_bytes': disk_io.read_bytes if disk_io else 0,
            'write_bytes': disk_io.write_bytes if disk_io else 0
        },
        'network': {
            'sent_bytes': network_io.bytes_sent if network_io else 0,
            'recv_bytes': network_io.bytes_recv if network_io else 0
        },
        'system': {
            'boot_time': boot_time.isoformat(),
            'uptime_days': uptime.days,
            'uptime_hours': uptime.seconds // 3600,
            'uptime_minutes': (uptime.seconds % 3600) // 60,
            'os_name': platform.system(),
            'os_version': platform.version(),
            'os_architecture': platform.architecture()[0],
            'python_version': platform.python_version()
        }
    })


# ============================================================================
# Videasy Player integration
# ============================================================================

# Persistent HTTP session pool for proxy (reuse connections to CDN)
_PROXY_SESSION = None
_PROXY_ADAPTER = None


def _get_proxy_session():
    global _PROXY_SESSION
    if _PROXY_SESSION is None:
        import requests as _req_mod
        _PROXY_SESSION = _req_mod.Session()
        adapter = _req_mod.adapters.HTTPAdapter(
            pool_connections=20,
            pool_maxsize=50,
            max_retries=2,
            pool_block=False
        )
        _PROXY_SESSION.mount('http://', adapter)
        _PROXY_SESSION.mount('https://', adapter)
    return _PROXY_SESSION


_VIDEASY_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_VIDEASY_ORIGIN = "https://player.videasy.to"
_PROXY_DNS_SERVERS = ['1.1.1.1', '8.8.8.8', '1.0.0.1', '8.8.4.4']


@require_GET
def health_view(request):
    """curl-able /api/health — reports server health and last Android API error."""
    from .api_errors import recent_errors
    from django.db import connections
    import time

    result = {
        'status': 'ok',
        'timestamp': time.time(),
        'databases': {},
        'last_api_error': None,
        'api_error_count': len(recent_errors()),
    }

    # DB connectivity check
    for alias in ['default', 'external']:
        try:
            start = time.time()
            connections[alias].ensure_connection()
            result['databases'][alias] = {
                'status': 'ok',
                'latency_ms': round((time.time() - start) * 1000, 1),
            }
        except Exception as e:
            result['status'] = 'degraded'
            result['databases'][alias] = {
                'status': 'error',
                'error': str(e)[:200],
            }

    # Last API error from in-memory ring buffer
    errors = recent_errors(limit=1)
    if errors:
        last = errors[0]
        result['last_api_error'] = {
            'time': last.get('time'),
            'view': last.get('view'),
            'method': last.get('method'),
            'path': last.get('path'),
            'error_type': last.get('error_type'),
            'message': last.get('message', '')[:200],
        }

    status_code = 200 if result['status'] == 'ok' else 503
    return JsonResponse(result, status=status_code)


@require_GET
def cookies_view(request):
    """Fetch DDoS-Guard cookies from videasy.to (needed by the player CDN)."""
    try:
        cj = http.cookiejar.CookieJar()
        opener = _urlreq.build_opener(
            _urlreq.HTTPCookieProcessor(cj)
        )
        opener.addheaders = [("User-Agent", _VIDEASY_USER_AGENT)]
        resp = opener.open(f"{_VIDEASY_ORIGIN}/", timeout=10)
        resp.read()
        cookies = {}
        for c in cj:
            cookies[c.name] = c.value
        for hdr in resp.headers.get_all("Set-Cookie") or []:
            m = __import__('re').match(r"([^=]+)=([^;]+)", hdr)
            if m:
                cookies[m.group(1).strip()] = m.group(2).strip()
        return JsonResponse({"cookies": cookies, "count": len(cookies)})
    except Exception as e:
        return JsonResponse({"cookies": {}, "error": str(e)})


def proxy_view(request, target):
    """
    Proxy requests to CDN / stream servers.
    Reconstructs the full URL from the path, tries with videasy.to Origin,
    then falls back without Origin on 403.
    Rewrites .m3u8 manifests so segment URLs also go through the proxy.
    """
    import requests as _req
    import urllib.parse as _urlparse

    # Reconstruct full URL — auto-detect http vs https
    if target.startswith("http/"):
        target_url = "http://" + target[5:]
    else:
        target_url = "https://" + target

    from .url_guard import UnsafeURLError, ensure_public_url, guarded_get
    try:
        target_url = ensure_public_url(target_url)
    except UnsafeURLError as exc:
        return HttpResponse(f"Proxy error: {exc}", status=400)

    headers = {
        'User-Agent': _VIDEASY_USER_AGENT,
        'Origin': _VIDEASY_ORIGIN,
        'Referer': f"{_VIDEASY_ORIGIN}/",
    }

    try:
        _session = _get_proxy_session()
        try:
            resp = guarded_get(target_url, session=_session, headers=headers,
                               timeout=(5, 120), stream=True)
        except UnsafeURLError as exc:
            return HttpResponse(f"Proxy error: {exc}", status=400)
        if resp.status_code == 403:
            headers.pop('Origin', None)
            headers.pop('Referer', None)
            resp.close()
            _session = _get_proxy_session()
            try:
                resp = guarded_get(target_url, session=_session, headers=headers,
                                   timeout=(5, 120), stream=True)
            except UnsafeURLError as exc:
                return HttpResponse(f"Proxy error: {exc}", status=400)
        if resp.status_code >= 400:
            resp.close()
            return HttpResponse(f"Proxy error: {resp.status_code}", status=resp.status_code)

        content_type = resp.headers.get('Content-Type', 'application/octet-stream')
        content_length = resp.headers.get('Content-Length')

        # If it's an m3u8 manifest, rewrite segment URLs to go through proxy
        if 'mpegurl' in content_type.lower() or target_url.endswith('.m3u8'):
            body = resp.text
            resp.close()
            base_url = target_url.rsplit('/', 1)[0] + '/'
            origin = request.build_absolute_uri('/proxy/').rstrip('/')
            lines = body.split('\n')
            rewritten = []
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    # Resolve relative URLs
                    if not stripped.startswith('http'):
                        stripped = _urlparse.urljoin(base_url, stripped)
                    try:
                        u = _urlparse.urlparse(stripped)
                        proxy_path = u.hostname + u.path
                        if u.query:
                            proxy_path += '?' + u.query
                        rewritten.append(f"{origin}/{proxy_path}")
                    except Exception:
                        rewritten.append(stripped)
                elif 'URI="' in stripped:
                    # Rewrite URIs in tags
                    import re
                    def rewrite_uri(m):
                        uri = m.group(1)
                        if not uri.startswith('http'):
                            uri = _urlparse.urljoin(base_url, uri)
                        try:
                            u = _urlparse.urlparse(uri)
                            proxy_path = u.hostname + u.path
                            if u.query:
                                proxy_path += '?' + u.query
                            return f'URI="{origin}/{proxy_path}"'
                        except Exception:
                            return m.group(0)
                    rewritten.append(re.sub(r'URI="([^"]+)"', rewrite_uri, stripped))
                else:
                    rewritten.append(stripped)
            body = '\n'.join(rewritten)
            http_resp = HttpResponse(body, content_type='application/vnd.apple.mpegurl')
            http_resp['Access-Control-Allow-Origin'] = '*'
            http_resp['Cache-Control'] = 'public, max-age=5, stale-while-revalidate=10'
            return http_resp

        # Non-m3u8: stream as-is, but fix content type for CDN-obfuscated video segments
        # CDN returns text/html for .html segments that are actually MPEG-TS video data
        real_content_type = content_type
        first_chunk = resp.iter_content(chunk_size=524288)
        try:
            first_data = next(first_chunk)
        except StopIteration:
            first_data = b''
        # Detect MPEG-TS by magic bytes (0x47) or FFmpeg signature
        if first_data and (first_data[:1] == b'\x47' or b'FFmpeg' in first_data[:32] or b'ftyp' in first_data[:12]):
            real_content_type = 'video/mp2t'
        def stream_chunks():
            try:
                if first_data:
                    yield first_data
                for chunk in first_chunk:
                    if chunk:
                        yield chunk
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        http_resp = StreamingHttpResponse(stream_chunks(), content_type=real_content_type)
        http_resp['Access-Control-Allow-Origin'] = '*'
        http_resp['Accept-Ranges'] = 'bytes'
        if content_length:
            http_resp['Content-Length'] = content_length
        # Force aggressive browser caching for video segments
        http_resp['Cache-Control'] = 'public, max-age=86400, stale-while-revalidate=604800'
        for hdr in ('ETag', 'Last-Modified'):
            val = resp.headers.get(hdr)
            if val:
                http_resp[hdr] = val
        return http_resp
    except Exception as e:
        return HttpResponse(f"Proxy error: {e}", status=502)


def _custom_dns_resolve(host):
    """Resolve hostname using custom DNS servers (1.1.1.1 / 8.8.8.8) for faster connections."""
    import socket as _sock
    import struct
    for dns in _PROXY_DNS_SERVERS:
        try:
            sock = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            sock.settimeout(2)
            # Build DNS query
            tid = b'\x12\x34'
            header = tid + b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
            question = b''
            for label in host.split('.'):
                question += struct.pack('!B', len(label)) + label.encode()
            question += b'\x00' + struct.pack('!HH', 1, 1)  # A record, IN class
            sock.sendto(header + question, (dns, 53))
            data = sock.recv(512)
            sock.close()
            # Parse answer — skip header (12 bytes) + question, read answer
            offset = 12 + len(question)
            if len(data) > offset + 12:
                rdlength = struct.unpack('!H', data[offset + 10:offset + 12])[0]
                if rdlength == 4:
                    ip = '.'.join(str(b) for b in data[offset + 12:offset + 16])
                    return ip
        except Exception:
            continue
    return None  # Fall back to system DNS


def _proxy_fetch_url(target_url, origin=None, referer=None):
    """Fetch a URL using streaming for better video playback.
    Uses the requests library for proper SSL/TLS handling."""
    import requests as _req
    try:
        headers = {
            'User-Agent': _VIDEASY_USER_AGENT,
        }
        if origin:
            headers['Origin'] = origin
        if referer:
            headers['Referer'] = referer
        # Use requests with streaming for proper SSL + chunked transfer
        # Do NOT replace hostname with IP — that breaks SSL cert verification
        from .url_guard import UnsafeURLError, ensure_public_url, guarded_get
        try:
            target_url = ensure_public_url(target_url)
        except UnsafeURLError as exc:
            return HttpResponse(f"Proxy error: {exc}", status=400)
        resp = guarded_get(
            target_url,
            headers=headers,
            timeout=(8, 30),  # connect timeout, read timeout
            stream=True,
        )
        if resp.status_code == 403 and origin:
            resp.close()
            return None
        if resp.status_code >= 400:
            resp.close()
            return HttpResponse(f"Proxy error: {resp.status_code}", status=resp.status_code)
        content_type = resp.headers.get('Content-Type', 'application/octet-stream')
        content_length = resp.headers.get('Content-Length')
        def stream_chunks():
            try:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        http_resp = StreamingHttpResponse(stream_chunks(), content_type=content_type)
        http_resp['Access-Control-Allow-Origin'] = '*'
        http_resp['Accept-Ranges'] = 'bytes'
        if content_length:
            http_resp['Content-Length'] = content_length
        # Pass through cache and other useful headers
        for hdr in ('Cache-Control', 'ETag', 'Last-Modified', 'Accept-Ranges'):
            val = resp.headers.get(hdr)
            if val:
                http_resp[hdr] = val
        return http_resp
    except Exception as e:
        return HttpResponse(f"Proxy error: {e}", status=502)


def videasy_player_view(request):
    """
    Serve the Videasy player page.
    Query params: tmdb_id, type (movie|tv), season, episode
    The player auto-loads sources when tmdb_id is provided.
    """
    tmdb_id = request.GET.get('tmdb_id', '')
    media_type = request.GET.get('type', 'movie')
    season = request.GET.get('season', '')
    episode = request.GET.get('episode', '')
    is_admin = request.user.is_staff or request.user.is_superuser
    return render(request, 'core/videasy_player.html', {
        'tmdb_id': tmdb_id,
        'media_type': media_type,
        'season': season,
        'episode': episode,
        'is_admin': is_admin,
    })


def videasy_player_frame_view(request):
    """Serve the Videasy HLS player iframe."""
    return render(request, 'core/videasy_player_frame.html')


def direct_player_view(request):
    """
    Browser player that plays the EXTRACTED links directly.

    Unlike /api/player/ (which serves the webplayer embed page), this page
    calls /api/links/ server-side-extracted links and plays them with
    hls.js in the browser — raw CDN url first, /proxy/ route as fallback.
    Query params: tmdb_id (required), type (movie|tv), season, episode, lang
    """
    return render(request, 'core/direct_player.html', {
        'tmdb_id': request.GET.get('tmdb_id', ''),
        'media_type': request.GET.get('type', 'movie'),
        'season': request.GET.get('season', ''),
        'episode': request.GET.get('episode', ''),
        'lang': request.GET.get('lang', ''),
    })


def series_extractor_view(request):
    """
    Standalone series source extractor page.
    Accepts ?url=<videasy-url> to pre-fill the URL input.
    """
    url = request.GET.get('url', '')
    return render(request, 'core/series_extractor.html', {'prefill_url': url})


@require_GET
def videasy_sources_view(request):
    """
    Fetch all playable sources from speedracelight API for a given TMDB ID.
    Returns sources from all servers with quality, language, and direct URLs.
    Used by the detail pages to list all available stream links.
    """
    import time as _time

    # Play block: refuse stream sources for a logged-in user whose account is
    # under a 'play' or 'full' block (movies/series must not play).
    if request.user.is_authenticated:
        prof = SyncedUser.objects.filter(email__iexact=request.user.email).first()
        if prof and prof.block_mode in ('play', 'full'):
            return JsonResponse({'success': False, 'error': 'Playback is currently restricted on this account.', 'servers': []}, status=403)

    tmdb_id = request.GET.get('tmdb_id', '')
    media_type = request.GET.get('media_type', 'movie')
    season_id = request.GET.get('season_id', '')
    episode_id = request.GET.get('episode_id', '')

    if not tmdb_id:
        return JsonResponse({'success': False, 'error': 'tmdb_id is required', 'servers': []})

    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Invalid tmdb_id', 'servers': []})

    timestamp = str(int(_time.time() * 1000))

    # Step 1: Get TMDB info
    try:
        tmdb_info = _vk_fetch_tmdb_info(tmdb_id, media_type)
    except Exception as e:
        tmdb_info = {'title': '', 'year': '', 'imdbId': ''}
        logger.warning("Could not fetch TMDB info: %s", e)

    title = tmdb_info.get('title', '')
    year = tmdb_info.get('year', '')
    imdb_id = tmdb_info.get('imdbId', '')

    # Step 2: Get seed
    seed = ''
    for attempt in range(2):
        try:
            seed = _vk_get_seed(_VIDKING_API_BASE, tmdb_id)
            break
        except Exception as e:
            if attempt == 0:
                _time.sleep(0.3)

    if not seed:
        return JsonResponse({'success': False, 'error': 'Could not fetch seed', 'servers': []})

    # Step 3: Fetch from servers (stop after 3 with results)
    server_results = []
    servers_with_sources = 0
    for server_name, endpoint in _VIDKING_SERVERS.items():
        try:
            result = _vk_fetch_sources_for_server(
                server_name, endpoint, tmdb_id, media_type,
                title=title, year=year, season_id=season_id,
                episode_id=episode_id, imdb_id=imdb_id,
                seed=seed, timestamp=timestamp,
            )
            sources = result.get('sources', [])
            subtitles = result.get('subtitles', [])
            server_sources = []
            if isinstance(sources, list):
                for src in sources:
                    if isinstance(src, dict) and src.get('url'):
                        lang = src.get('language', '') or src.get('audioLanguage', '') or src.get('audio', '') or ''
                        quality = src.get('quality', '?')
                        server_sources.append({
                            'url': src['url'],
                            'quality': quality,
                            'language': lang,
                            'type': _guess_video_type(src.get('url', '')),
                        })
            if server_sources:
                server_results.append({
                    'server': server_name,
                    'sources': server_sources,
                    'subtitles': subtitles if isinstance(subtitles, list) else [],
                })
                servers_with_sources += 1
                if servers_with_sources >= 3:
                    break
        except Exception as e:
            logger.debug("Server %s failed: %s", server_name, e)
            continue

    return JsonResponse({
        'success': True,
        'title': title,
        'year': year,
        'tmdb_id': tmdb_id,
        'media_type': media_type,
        'season': season_id,
        'episode': episode_id,
        'servers': server_results,
    })


@require_GET
def player_episodes_view(request):
    """
    Fetch seasons & episodes list for a TV show from speedracelight DB.
    Used by the player's season/episode dropdowns.
    """
    tmdb_id = request.GET.get('tmdb_id', '')
    season = request.GET.get('season', '')
    if not tmdb_id:
        return JsonResponse({'success': False, 'error': 'tmdb_id is required'})

    db_base = 'https://db.speedracelight.com/3'
    try:
        # Get show info (number of seasons)
        show_url = f'{db_base}/tv/{tmdb_id}?language=en'
        r = requests.get(show_url, timeout=10)
        r.raise_for_status()
        show_data = r.json()
        num_seasons = show_data.get('number_of_seasons', 1)

        seasons_list = []
        for i in range(1, num_seasons + 1):
            seasons_list.append({'number': i, 'name': f'Season {i}'})

        episodes_list = []
        if season:
            ep_url = f'{db_base}/tv/{tmdb_id}/season/{season}?language=en'
            r2 = requests.get(ep_url, timeout=10)
            r2.raise_for_status()
            ep_data = r2.json()
            for ep in ep_data.get('episodes', []):
                episodes_list.append({
                    'number': ep.get('episode_number', 0),
                    'name': ep.get('name', ''),
                    'overview': ep.get('overview', ''),
                    'air_date': ep.get('air_date', ''),
                    'still_path': ep.get('still_path', ''),
                    'vote_average': ep.get('vote_average', 0),
                    'runtime': ep.get('runtime', 0),
                })

        return JsonResponse({
            'success': True,
            'seasons': seasons_list,
            'episodes': episodes_list,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


_KNOWN_LANG_NAMES = {'hindi','english','tamil','telugu','kannada','bengali','malayalam','marathi',
                     'german','spanish','portuguese','french','japanese','korean','chinese',
                     'hinglish','thai','indonesian','turkish','arabic','russian','polish','italian','dutch'}


@require_GET
def api_links_view(request):
    """
    Direct-links API for native apps (ExoPlayer / VLC / MX Player etc).

    Instead of loading the webplayer embed, apps call this endpoint with a
    TMDB id and get back the extracted stream links — both the RAW CDN urls
    (try these first) and /proxy/ routes that rewrite HLS manifests so any
    player can stream even CDNs that block unknown clients.

    Params: tmdb_id (required), type (movie|tv), season, episode,
            lang (optional — filter sources by language, e.g. lang=hindi),
            server (optional — restrict to one server name).

    Response: { success, title, year, tmdbId, type, links: [
      { server, quality, language, type: 'hls'|'mp4', url, proxy_url } ] }
    """
    import time as _time
    import concurrent.futures

    tmdb_id = request.GET.get('tmdb_id', '')
    media_type = request.GET.get('type', 'movie')
    season = request.GET.get('season', '')
    episode = request.GET.get('episode', '')
    lang_filter = (request.GET.get('lang', '') or '').strip().lower()
    server_filter = (request.GET.get('server', '') or '').strip().lower()

    if not tmdb_id:
        return JsonResponse({'success': False, 'error': 'tmdb_id is required', 'links': []})
    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Invalid tmdb_id', 'links': []})

    timestamp = str(int(_time.time() * 1000))

    # TMDB info + seed in parallel (same as videasy_sources_view)
    def _fetch_tmdb():
        try:
            return _vk_fetch_tmdb_info(tmdb_id, media_type)
        except Exception:
            return {'title': '', 'year': '', 'imdbId': ''}

    def _fetch_seed():
        for attempt in range(2):
            try:
                return _vk_get_seed(_VIDKING_API_BASE, tmdb_id)
            except Exception:
                if attempt == 0:
                    _time.sleep(0.2)
        return ''

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        tmdb_future = pool.submit(_fetch_tmdb)
        seed_future = pool.submit(_fetch_seed)
        tmdb_info = tmdb_future.result()
        seed = seed_future.result()

    if not seed:
        return JsonResponse({'success': False, 'error': 'Could not fetch seed', 'links': []})

    title = tmdb_info.get('title', '')
    year = tmdb_info.get('year', '')
    imdb_id = tmdb_info.get('imdbId', '')

    def _fetch_one(server_name, endpoint):
        try:
            return _vk_fetch_sources_for_server(
                server_name, endpoint, tmdb_id, media_type,
                title=title, year=year, season_id=season,
                episode_id=episode, imdb_id=imdb_id,
                seed=seed, timestamp=timestamp,
            )
        except Exception:
            return {'sources': []}

    links = []
    seen_urls = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        futures = {pool.submit(_fetch_one, name, ep): name for name, ep in _VIDKING_SERVERS.items()}
        for future in concurrent.futures.as_completed(futures):
            server_name = futures[future]
            try:
                result = future.result()
            except Exception:
                continue
            sources = result.get('sources', []) if isinstance(result, dict) else []
            if not isinstance(sources, list):
                continue
            for src in sources:
                if not (isinstance(src, dict) and src.get('url') and src['url'] not in seen_urls):
                    continue
                seen_urls.add(src['url'])
                lang = (src.get('language', '') or src.get('audioLanguage', '') or src.get('audio', '') or '').strip()
                quality = (src.get('quality', '?') or '?').strip()
                # Some servers put the LANGUAGE in the quality field ("Hindi") —
                # normalize so apps can filter/label properly.
                if not lang and quality.lower() in _KNOWN_LANG_NAMES:
                    lang = quality
                    quality = 'Auto'
                if lang_filter and lang_filter not in (lang + ' ' + quality).lower():
                    continue
                if server_filter and server_filter != server_name.lower():
                    continue
                url = src['url']
                is_hls = '.m3u8' in url or 'mpegurl' in url
                links.append({
                    'server': server_name,
                    'quality': quality,
                    'language': lang or 'Original',
                    'type': 'hls' if is_hls else 'mp4',
                    'url': url,
                    # Proxy route: rewrites the manifest so segments stream
                    # through our proxy — use when the raw url 403s in the app.
                    'proxy_url': request.build_absolute_uri(f"/proxy/{url.split('://', 1)[-1]}"),
                    'headers': {
                        # Referer some CDNs expect; harmless otherwise.
                        'Referer': _VIDEASY_ORIGIN + '/',
                        'Origin': _VIDEASY_ORIGIN,
                    },
                })

    resp = JsonResponse({
        'success': True,
        'title': title,
        'year': year,
        'tmdbId': tmdb_id,
        'type': media_type,
        'season': season,
        'episode': episode,
        'count': len(links),
        'links': links,
    })
    # Native apps / WebView clients are not same-origin — allow any caller.
    resp['Access-Control-Allow-Origin'] = '*'
    return resp


# ===== Subtitle Fetcher =====
@require_GET
def player_subtitles_view(request):
    """Fetch subtitles for a movie/series episode from opensubtitles.com API."""
    import time as _time
    imdb_id = request.GET.get('imdb_id', '')
    tmdb_id = request.GET.get('tmdb_id', '')
    season = request.GET.get('season', '')
    episode = request.GET.get('episode', '')
    lang = request.GET.get('lang', 'en')

    if not imdb_id and not tmdb_id:
        return JsonResponse({'success': False, 'error': 'imdb_id or tmdb_id required', 'subtitles': []})

    # Try opensubtitles.com API (free tier — 5 req/s, 100/day without key)
    subs = []
    try:
        headers = {'Api-Key': 'T6m9wJYrWVYjvVZvRqSZp5SvxA1IxbqY', 'User-Agent': 'newmovies v1.0'}
        params = {'languages': lang}
        if imdb_id:
            params['imdb_id'] = imdb_id.lstrip('tt')
        elif tmdb_id:
            params['tmdb_id'] = tmdb_id
        if season and episode and season.isdigit() and episode.isdigit():
            params['season_number'] = int(season)
            params['episode_number'] = int(episode)

        r = requests.get('https://api.opensubtitles.com/api/v1/subtitles', headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            for item in (data.get('data', []) or [])[:20]:
                attr = item.get('attributes', {})
                file_id = attr.get('files', [{}])[0].get('file_id') if attr.get('files') else None
                if file_id:
                    subs.append({
                        'id': file_id,
                        'lang': attr.get('language', lang),
                        'lang_name': attr.get('language', lang),
                        'label': attr.get('release', '') or f"{attr.get('language', lang)} subtitle",
                        'format': attr.get('files', [{}])[0].get('format', 'srt') if attr.get('files') else 'srt',
                    })
    except Exception as e:
        logger.debug('Subtitle fetch failed: %s', e)

    # Fallback: search subdl.com
    if not subs:
        try:
            media_type = 'movie'
            if season and episode:
                media_type = 'tv'
            params_sd = {'type': media_type, 'languages': lang}
            if imdb_id:
                params_sd['imdb_id'] = imdb_id
            elif tmdb_id:
                params_sd['tmdb_id'] = tmdb_id
            if season:
                params_sd['season_number'] = season
            if episode:
                params_sd['episode_number'] = episode

            r2 = requests.get('https://api.subdl.com/auto', params=params_sd, timeout=10)
            if r2.status_code == 200:
                data2 = r2.json()
                for sub in (data2.get('subtitles', []) or [])[:10]:
                    subs.append({
                        'id': sub.get('url', ''),
                        'lang': sub.get('lang', lang),
                        'lang_name': sub.get('lang', lang),
                        'label': sub.get('release_name', '') or f"{sub.get('lang', lang)} subtitle",
                        'format': sub.get('format', 'srt'),
                        'url': sub.get('url', ''),
                    })
        except Exception as e:
            logger.debug('Subdl fetch failed: %s', e)

    return JsonResponse({'success': True, 'subtitles': subs})


@require_GET
def stremio_subtitles_view(request):
    """
    Fetch subtitles from Stremio OpenSubtitles addon.
    GET /ajax/stremio-subtitles/?type=movie&id=tt1375666&lang=eng
    GET /ajax/stremio-subtitles/?type=movie&tmdb_id=550&lang=eng
    GET /ajax/stremio-subtitles/?type=tv&id=tt0903747&season=1&episode=1&lang=eng
    """
    media_type = request.GET.get('type', 'movie')  # 'movie' or 'tv'
    imdb_id = request.GET.get('id', '')  # e.g. tt1375666
    tmdb_id = request.GET.get('tmdb_id', '')  # alternative: resolve via TMDB
    lang = request.GET.get('lang', 'eng')  # ISO 639-2 language code
    season = request.GET.get('season', '')
    episode = request.GET.get('episode', '')

    # If no imdb_id but tmdb_id provided, resolve via TMDB API
    if not imdb_id and tmdb_id:
        try:
            client = get_data_client()
            if media_type == 'tv':
                details = client.get_series_details(tmdb_id)
            else:
                details = client.get_movie_details(tmdb_id)
            imdb_id = details.get('imdb_id', '') if details else ''
        except Exception as e:
            logger.debug('TMDB→IMDB resolution failed: %s', e)

    if not imdb_id:
        return JsonResponse({'success': False, 'error': 'id (imdb_id) or tmdb_id is required', 'subtitles': []})

    # Ensure imdb_id has tt prefix
    if not imdb_id.startswith('tt'):
        imdb_id = 'tt' + imdb_id

    # Build Stremio API URL
    stremio_type = 'tv' if media_type == 'tv' else 'movie'
    stremio_id = imdb_id
    if media_type == 'tv' and season and episode:
        stremio_id = f"{imdb_id}:s{season}e{episode}"

    url = f'https://opensubtitles-v3.strem.io/subtitles/{stremio_type}/{stremio_id}.json'

    subs = []
    try:
        resp = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0'
        })
        if resp.status_code == 200:
            data = resp.json()
            raw_subs = data.get('subtitles', [])

            # Stremio returns: { id, url, subtitle_manifest_url, lang, lang_code }
            # We want direct VTT/SRT URLs
            for sub in raw_subs:
                sub_lang = sub.get('lang_code', sub.get('lang', ''))
                sub_lang_name = sub.get('lang', sub_lang)
                sub_url = sub.get('url', '')
                sub_id = sub.get('id', '')

                if not sub_url:
                    continue

                # Filter by requested language if specified
                if lang and lang != 'all':
                    if sub_lang and lang.lower() not in [sub_lang.lower(), sub_lang_name.lower()]:
                        # Also try matching first 3 chars (eng matches en)
                        if not sub_lang.lower().startswith(lang.lower()[:2]):
                            continue

                subs.append({
                    'id': sub_id,
                    'url': sub_url,
                    'lang': sub_lang,
                    'lang_name': sub_lang_name,
                    'label': f"{sub_lang_name} ({sub_lang})",
                    'format': 'vtt',  # Stremio serves WebVTT by default
                })

            # Deduplicate by lang_code, keep first (best quality)
            seen = set()
            unique_subs = []
            for sub in subs:
                key = sub['lang']
                if key not in seen:
                    seen.add(key)
                    unique_subs.append(sub)
            subs = unique_subs

    except requests.Timeout:
        logger.debug('Stremio subtitles timeout for %s', stremio_id)
    except requests.RequestException as e:
        logger.debug('Stremio subtitles request failed: %s', e)
    except (ValueError, KeyError) as e:
        logger.debug('Stremio subtitles parse error: %s', e)

    return JsonResponse({
        'success': True,
        'subtitles': subs,
        'source': 'stremio',
        'query': {
            'type': media_type,
            'id': imdb_id,
            'season': season,
            'episode': episode,
            'lang': lang,
        }
    })


# ===== Subscriber System =====
from django.contrib.auth.decorators import login_required
from core.models import Subscriber, EmailMessage, EmailDelivery
import json
from django.core.mail import send_mass_mail, send_mail
from django.conf import settings as django_settings


def ajax_subscribe(request):
    """AJAX endpoint: subscribe an email."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'message': 'Invalid method'}, status=405)
    try:
        data = json.loads(request.body)
        email = (data.get('email') or '').strip().lower()
        if not email or '@' not in email:
            return JsonResponse({'ok': False, 'message': 'Invalid email address'})
        sub, created = Subscriber.objects.get_or_create(email=email)
        if not created and not sub.is_active:
            sub.is_active = True
            sub.unsubscribed_at = None
            sub.save()
            return JsonResponse({'ok': True, 'message': 'Welcome back! You are now subscribed.'})
        if created:
            return JsonResponse({'ok': True, 'message': 'Successfully subscribed!'})
        return JsonResponse({'ok': True, 'message': 'You are already subscribed.'})
    except Exception as e:
        return JsonResponse({'ok': False, 'message': 'Error: ' + str(e)})


def unsubscribe_page(request):
    """Unsubscribe page — user enters email, sees a checkbox to confirm."""
    email = request.GET.get('email', '').strip()
    msg = ''
    success = False
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        action = request.POST.get('action', '')
        if email and action == 'unsubscribe':
            try:
                sub = Subscriber.objects.get(email=email, is_active=True)
                sub.is_active = False
                sub.unsubscribed_at = timezone.now()
                sub.save()
                msg = 'You have been unsubscribed successfully.'
                success = True
            except Subscriber.DoesNotExist:
                msg = 'Email not found or already unsubscribed.'
    return render(request, 'core/unsubscribe.html', {
        'email': email, 'msg': msg, 'success': success,
    })


@login_required
def admin_subscribers(request):
    """Admin page to view subscribers, send emails.

    Auto-syncs all registered User and SyncedUser emails into the
    Subscriber table so every user who ever signed up or logged in
    from the Android app is visible here and can be emailed.
    """
    if not request.user.is_staff:
        return HttpResponseForbidden('Staff only')

    # --- Auto-sync: create Subscriber rows for any User/SyncedUser email not yet present ---
    from .models import SyncedUser
    existing_emails = set(Subscriber.objects.values_list('email', flat=True))
    new_subs = []
    # Django auth users
    for u in User.objects.exclude(email='').exclude(email__isnull=True):
        if u.email.lower() not in existing_emails:
            new_subs.append(Subscriber(email=u.email.lower(), is_active=True))
            existing_emails.add(u.email.lower())
    # Android-synced users
    for su in SyncedUser.objects.exclude(email='').exclude(email__isnull=True):
        if su.email.lower() not in existing_emails:
            new_subs.append(Subscriber(email=su.email.lower(), is_active=True))
            existing_emails.add(su.email.lower())
    if new_subs:
        Subscriber.objects.bulk_create(new_subs, ignore_conflicts=True)

    # Build lookup maps for registration and plan info
    user_map = {u.email.lower(): u for u in User.objects.exclude(email='').exclude(email__isnull=True)}
    synced_map = {s.email.lower(): s for s in SyncedUser.objects.exclude(email='').exclude(email__isnull=True)}

    # Annotate each subscriber with user info
    sub_list = []
    for sub in Subscriber.objects.all().order_by('-created_at'):
        em = sub.email.lower()
        is_registered = em in user_map or em in synced_map
        plan = ''
        if em in synced_map:
            su = synced_map[em]
            plan = su.plan or ('Subscribed' if su.is_subscribed else '')
        elif em in user_map:
            u = user_map[em]
            if u.is_superuser:
                plan = 'Superadmin'
            elif u.is_staff:
                plan = 'Admin'
        sub_list.append({
            'sub': sub,
            'is_registered': is_registered,
            'plan': plan,
        })

    total_active = Subscriber.objects.filter(is_active=True).count()
    total_inactive = Subscriber.objects.filter(is_active=False).count()
    sent_messages = EmailMessage.objects.select_related('sent_by').all()[:20]
    all_deliveries = EmailDelivery.objects.select_related('message', 'subscriber').all()[:100]
    return render(request, 'core/admin_subscribers.html', {
        'sub_list': sub_list,
        'total_active': total_active,
        'total_inactive': total_inactive,
        'sent_messages': sent_messages,
        'deliveries': all_deliveries,
    })


@login_required
def ajax_send_email(request):
    """Send email to selected subscribers via BCC."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'message': 'Invalid method'}, status=405)
    if not request.user.is_staff:
        return JsonResponse({'ok': False, 'message': 'Staff only'}, status=403)
    try:
        data = json.loads(request.body)
        subject = (data.get('subject') or '').strip()
        body = (data.get('body') or '').strip()
        selected_ids = data.get('subscriber_ids', [])
        send_to_all = data.get('send_to_all', False)

        if not subject or not body:
            return JsonResponse({'ok': False, 'message': 'Subject and body are required'})

        if send_to_all:
            sub_qs = Subscriber.objects.filter(is_active=True)
        elif selected_ids:
            sub_qs = Subscriber.objects.filter(id__in=selected_ids, is_active=True)
        else:
            return JsonResponse({'ok': False, 'message': 'No recipients selected'})

        sub_list = list(sub_qs)
        if not sub_list:
            return JsonResponse({'ok': False, 'message': 'No active subscribers found'})

        from_email = getattr(django_settings, 'DEFAULT_FROM_EMAIL', 'noreply@newmovies.com')

        # Get SMTP backend from the admin-configured email addresses
        from .models import EmailAddress as EmailAddrModel
        smtp_addr = EmailAddrModel.objects.filter(
            is_active=True, purpose='notification'
        ).order_by('-is_default').first()
        if not smtp_addr:
            smtp_addr = EmailAddrModel.objects.filter(is_active=True).order_by('-is_default').first()

        if smtp_addr:
            email_backend = smtp_addr.get_backend()
            from_email = smtp_addr.email
        else:
            # Fallback to Django settings (console backend)
            email_backend = None

        # Create EmailMessage record first
        email_msg = EmailMessage.objects.create(
            subject=subject,
            body=body,
            sent_by=request.user,
            recipient_count=len(sub_list),
        )

        # Send via BCC — each subscriber gets their own email, can't see others
        from django.core.mail import EmailMessage as DjangoEmailMsg
        sent_count = 0
        for sub in sub_list:
            try:
                msg = DjangoEmailMsg(subject=subject, body=body, from_email=from_email, to=[sub.email])
                if email_backend:
                    msg.connection = email_backend
                    msg.send()
                else:
                    msg.send()
                EmailDelivery.objects.create(message=email_msg, subscriber=sub, status='sent')
                sent_count += 1
            except Exception as mail_err:
                EmailDelivery.objects.create(message=email_msg, subscriber=sub, status='failed')
                logger.warning('Failed to send email to %s: %s', sub.email, mail_err)

        email_msg.recipient_count = sent_count
        email_msg.save(update_fields=['recipient_count'])

        return JsonResponse({
            'ok': True,
            'message': f'Email sent to {sent_count}/{len(sub_list)} subscribers',
            'sent_count': sent_count,
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'message': f'Error sending email: {str(e)}'})



# ===== Ad verification file management =====
import os

AD_FILES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ad-files')

def _ensure_ad_files_dir():
    os.makedirs(AD_FILES_DIR, exist_ok=True)

def _read_ad_file(filename):
    path = os.path.join(AD_FILES_DIR, filename)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    # Fallback to root
    root_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), filename)
    if os.path.exists(root_path):
        with open(root_path, 'r', encoding='utf-8') as f:
            return f.read()
    return ''

def _write_ad_file(filename, content):
    _ensure_ad_files_dir()
    path = os.path.join(AD_FILES_DIR, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)

def _delete_ad_file(filename):
    path = os.path.join(AD_FILES_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
    # Also remove from root if exists
    root_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), filename)
    if os.path.exists(root_path):
        os.remove(root_path)

def _list_ad_files():
    _ensure_ad_files_dir()
    files = []
    for f in os.listdir(AD_FILES_DIR):
        if not f.startswith('.'):
            path = os.path.join(AD_FILES_DIR, f)
            files.append({
                'name': f,
                'size': os.path.getsize(path),
                'modified': os.path.getmtime(path),
            })
    # Also check root for sw.js, ads.txt etc
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for f in ['sw.js', 'ads.txt', 'app-ads.txt']:
        root_path = os.path.join(root_dir, f)
        if os.path.exists(root_path) and not any(x['name'] == f for x in files):
            files.append({
                'name': f,
                'size': os.path.getsize(root_path),
                'modified': os.path.getmtime(root_path),
                'in_root': True,
            })
    files.sort(key=lambda x: x['name'])
    return files

def serve_sw_js(request):
    content = _read_ad_file('sw.js')
    if not content:
        # Serve the default sw.js from root
        root_sw = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sw.js')
        if os.path.exists(root_sw):
            with open(root_sw, 'r', encoding='utf-8') as f:
                content = f.read()
    from django.http import HttpResponse
    return HttpResponse(content, content_type='application/javascript')


def serve_sw_proxy_js(request):
    """Serve the browser proxy Service Worker from the root path.
    This ensures the SW scope is '/' so it can intercept /proxy/ requests
    from any page on the site (not just /static/ pages)."""
    from django.http import HttpResponse
    sw_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'sw-proxy.js')
    try:
        with open(sw_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        content = '// sw-proxy.js not found'
    resp = HttpResponse(content, content_type='application/javascript')
    resp['Service-Worker-Allowed'] = '/'
    return resp

def serve_ads_txt(request):
    content = _read_ad_file('ads.txt')
    from django.http import HttpResponse
    return HttpResponse(content, content_type='text/plain')

def serve_app_ads_txt(request):
    content = _read_ad_file('app-ads.txt')
    from django.http import HttpResponse
    return HttpResponse(content, content_type='text/plain')


SEO_SITE_URL = 'https://newmovies.linkpc.net'


def serve_robots_txt(request):
    """Serve robots.txt at the site root for Google indexing.

    Googlebot checks /robots.txt first. Allow all paths for a public site so
    Google can crawl and index every movie, series, and public content page.
    """
    from django.http import HttpResponse
    from .header_block import robots_disallow_lines
    lines = [
        'User-agent: *',
        'Allow: /',
        '',
    ]
    # The rules the admin chose to publish: an address block is invisible to a
    # crawler that reads robots.txt, but a header rule can be published, and the
    # ones that ignore the file are then read by the same rule and refused.
    published = robots_disallow_lines()
    if published:
        lines += [
            '# Crawlers the site blocks (Blocked headers in the dashboard)',
        ] + published
    lines += [
        'Sitemap: ' + SEO_SITE_URL + '/sitemap.xml',
        '',
    ]
    return HttpResponse('\n'.join(lines), content_type='text/plain')


def serve_sitemap_xml(request):
    """Serve sitemap.xml at the site root for Google indexing.

    Lists the public static pages plus movie/series detail pages (by TMDB id)
    so Search Console can discover and index the whole catalog. Results are
    cached briefly to avoid re-querying the catalog on every crawl.
    """
    from django.http import HttpResponse
    from django.core.cache import cache

    cached = cache.get('seo_sitemap_xml')
    if cached:
        return HttpResponse(cached, content_type='application/xml')

    try:
        from django.conf import settings
        site = getattr(settings, 'SITE_URL', SEO_SITE_URL) or SEO_SITE_URL
    except Exception:
        site = SEO_SITE_URL
    site = site.rstrip('/')

    urls = [
        site + '/',
        site + '/movies/',
        site + '/series/',
        site + '/search/',
        site + '/calendar/',
        site + '/about/',
        site + '/contact/',
        site + '/privacy/',
        site + '/terms/',
    ]

    seen = set(urls)

    def _add(url):
        if url not in seen:
            seen.add(url)
            urls.append(url)

    # Titles users have actually played. The site uses slug URLs
    # (url_format='slug') and slugs are just slugify(title), so these resolve
    # straight to a 200 instead of a redirect from the /id/ route.
    try:
        from django.utils.text import slugify
        from .models import PlayHistory
        for media_type, title in (PlayHistory.objects
                                  .exclude(title__isnull=True)
                                  .exclude(title='')
                                  .values_list('media_type', 'title')
                                  .distinct()[:10000]):
            slug = slugify(title or '')
            if not slug:
                continue
            prefix = '/series/' if media_type == 'tv' else '/movies/'
            _add(site + prefix + slug + '/')
    except Exception:
        pass

    # Public detail pages the site has really served. Only accept the exact
    # single-segment slug shape so redirecting /id/ paths and logged 404s are
    # never advertised to crawlers.
    try:
        import re
        from django.db.models import Q
        from .models import WebsiteVisitorVisit
        slug_path = re.compile(r'^/(?:movies|series)/[a-z0-9][a-z0-9-]*/$')
        for path in (WebsiteVisitorVisit.objects
                     .filter(Q(path__startswith='/movies/') | Q(path__startswith='/series/'),
                             is_bot=False)
                     .order_by('-id')
                     .values_list('path', flat=True)[:20000]):
            path = (path or '').split('?', 1)[0]
            if slug_path.match(path):
                _add(site + path)
    except Exception:
        pass

    def _esc(value):
        return (value.replace('&', '&amp;')
                     .replace('<', '&lt;')
                     .replace('>', '&gt;')
                     .replace('"', '&quot;'))

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in urls:
        parts.append('  <url><loc>' + _esc(url) + '</loc></url>')
    parts.append('</urlset>')
    xml = '\n'.join(parts)
    try:
        cache.set('seo_sitemap_xml', xml, 3600)
    except Exception:
        pass
    return HttpResponse(xml, content_type='application/xml')


def serve_google_verification(request):
    """Serve the Google Search Console site-verification file at the site root.

    Google looks for ``/google3d3495bfc5b3fba2.html`` and checks that the
    body contains ``google-site-verification: google3d3495bfc5b3fba2.html``.
    The file is read from the project root (where it lives alongside
    manage.py / sw.js) via the same ``_read_ad_file`` helper used for
    ads.txt, or the AD_FILES_DIR override when one is configured.
    """
    content = _read_ad_file('google3d3495bfc5b3fba2.html')
    from django.http import HttpResponse
    return HttpResponse(content, content_type='text/html')

@login_required
@user_passes_test(is_staff_or_superuser)
def admin_ad_files(request):
    if not request.user.is_staff:
        return redirect('/admin/')
    files = _list_ad_files()
    return render(request, 'core/admin_ad_files.html', {
        'files': files,
    })

def ajax_save_ad_file(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False}, status=405)
    if not request.user.is_staff:
        return JsonResponse({'ok': False, 'message': 'Staff only'}, status=403)
    try:
        data = json.loads(request.body)
        filename = data.get('filename', '').strip()
        content = data.get('content', '')
        if not filename:
            return JsonResponse({'ok': False, 'message': 'Filename required'})
        # Sanitize filename
        filename = os.path.basename(filename)
        if '/' in filename or '' in filename:
            return JsonResponse({'ok': False, 'message': 'Invalid filename'})
        _write_ad_file(filename, content)
        return JsonResponse({'ok': True, 'message': f'{filename} saved'})
    except Exception as e:
        return JsonResponse({'ok': False, 'message': str(e)})

def ajax_delete_ad_file(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False}, status=405)
    if not request.user.is_staff:
        return JsonResponse({'ok': False, 'message': 'Staff only'}, status=403)
    try:
        data = json.loads(request.body)
        filename = data.get('filename', '').strip()
        if not filename:
            return JsonResponse({'ok': False, 'message': 'Filename required'})
        filename = os.path.basename(filename)
        _delete_ad_file(filename)
        return JsonResponse({'ok': True, 'message': f'{filename} deleted'})
    except Exception as e:
        return JsonResponse({'ok': False, 'message': str(e)})


# ---------------------------------------------------------------------------
# Android User API (Register, Login, Sync) + Admin
# ---------------------------------------------------------------------------
from .models import SyncedUser, UserCloudData, UserHeartbeat
from .auth import send_configured_email
from .auth import (
    create_user_token_pair, decode_jwt_token, reset_rate_limit,
    check_rate_limit, record_rate_limit_attempt, rate_limit_response,
    RATE_LIMIT_IP_MAX_ATTEMPTS,
)
import hashlib


# Default free-tier subscription payload
FREE_SUBSCRIPTION = {
    'isSubscribed': False,
    'plan': 'Standard Free',
    'status': 'active',
    'validUntil': 'Unlimited',
    'daysRemaining': 0,
    'features': [
        'Full HD Streaming',
        'Standard Live TV Channels',
        'Subtitles & Audio Tracks',
        'Watchlist & Cloud Progress Sync',
    ],
}



def _record_session(user, source, request, device_model='', os_version='', app_version=''):
    """Record a login session for tracking logins, active sessions, and source."""
    from .models import UserSession
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', '')
    ua = request.META.get('HTTP_USER_AGENT', '')
    UserSession.objects.create(
        user=user,
        source=source,
        ip_address=ip or None,
        user_agent=ua[:500],
        device_model=device_model,
        os_version=os_version,
        app_version=app_version,
    )


def _lift_block_after_auth(request):
    """Lift a normal IP block once a blocked visitor signs in or registers.

    The block page a blocked address receives shows only the login and
    registration forms and promises that either one removes the block; this is
    where that promise is kept. A permanent block is never lifted here, and a
    failure is swallowed so it can never break a login or a registration.
    """
    try:
        from .ip_block import note_auth_success
        note_auth_success(request)
    except Exception:
        logger.warning('IPBlock: could not lift the block after auth', exc_info=True)


def _require_post(view_func):
    """POST-only guard that answers in JSON.

    Django's own ``require_http_methods`` returns an HTML body, and the app
    shows an error's ``message`` verbatim — an HTML page would surface as a
    blank or unparseable failure. Every refused method therefore gets the same
    JSON shape as any other error.
    """
    @wraps(view_func)
    def _wrapper(request, *args, **kwargs):
        if request.method != 'POST':
            return JsonResponse({
                'status': 'error',
                'message': 'Method not allowed. Use POST.',
            }, status=405)
        return view_func(request, *args, **kwargs)
    return _wrapper


def _https_only(view_func):
    """Refuse plain-HTTP calls in production, in JSON.

    The app hardcodes ``https://`` for these endpoints, so an inbound plain-HTTP
    request is either a misconfiguration or an attacker downgrading the
    connection in the hope of reading the token or the deletion code. Locally
    (ENV != 'prod') the guard stays out of the way so the test client works.
    """
    @wraps(view_func)
    def _wrapper(request, *args, **kwargs):
        if getattr(settings, 'ENV', 'dev') == 'prod' and not request.is_secure():
            return JsonResponse({
                'status': 'error',
                'message': 'This endpoint requires a secure (HTTPS) connection.',
            }, status=403)
        return view_func(request, *args, **kwargs)
    return _wrapper


def _validate_android_auth(request):
    """Validate Basic Auth + Android headers. Returns (app_match, error_response)."""
    from .models import AndroidApp
    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth_header.startswith('Basic '):
        return None, JsonResponse({'status': 'error', 'message': 'Missing Authorization header'}, status=401)
    try:
        decoded = base64.b64decode(auth_header.split(' ', 1)[1]).decode('utf-8')
        username, password = decoded.split(':', 1)
    except Exception:
        return None, JsonResponse({'status': 'error', 'message': 'Invalid Authorization header'}, status=401)
    app_match = AndroidApp.objects.filter(
        access_username=username, access_password=password, is_active=True,
    ).first()
    if not app_match:
        return None, JsonResponse({'status': 'error', 'message': 'Invalid credentials'}, status=403)
    return app_match, None


def _user_payload(user_obj, django_user):
    """Build the user object for API responses with JWT tokens."""
    photo = django_user.email and f"https://ui-avatars.com/api/?name={django_user.first_name or django_user.username}&background=4F46E5&color=fff&size=128" or ''
    tokens = create_user_token_pair(django_user)
    return {
        'userId': f'usr_{django_user.pk}',
        'email': django_user.email,
        'displayName': django_user.first_name or django_user.username,
        'photoUrl': user_obj.photo_url if user_obj and user_obj.photo_url else photo,
        'token': tokens['token'],
        'refreshToken': tokens['refreshToken'],
        'tokenExpiresIn': tokens['expiresIn'],
        'isEmailVerified': django_user.is_active,
    }


def _build_user_response(django_user, user_obj, extra=None):
    """Build the full JSON response with user + subscription + userData."""
    resp = {
        'status': 'success',
        'user': _user_payload(user_obj, django_user),
        'subscription': user_obj.subscription_payload() if user_obj else FREE_SUBSCRIPTION,
    }
    # Pull web data into cloud, then return merged payload
    cloud = getattr(django_user, 'cloud_data', None)
    if cloud:
        cloud.sync_from_web_models()
        cloud.refresh_from_db()
        resp['userData'] = cloud.get_cloud_payload()
    if extra:
        resp.update(extra)
    return resp


def _ensure_user_and_profile(email, body):
    """Create or get Django User + SyncedUser. Returns (django_user, user_obj, created)."""
    display_name = body.get('displayName', '') or email.split('@')[0]
    photo_url = body.get('photoUrl', '')
    google_id = body.get('googleId', '')
    device_id = body.get('deviceId', '')
    device_model = body.get('deviceModel', '')
    os_version = body.get('osVersion', '')
    app_version = body.get('appVersion', '')
    build_number = body.get('buildNumber', 0)

    # Check if Django User already exists
    django_user = User.objects.filter(email__iexact=email).first()
    user_obj = SyncedUser.objects.filter(email__iexact=email).first()
    created = False

    if django_user and user_obj:
        # Existing user — update profile
        user_obj.display_name = display_name or user_obj.display_name
        user_obj.photo_url = photo_url or user_obj.photo_url
        user_obj.google_id = google_id or user_obj.google_id
        user_obj.device_id = device_id or user_obj.device_id
        user_obj.device_model = device_model or user_obj.device_model
        user_obj.os_version = os_version or user_obj.os_version
        user_obj.app_version = app_version or user_obj.app_version
        user_obj.build_number = build_number or user_obj.build_number
        user_obj.save()
    elif django_user and not user_obj:
        # Django User exists (web registration) but no SyncedUser — create profile
        user_obj = SyncedUser.objects.create(
            user=django_user, email=email, display_name=display_name,
            photo_url=photo_url, google_id=google_id,
            device_id=device_id, device_model=device_model,
            os_version=os_version, app_version=app_version, build_number=build_number,
        )
    else:
        # Brand new user — create both
        base_username = email.split('@')[0]
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{counter}"
            counter += 1
        django_user = User.objects.create_user(
            username=username, email=email, password=None,
        )
        django_user.first_name = display_name
        django_user.is_active = True
        django_user.save()
        user_obj = SyncedUser.objects.create(
            user=django_user, email=email, display_name=display_name,
            photo_url=photo_url, google_id=google_id,
            device_id=device_id, device_model=device_model,
            os_version=os_version, app_version=app_version, build_number=build_number,
        )
        created = True

    # Ensure cloud data exists
    UserCloudData.objects.get_or_create(user=django_user)
    return django_user, user_obj, created


# Minimum seconds between FULL /api/user/sync cycles for the same user.
# The Android app polls this endpoint aggressively (5,000+ calls/day observed
# per device); a full sync does user upsert + cloud merge + web-model
# backfill, so syncs arriving closer than this get the stored cloud state
# served back cheaply instead.
_USER_SYNC_MIN_INTERVAL = 45
# Request caps for POST /api/user/sync. The interval above only decides whether
# a sync is *full* or answered from stored data; these cap how often a client
# may ask at all, because every answer carries the whole cloud payload.
_USER_SYNC_MAX_PER_WINDOW = 15       # per account, per window below
_USER_SYNC_IP_MAX_PER_WINDOW = 60    # per address (shared/office/carrier IPs)
_USER_SYNC_WINDOW_SECONDS = 60


def _guard_android_api_errors(view_func):
    """Return clean JSON + full server-side traceback when an Android user-API
    endpoint hits an unexpected exception, instead of an opaque HTML 500."""
    @wraps(view_func)
    def _wrapper(request, *args, **kwargs):
        try:
            return view_func(request, *args, **kwargs)
        except UnreadablePostError:
            # The client (usually the Android app on a flaky mobile network)
            # dropped the TCP connection while the POST body was being read.
            # That is a client-side disconnect, not a server fault: answer a
            # 400 so the app retries, and do not email/record it as a 500.
            logger.info('%s: client disconnected during body read — returning 400', view_func.__name__)
            return JsonResponse({
                'status': 'error',
                'message': 'Connection lost while uploading data. Please retry.',
                'retryable': True,
            }, status=400)
        except DatabaseError as e:
            # A broken/unreachable user database (a deploy that has not yet
            # migrated the external DB, a dropped connection) is a server-side
            # outage, not a client bug. Answer 503 + retryable so the app tells
            # the user the server is busy instead of "login failed", and record
            # it loudly for the admin error page.
            logger.exception('Database error in Android API endpoint %s', view_func.__name__)
            try:
                from .api_errors import record_api_error
                record_api_error(view_func.__name__, request, e)
            except Exception:
                pass
            return JsonResponse({
                'status': 'error',
                'message': 'Server is temporarily unavailable. Please try again in a moment.',
                'retryable': True,
            }, status=503)
        except Exception as e:
            logger.exception('Unhandled error in Android API endpoint %s', view_func.__name__)
            try:
                from .api_errors import record_api_error
                record_api_error(view_func.__name__, request, e)
            except Exception:
                pass
            return JsonResponse({
                'status': 'error',
                'message': 'Internal server error. Please try again.',
            }, status=500)
    return _wrapper


@login_required
@user_passes_test(is_staff_or_superuser)
def api_error_log(request):
    """Admin page: every captured error — Android API and ordinary pages."""
    from .api_errors import recent_errors, clear_errors, error_total, error_to_dict

    if request.method == 'POST':
        if request.POST.get('action') == 'clear':
            clear_errors()
        return redirect('api_error_log')

    kind = request.GET.get('kind') or ''
    status = request.GET.get('status') or ''
    q = (request.GET.get('q') or '').strip()
    if kind not in ('api', 'server'):
        kind = ''
    if status not in ('4xx', '5xx'):
        status = ''

    records = recent_errors(limit=300, kind=kind, status=status, q=q)
    if request.GET.get('format') == 'json':
        return JsonResponse({
            'status': 'success',
            'count': len(records),
            'filters': {'kind': kind, 'status': status, 'q': q},
            'errors': [error_to_dict(r) for r in records],
        })

    # Opening this page clears its sidebar badge for this browser.
    try:
        request.session['admin_api_errors_seen'] = timezone.now().isoformat()
    except Exception:
        pass

    return render(request, 'core/admin_api_errors.html', {
        'records': records,
        'count': len(records),
        'total': error_total(),
        'kind': kind,
        'status': status,
        'q': q,
        'back_url': 'admin_dashboard',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_requests(request):
    """Requests & Users — one admin page holding the traffic and user views.

    ``?tab=log`` (the default) is the raw request log, ``?tab=users`` is the
    aggregated active / inactive visitor list, ``?tab=journeys`` is each
    visitor's own page-by-page history, and ``?tab=details`` is the per-account
    profile (devices, watch counts, logins, subscription). Only the tab being
    shown is computed, so the page stays as fast as either list was alone.
    """
    tab = (request.GET.get('tab') or '').strip().lower()
    if tab == 'users':
        return admin_active_users(request)
    if tab == 'journeys':
        return admin_visitor_journeys(request)
    if tab == 'details':
        return admin_user_details(request)
    return _request_log_tab(request)


def _admin_apply_subscription_edit(request):
    """Admin-only POST: set one account's subscription window and plan.

    Writes the ``SyncedUser`` record the Android app already reads through
    ``subscription_payload()``, so an extension made here shows up in the app on
    its next sync. Only ``admin_user_details`` calls this and that view is
    already staff-gated, so the caller enforces who may edit.

    Always redirects back to the filtered tab, with a toast either way.
    """
    from datetime import datetime
    from .models import SyncedUser

    def _back():
        # Only ever bounce back to a path on this site (never an open redirect).
        nxt = request.POST.get('next') or ''
        if nxt.startswith('/') and not nxt.startswith('//'):
            return redirect(nxt)
        return redirect('request_log')

    try:
        uid = int((request.POST.get('user_id') or '').strip())
    except (TypeError, ValueError):
        messages.error(request, 'Unknown account.')
        return _back()

    account = User.objects.filter(id=uid).first()
    if account is None:
        messages.error(request, 'That account no longer exists.')
        return _back()

    is_subscribed = (request.POST.get('is_subscribed') or '').lower() in (
        '1', 'on', 'true', 'yes')
    plan = (request.POST.get('plan') or '').strip()[:100]

    valid_until = None
    raw_until = (request.POST.get('valid_until') or '').strip()
    if raw_until:
        try:
            valid_until = datetime.strptime(raw_until, '%Y-%m-%d').date()
        except ValueError:
            messages.error(request, 'Subscribed-till must be a valid date (YYYY-MM-DD).')
            return _back()

    features = [f.strip()[:50]
                for f in (request.POST.get('features') or '').split(',')]
    features = [f for f in features if f][:20]

    # Prefer the record already linked to this account; otherwise adopt the one
    # synced under the same email so we never trip the unique email constraint.
    profile = SyncedUser.objects.filter(user_id=uid).first()
    if profile is None and account.email:
        profile = SyncedUser.objects.filter(email__iexact=account.email).first()
        if profile is not None and not profile.user_id:
            profile.user = account
    if profile is None:
        email = (account.email or '').strip()
        if not email:
            messages.error(request, 'Add an email to this account before setting a subscription.')
            return _back()
        profile = SyncedUser(user_id=uid, email=email)

    was_subscribed, was_plan, was_valid_until = (
        profile.is_subscribed, profile.plan, profile.valid_until)
    profile.is_subscribed = is_subscribed
    profile.plan = plan if is_subscribed else ''
    profile.valid_until = valid_until if is_subscribed else None
    profile.features = features if is_subscribed else []
    if not profile.display_name:
        profile.display_name = (account.get_full_name() or account.username or '')[:255]
    try:
        profile.save()
    except Exception as exc:
        logger.warning('subscription edit failed for user %s: %s', uid, exc)
        messages.error(request, 'Could not save the subscription: %s' % exc)
        return _back()

    who = request.user.get_username()
    _log_subscription_event(profile, who, 'account-page',
                            {'is_subscribed': was_subscribed, 'plan': was_plan,
                             'valid_until': was_valid_until})
    if is_subscribed:
        until = valid_until.isoformat() if valid_until else 'no end date'
        logger.info('admin %s set subscription for user %s: plan=%r until=%s',
                    who, uid, plan, until)
        messages.success(
            request,
            'Subscription for %s saved — %s%s.' % (
                account.username, plan or 'no plan',
                (' till ' + valid_until.isoformat()) if valid_until else ''))
    else:
        logger.info('admin %s cleared subscription for user %s', who, uid)
        messages.success(request, 'Subscription for %s cleared.' % account.username)
    return _back()


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_details(request):
    """Per-account profile tab: devices, watch counts, logins and subscription.

    One row per registered account, joining the identity (``auth.User`` and its
    ``SyncedUser`` profile) with everything we know about how it is used: every
    IP and device it logged in from, how many movies / series / episodes it
    watched (from ``PlayHistory``), first and last login (``UserSession``),
    time on site (``UserPageView``), and the subscription window
    (``SyncedUser.valid_until``). All aggregations key on ``user_id`` and are
    folded together in Python because the user models live on the external DB
    while ``auth.User`` stays on the default one, so a cross-database SQL join
    is not available.
    """
    if request.method == 'POST':
        return _admin_apply_subscription_edit(request)

    import csv
    from datetime import timedelta
    from django.db.models import Min, Max
    from .models import (PlayHistory, UserSession, UserPageView, WatchList,
                         SyncedUser, WebsiteVisitor, AndroidAppDevice,
                         AndroidAppDeviceVisit)

    def clean(name, maxlen=200):
        return (request.GET.get(name) or '').strip()[:maxlen]

    def _fmt(value):
        if not value:
            return ''
        try:
            return timezone.localtime(value).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return str(value)

    search = clean('q')
    sub_filter = clean('sub', 12).lower()
    if sub_filter not in ('', 'subscribed', 'expiring', 'expired', 'free'):
        sub_filter = ''
    platform_filter = clean('platform', 10).lower()
    if platform_filter not in ('', 'web', 'android'):
        platform_filter = ''
    sort = clean('sort', 20) or 'last_seen'
    direction = clean('dir', 4).lower()
    if direction not in ('asc', 'desc'):
        direction = 'desc'
    sortable = {'user', 'joined', 'first_login', 'last_login', 'last_seen',
                'movies', 'series', 'episodes', 'watch_time', 'views',
                'devices', 'sub_until'}
    if sort not in sortable:
        sort = 'last_seen'

    today = timezone.localdate()
    expiring_cutoff = today + timedelta(days=7)

    # --- identity: every Django account, plus its SyncedUser profile --------
    try:
        accounts = list(User.objects.all().only(
            'id', 'username', 'email', 'first_name', 'last_name',
            'date_joined', 'last_login', 'is_active', 'is_staff', 'is_superuser'))
    except Exception as exc:
        logger.warning('user details: account query failed: %s', exc)
        accounts = []

    profiles = {}
    try:
        for sp in SyncedUser.objects.all():
            if sp.user_id:
                profiles[sp.user_id] = sp
    except Exception as exc:
        logger.warning('user details: synced profile query failed: %s', exc)

    # --- watch activity (movies / series / episodes), keyed by user_id ----
    watch = {}
    last_watch_title = {}
    try:
        watch_agg = (PlayHistory.objects.values('user_id').annotate(
            movies=Count('id', filter=Q(media_type='movie'), distinct=True),
            movie_rows=Count('id', filter=Q(media_type='movie')),
            shows=Count('tmdb_id', filter=Q(media_type='tv'), distinct=True),
            episodes=Count('id', filter=Q(media_type='tv')),
            completed=Count('id', filter=Q(completed=True)),
            watch_seconds=Sum('duration_seconds'),
            last_played=Max('last_played_at'),
        ))
        for row in watch_agg:
            watch[row['user_id']] = row
    except Exception as exc:
        logger.warning('user details: watch aggregate failed: %s', exc)
    try:
        for ph in (PlayHistory.objects.order_by('user_id', '-last_played_at')
                   .values('user_id', 'title', 'media_type', 'season_number',
                           'episode_number', 'last_played_at')):
            if ph['user_id'] not in last_watch_title:
                last_watch_title[ph['user_id']] = ph
    except Exception as exc:
        logger.warning('user details: last watch query failed: %s', exc)

    # --- login sessions: counts + first/last login -------------------------
    sess = {}
    try:
        sess_agg = (UserSession.objects.values('user_id').annotate(
            logins=Count('id'),
            first_login=Min('logged_in_at'),
            last_login=Max('logged_in_at'),
            last_seen=Max('last_seen_at'),
            open_sessions=Count('id', filter=Q(is_active=True)),
        ))
        for row in sess_agg:
            sess[row['user_id']] = row
    except Exception as exc:
        logger.warning('user details: session aggregate failed: %s', exc)

    # --- IPs + device signatures, from sessions and page views -------------
    ips = {}
    device_sigs = {}
    platforms = {}
    try:
        for s in (UserSession.objects.values('user_id', 'source', 'ip_address',
                                             'device_model', 'os_version',
                                             'app_version').distinct()[:50000]):
            uid = s['user_id']
            if not uid:
                continue
            if s['ip_address']:
                ips.setdefault(uid, set()).add(s['ip_address'])
            if s['source']:
                platforms.setdefault(uid, set()).add(s['source'])
            sig = ' · '.join(x for x in (s['device_model'],
                                         ('OS ' + s['os_version']) if s['os_version'] else '',
                                         ('v' + s['app_version']) if s['app_version'] else '') if x)
            if sig:
                device_sigs.setdefault(uid, set()).add(sig)
    except Exception as exc:
        logger.warning('user details: session detail query failed: %s', exc)

    pv_stats = {}
    try:
        for pv in (UserPageView.objects.exclude(user_id=None)
                   .values('user_id').annotate(
                       views=Count('id'),
                       total_time=Sum('time_spent_seconds'),
                       last_view=Max('viewed_at'))):
            pv_stats[pv['user_id']] = pv
    except Exception as exc:
        logger.warning('user details: page view aggregate failed: %s', exc)
    try:
        for pv in (UserPageView.objects.exclude(ip_address=None)
                   .values('user_id', 'ip_address').distinct()[:50000]):
            uid = pv['user_id']
            if uid and pv['ip_address']:
                ips.setdefault(uid, set()).add(pv['ip_address'])
    except Exception as exc:
        logger.warning('user details: page view IP query failed: %s', exc)

    # --- website visitor rows (anonymous sessions tied to an account) ------
    visitors = {}
    try:
        for v in (WebsiteVisitor.objects.exclude(user_id=None)
                  .values('user_id', 'visitor_id', 'first_seen_at',
                          'last_seen_at', 'total_visits', 'last_path',
                          'last_ip_address', 'user_agent')):
            uid = v['user_id']
            prev = visitors.get(uid)
            if prev is None or (v['last_seen_at'] and
                                (prev['last_seen_at'] is None or v['last_seen_at'] > prev['last_seen_at'])):
                visitors[uid] = v
            if uid and v['last_ip_address']:
                ips.setdefault(uid, set()).add(v['last_ip_address'])
    except Exception as exc:
        logger.warning('user details: website visitor query failed: %s', exc)

    # --- Android devices, resolved through the SyncedUser device_id --------
    android_devices = {}
    try:
        device_ids = {sp.device_id for sp in profiles.values() if sp.device_id}
        if device_ids:
            latest_visit = {}
            for dv in (AndroidAppDeviceVisit.objects.filter(device_id__in=list(device_ids))
                       .order_by('-visited_at')
                       .values('device_id', 'ip_address', 'device_model',
                               'os_version', 'visited_at')[:20000]):
                latest_visit.setdefault(dv['device_id'], dv)
            for d in (AndroidAppDevice.objects.filter(user_id__in=list(device_ids))
                      .only('user_id', 'device_model', 'os_version',
                            'first_seen_at', 'last_seen_at', 'total_visits')):
                visit = latest_visit.get(d.user_id)
                android_devices.setdefault(d.user_id, []).append({
                    'device_id': d.user_id,
                    'device_model': d.device_model,
                    'os_version': d.os_version,
                    'first_seen': d.first_seen_at,
                    'last_seen': d.last_seen_at,
                    'visits': d.total_visits,
                    'ip': (visit or {}).get('ip_address') or '',
                })
    except Exception as exc:
        logger.warning('user details: android device query failed: %s', exc)

    # --- watchlist / cloud favourites -------------------------------------
    watchlist_counts = {}
    try:
        for row in WatchList.objects.values('user_id').annotate(n=Count('id')):
            watchlist_counts[row['user_id']] = row['n']
    except Exception as exc:
        logger.warning('user details: watchlist aggregate failed: %s', exc)

    # --- fold everything into one row per account --------------------------
    rows = []
    seen_uids = set()
    for account in accounts:
        uid = account.id
        seen_uids.add(uid)
        profile = profiles.get(uid)
        w = watch.get(uid) or {}
        s = sess.get(uid) or {}
        pv = pv_stats.get(uid) or {}
        v = visitors.get(uid)
        user_ips = sorted(ips.get(uid) or [])
        dev_sigs = sorted(device_sigs.get(uid) or [])
        plat = platforms.get(uid) or set()

        is_subscribed = bool(profile.is_subscribed) if profile else False
        valid_until = profile.valid_until if profile else None
        days_left = None
        if valid_until:
            days_left = (valid_until - today).days
        if not is_subscribed:
            sub_state = 'free'
        elif valid_until and valid_until < today:
            sub_state = 'expired'
        elif valid_until and valid_until <= expiring_cutoff:
            sub_state = 'expiring'
        else:
            sub_state = 'subscribed'

        last_watch = last_watch_title.get(uid)
        last_watch_label = ''
        if last_watch:
            last_watch_label = last_watch['title'] or ''
            if last_watch['media_type'] == 'tv' and last_watch['season_number'] and last_watch['episode_number']:
                last_watch_label += f" S{last_watch['season_number']}E{last_watch['episode_number']}"

        last_seen = None
        for candidate in (s.get('last_seen'), pv.get('last_view'),
                          (v or {}).get('last_seen_at'), account.last_login):
            if candidate and (last_seen is None or candidate > last_seen):
                last_seen = candidate

        devices = len(dev_sigs) + len(android_devices.get(uid, []))
        rows.append({
            'user_id': uid,
            'username': account.username,
            'email': account.email or (profile.email if profile else ''),
            'full_name': (account.get_full_name() or '').strip(),
            'display_name': ((profile.display_name if profile else '') or account.get_full_name() or account.username),
            'is_staff': account.is_staff,
            'is_superuser': account.is_superuser,
            'is_active': account.is_active,
            'date_joined': account.date_joined,
            'photo_url': (profile.photo_url if profile else '') or '',
            'google_id': (profile.google_id if profile else '') or '',
            'app_version': (profile.app_version if profile else '') or '',
            # subscription
            'is_subscribed': is_subscribed,
            'plan': (profile.plan if profile else '') or '',
            'valid_until': valid_until,
            'days_left': days_left,
            'sub_state': sub_state,
            'features': (profile.features if profile else []) or [],
            'last_synced_at': (profile.last_synced_at if profile else None),
            # watch activity
            'movies': w.get('movies') or 0,
            'shows': w.get('shows') or 0,
            'episodes': w.get('episodes') or 0,
            'completed': w.get('completed') or 0,
            'watch_seconds': w.get('watch_seconds') or 0,
            'last_played': w.get('last_played'),
            'last_watch_label': last_watch_label,
            'watchlist': watchlist_counts.get(uid, 0),
            # sessions
            'logins': s.get('logins') or 0,
            'open_sessions': s.get('open_sessions') or 0,
            'first_login': s.get('first_login') or account.last_login,
            'last_login': s.get('last_login') or account.last_login,
            'last_seen': last_seen,
            # engagement
            'views': pv.get('views') or 0,
            'total_time': pv.get('total_time') or 0,
            # devices / network
            'ips': user_ips,
            'ip_count': len(user_ips),
            'ip': user_ips[-1] if user_ips else ((v or {}).get('last_ip_address') or ''),
            'device_sigs': dev_sigs,
            'device_count': devices,
            'platforms': sorted(plat),
            'android_devices': android_devices.get(uid, []),
            'visits': (v or {}).get('total_visits') or 0,
            'last_path': (v or {}).get('last_path') or '',
        })

    # Accounts that only exist as a SyncedUser (no local Django account yet).
    for uid, profile in profiles.items():
        if uid in seen_uids:
            continue
        rows.append({
            'user_id': uid, 'username': '', 'email': profile.email,
            'full_name': '', 'display_name': profile.display_name or profile.email,
            'is_staff': False, 'is_superuser': False, 'is_active': False,
            'date_joined': profile.created_at, 'photo_url': profile.photo_url or '',
            'google_id': profile.google_id or '', 'app_version': profile.app_version or '',
            'is_subscribed': bool(profile.is_subscribed), 'plan': profile.plan or '',
            'valid_until': profile.valid_until, 'days_left': None,
            'sub_state': 'subscribed' if profile.is_subscribed else 'free',
            'features': profile.features or [], 'last_synced_at': profile.last_synced_at,
            'movies': 0, 'shows': 0, 'episodes': 0, 'completed': 0,
            'watch_seconds': 0, 'last_played': None, 'last_watch_label': '',
            'watchlist': 0, 'logins': 0, 'open_sessions': 0,
            'first_login': None, 'last_login': None, 'last_seen': profile.last_synced_at,
            'views': 0, 'total_time': 0, 'ips': [], 'ip_count': 0, 'ip': '',
            'device_sigs': [], 'device_count': 0, 'platforms': ['android'],
            'android_devices': [], 'visits': 0, 'last_path': '',
        })

    # --- summary cards (computed over all rows, before filtering) ----------
    summary = {
        'total_users': len(rows),
        'subscribed': sum(1 for r in rows if r['sub_state'] in ('subscribed', 'expiring')),
        'expiring': sum(1 for r in rows if r['sub_state'] == 'expiring'),
        'expired': sum(1 for r in rows if r['sub_state'] == 'expired'),
        'free': sum(1 for r in rows if r['sub_state'] == 'free'),
        'staff': sum(1 for r in rows if r['is_staff']),
        'movies': sum(r['movies'] for r in rows),
        'shows': sum(r['shows'] for r in rows),
        'episodes': sum(r['episodes'] for r in rows),
        'watch_seconds': sum(r['watch_seconds'] for r in rows),
        'with_devices': sum(1 for r in rows if r['device_count'] or r['ip_count']),
    }

    # --- filters -----------------------------------------------------------
    needle = search.lower()

    def _matches(r):
        if not needle:
            return True
        haystack = ' '.join(str(x or '') for x in (
            r['username'], r['email'], r['display_name'], r['full_name'],
            r['ip'], ' '.join(r['ips']), ' '.join(r['device_sigs']),
            r['plan'], r['last_path'], ' '.join(r['platforms']))).lower()
        return needle in haystack

    def _in_scope(r):
        if sub_filter:
            if sub_filter == 'subscribed' and r['sub_state'] not in ('subscribed', 'expiring'):
                return False
            if sub_filter == 'expiring' and r['sub_state'] != 'expiring':
                return False
            if sub_filter == 'expired' and r['sub_state'] != 'expired':
                return False
            if sub_filter == 'free' and r['sub_state'] != 'free':
                return False
        if platform_filter == 'web' and 'web' not in r['platforms']:
            return False
        if platform_filter == 'android' and 'android' not in r['platforms']:
            return False
        return _matches(r)

    filtered = [r for r in rows if _in_scope(r)]

    epoch = timezone.now() - timedelta(days=3650)
    sort_keys = {
        'user': lambda r: (r['display_name'] or '').lower(),
        'joined': lambda r: r['date_joined'] or epoch,
        'first_login': lambda r: r['first_login'] or epoch,
        'last_login': lambda r: r['last_login'] or epoch,
        'last_seen': lambda r: r['last_seen'] or epoch,
        'movies': lambda r: r['movies'],
        'series': lambda r: r['shows'],
        'episodes': lambda r: r['episodes'],
        'watch_time': lambda r: r['watch_seconds'],
        'views': lambda r: r['views'],
        'devices': lambda r: r['device_count'],
        'sub_until': lambda r: r['valid_until'] or epoch.date(),
    }
    filtered.sort(key=sort_keys.get(sort, sort_keys['last_seen']),
                  reverse=(direction != 'asc'))
    max_display = 500
    displayed = filtered[:max_display]

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="user_details.csv"'

        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow(['User', 'Email', 'Plan', 'Subscribed till', 'Days left',
                         'Movies watched', 'Series watched', 'Episodes watched',
                         'Watch time (min)', 'First login', 'Last login',
                         'Last seen', 'Logins', 'IPs', 'Devices', 'App version',
                         'Page views', 'Time on site (s)', 'Watchlist',
                         'Joined', 'Staff', 'Account active'])
        for r in filtered:
            writer.writerow([
                r['display_name'], r['email'], r['plan'],
                r['valid_until'].isoformat() if r['valid_until'] else '',
                r['days_left'] if r['days_left'] is not None else '',
                r['movies'], r['shows'], r['episodes'],
                round(r['watch_seconds'] / 60), _fmt(r['first_login']),
                _fmt(r['last_login']), _fmt(r['last_seen']), r['logins'],
                ', '.join(r['ips']), ' | '.join(r['device_sigs']),
                r['app_version'], r['views'], r['total_time'], r['watchlist'],
                _fmt(r['date_joined']), 'yes' if r['is_staff'] else 'no',
                'yes' if r['is_active'] else 'no',
            ])
        return response

    from .ip_block import active_blocked_ips
    blocked = active_blocked_ips()

    return render(request, 'core/admin_requests.html', {
        'panel': 'details',
        'rows': displayed,
        'blocked_ips': blocked,
        'blocked_total': len(blocked),
        'shown': len(displayed),
        'row_total': len(filtered),
        'max_display': max_display,
        'summary': summary,
        'search': search,
        'sub_filter': sub_filter,
        'platform_filter': platform_filter,
        'sort': sort,
        'dir': direction,
        'expiring_cutoff': expiring_cutoff,
        'today': today,
        'now': timezone.now(),
    })


def _request_log_tab(request):
    """Request Log tab: every incoming request — IP, page, method, status, timing.

    Renders as cards or as a spreadsheet-style table (``?view=table``) with a
    filter condition on every column, and exports JSON or CSV with the same
    filters applied.
    """
    import csv
    from django.http import HttpResponse
    from .request_log import (recent_requests, clear_requests, request_total,
                              request_to_dict, FIELD_FILTERS, SORTABLE_FIELDS,
                              clean_preset_query)
    from .models import RequestLogPreset

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'clear':
            clear_requests()
            return redirect('request_log')
        if action == 'save_preset':
            name = (request.POST.get('preset_name') or '').strip()[:120]
            query = clean_preset_query(request.POST.get('query') or '')
            if name:
                RequestLogPreset.objects.create(
                    name=name, query=query,
                    owner=(request.user.username or '')[:150],
                )
                messages.success(request, f'Saved view "{name}".')
            else:
                messages.error(request, 'Give the saved view a name.')
        elif action == 'delete_preset':
            RequestLogPreset.objects.filter(id=request.POST.get('preset_id')).delete()
        # Return to the exact view we came from (filters intact).
        return redirect(request.get_full_path() or 'request_log')

    def clean(name, maxlen=200):
        return (request.GET.get(name) or '').strip()[:maxlen]

    category = clean('category', 10)
    method = clean('method', 10).upper()
    status = clean('status', 10)
    if category not in ('page', 'api', 'admin'):
        category = ''
    if method not in ('GET', 'POST', 'HEAD', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
        method = ''
    if status not in ('2xx', '3xx', '4xx', '5xx'):
        status = ''

    filters = {
        'category': category,
        'method': method,
        'status': status,
        'status_code': clean('status_code', 6),
        'q': clean('q'),
        'min_duration': clean('min_duration', 12),
        'min_size': clean('min_size', 12),
        'min_time': clean('min_time', 12),
        'min_clicks': clean('min_clicks', 12),
        'on_date': clean('on_date', 10),
        'sort': clean('sort', 30),
        'direction': clean('dir', 4).lower(),
    }
    if filters['sort'] not in SORTABLE_FIELDS:
        filters['sort'] = ''
    if filters['direction'] not in ('asc', 'desc'):
        filters['direction'] = ''
    for field in FIELD_FILTERS:
        filters[field] = clean(field)

    records = recent_requests(limit=500, **filters)

    # For the spreadsheet view, populate each column's filter dropdown with the
    # distinct values actually present in the log (from the unfiltered set, so
    # choosing one value never blanks out the other columns).
    options = {}
    if request.GET.get('view') == 'table' and request.GET.get('format') not in ('json', 'csv'):
        from .request_log import column_options
        options = column_options(recent_requests(limit=1000))

    if request.GET.get('format') == 'json':
        return JsonResponse({
            'status': 'success',
            'count': len(records),
            'filters': {k: v for k, v in filters.items() if v},
            'requests': [request_to_dict(r) for r in records],
        })

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="request_log.csv"'
        # BOM so Excel opens the UTF-8 file without mangling accented data.
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow([
            'Time', 'Method', 'Status', 'Category', 'Path', 'Query',
            'Client IP', 'Username', 'Duration (ms)', 'Response size (B)',
            'Time on page (ms)', 'Clicks', 'View', 'Referer', 'User-Agent',
            'Payload',
        ])
        for r in records:
            writer.writerow([
                timezone.localtime(r.created_at).strftime('%Y-%m-%d %H:%M:%S'),
                r.method, r.status_code, r.get_category_display(),
                r.path, r.query, r.client_ip, r.username, r.duration_ms,
                r.response_size, r.time_on_page_ms, r.click_count,
                r.view_name, r.referer, r.user_agent, r.request_body,
            ])
        return response

    from .ip_block import active_blocked_ips
    blocked = active_blocked_ips()

    view_mode = 'table' if request.GET.get('view') == 'table' else 'cards'
    return render(request, 'core/admin_requests.html', {
        'panel': 'log',
        'records': records,
        'count': len(records),
        'total': request_total(),
        # Every currently blocked address, so each row can offer Block or Unblock.
        'blocked_ips': blocked,
        'blocked_total': len(blocked),
        'view_mode': view_mode,
        'filters': filters,
        'options': options,
        'presets': list(RequestLogPreset.objects.all()[:50]),
        'methods': ('GET', 'POST', 'HEAD', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'),
        # Keep the old context keys so the card view's chips still work.
        'category': category,
        'method': method,
        'status': status,
        'q': filters['q'],
    })


@csrf_exempt
@require_POST
def ajax_page_activity(request):
    """Beacon: attach time-on-page and click count to the matching page row.

    Sent by the site's own JS when a visitor leaves a page. Anonymous, so no
    login is required; it only ever updates an existing page row.
    """
    from .request_log import record_page_activity, _client_ip as _rl_client_ip
    try:
        data = json.loads(request.body or b'{}')
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    record_page_activity(
        path=data.get('path') or '',
        client_ip=_rl_client_ip(request),
        time_ms=data.get('time_ms'),
        clicks=data.get('clicks'),
    )
    return JsonResponse({'status': 'success'})


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_register(request):
    """POST /api/user/register — register a new Android user with email verification."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    email = body.get('email', '').strip().lower()
    password = body.get('password', '')
    display_name = body.get('displayName', '') or body.get('name', '')

    if not email or not password:
        return JsonResponse({'status': 'error', 'message': 'Email and password are required'}, status=400)

    # Validate email domain
    allowed_domains = ['gmail.com', 'yahoo.com', 'yahoo.co.in']
    email_domain = email.split('@')[-1] if '@' in email else ''
    if email_domain not in allowed_domains:
        return JsonResponse({'status': 'error', 'message': "We don't support this email. Please use Gmail or Yahoo."}, status=400)

    if len(password) < 6:
        return JsonResponse({'status': 'error', 'message': 'Password must be at least 6 characters'}, status=400)

    # Check if already registered (case-insensitive so Alok.AK639@gmail.com matches alok.ak639@gmail.com)
    existing_user = User.objects.filter(email__iexact=email).first()
    if existing_user and existing_user.has_usable_password():
        return JsonResponse({'status': 'error', 'message': 'Email is already registered. Please login instead.'}, status=409)

    if existing_user:
        # Android-synced user — set their password, send verification
        existing_user.set_password(password)
        existing_user.first_name = display_name or existing_user.first_name
        existing_user.is_active = False
        existing_user.save()
        django_user = existing_user
        user_obj, _ = SyncedUser.objects.get_or_create(
            email=email,
            defaults={
                'user': django_user, 'display_name': display_name,
                'device_id': body.get('deviceId', ''),
                'device_model': body.get('deviceModel', ''),
                'os_version': body.get('osVersion', ''),
                'app_version': body.get('appVersion', ''),
                'build_number': body.get('buildNumber', 0),
            },
        )
        user_obj.user = django_user
        user_obj.save(update_fields=['user'])
        created = False
    else:
        # New user — create everything
        base_username = email.split('@')[0]
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{counter}"
            counter += 1
        django_user = User.objects.create_user(username=username, email=email, password=password)
        django_user.first_name = display_name
        django_user.is_active = False
        django_user.save()
        user_obj = SyncedUser.objects.create(
            user=django_user, email=email, display_name=display_name,
            device_id=body.get('deviceId', ''),
            device_model=body.get('deviceModel', ''),
            os_version=body.get('osVersion', ''),
            app_version=body.get('appVersion', ''),
            build_number=body.get('buildNumber', 0),
        )
        UserCloudData.objects.create(user=django_user)
        created = True

    # Send verification email with link token + 6-digit OTP (app can verify via OTP)
    _token, otp = _issue_verification_email(django_user, request, display_name=display_name)
    _lift_block_after_auth(request)

    resp = {
        'status': 'success',
        'message': 'Account registered. Check your inbox for the verification code.',
        'requiresVerification': True,
        # Include the OTP so the app can pre-match it against the user's input
        'otp': otp,
        'user': _user_payload(user_obj, django_user),
        'subscription': FREE_SUBSCRIPTION,
    }
    return JsonResponse(resp)


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_login(request):
    """POST /api/user/login — authenticate user and return full account + cloud data."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    email = body.get('email', '').strip().lower()
    password = body.get('password', '')

    if not email or not password:
        return JsonResponse({'status': 'error', 'message': 'Email and password are required'}, status=400)

    # Failed attempts are rate limited per IP and per email, but only *failed*
    # attempts count and a correct password always gets in. Checking the
    # buckets before authenticating (as this used to) meant that once a bucket
    # was exhausted the account owner could not log in even with the right
    # password: ten typos from one person behind a shared carrier/office IP
    # (or ten wrong guesses aimed at an address) locked everyone out for half
    # an hour and reported "Too many requests" as a login failure.
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', '')

    def _limit_response():
        """429 when either bucket is spent, otherwise None (keep going)."""
        allowed, _, retry_after = check_rate_limit(ip, 'login', max_attempts=RATE_LIMIT_IP_MAX_ATTEMPTS)
        if not allowed:
            return rate_limit_response(retry_after)
        allowed_email, _, retry_email = check_rate_limit(email, 'login_email')
        if not allowed_email:
            return rate_limit_response(retry_email)
        return None

    def _failed():
        """Record this failed attempt, then honour the limiter."""
        record_rate_limit_attempt(ip, 'login')
        record_rate_limit_attempt(email, 'login_email')
        return _limit_response()

    django_user = User.objects.filter(email__iexact=email).first()
    if not django_user:
        return _failed() or JsonResponse(
            {'status': 'error', 'message': 'Email is not registered. Please register first.'}, status=404)

    if not django_user.has_usable_password():
        return _limit_response() or JsonResponse({
            'status': 'error',
            'message': 'This account was created from Android sync. Please register with a password first.',
        }, status=400)

    if not django_user.is_active:
        return _limit_response() or JsonResponse({
            'status': 'error',
            'message': 'Email not verified. Please check your inbox for the verification code.',
            'requiresVerification': True,
        }, status=403)

    auth_user = authenticate(request, username=django_user.username, password=password)
    if not auth_user:
        return _failed() or JsonResponse({'status': 'error', 'message': 'Invalid password'}, status=401)

    # Success — reset rate limits
    reset_rate_limit(ip, 'login')
    reset_rate_limit(email, 'login_email')

    # Get or create synced profile
    user_obj, _ = SyncedUser.objects.get_or_create(
        email=email, defaults={'user': auth_user, 'display_name': auth_user.first_name},
    )
    # A SyncedUser created by an earlier sync can hold a stale FK to a Django
    # user that no longer exists. Dereferencing `.user` then raises
    # User.DoesNotExist and 500s the login, so compare the FK id directly and
    # repoint it at the authenticated user.
    if not user_obj.user_id or user_obj.user_id != auth_user.pk:
        user_obj.user = auth_user
        user_obj.save(update_fields=['user'])

    # Update device info
    device_id = body.get('deviceId', '')
    if device_id:
        user_obj.device_id = device_id
        user_obj.device_model = body.get('deviceModel', '')
        user_obj.os_version = body.get('osVersion', '')
        user_obj.app_version = body.get('appVersion', '')
        user_obj.build_number = body.get('buildNumber', 0)
        user_obj.save()

    _record_session(auth_user, 'android', request, device_model=body.get('deviceModel', ''), os_version=body.get('osVersion', ''), app_version=body.get('appVersion', ''))
    _lift_block_after_auth(request)

    # Admin access control: a full/app block refuses login outright.
    block_err = _enforce_user_block(email, app_match)
    if block_err:
        return block_err

    return JsonResponse(_build_user_response(auth_user, user_obj, {
        'message': 'Login successful',
        'requiresVerification': False,
    }))


def _sync_rate_ok(key, limit):
    """Fixed-window request counter for /api/user/sync.

    Returns (allowed, retry_after_seconds). Kept in the cache on purpose: it
    is a cheap in-memory check, so a throttled client costs one dictionary
    lookup instead of the database reads and the half-megabyte payload a real
    sync builds.
    """
    now = timezone.now().timestamp()
    state = cache.get(key)
    if not state or state.get('reset', 0) <= now:
        cache.set(key, {'count': 1, 'reset': now + _USER_SYNC_WINDOW_SECONDS},
                  _USER_SYNC_WINDOW_SECONDS)
        return True, 0
    if state.get('count', 0) >= limit:
        return False, max(1, int(round(state['reset'] - now)))
    state['count'] = state.get('count', 0) + 1
    cache.set(key, state, max(1, int(round(state['reset'] - now))))
    return True, 0


def _sync_rate_limited(request, email, body, app_match=None):
    """429 when this account/device — or this address — syncs too fast.

    A client stuck in a retry loop asks every second or two; every answer it
    gets carries the user's whole cloud payload (~0.5 MB here), so a single
    looping phone was enough to pin both gunicorn workers on a 1-vCPU box and
    make nginx return 504s for everybody else. Throttled calls now come back
    as a tiny 429 with Retry-After, which costs no database work and lets the
    rest of the site breathe. Real clients sync a handful of times a minute.

    The budget is per account, not per device: the looping client observed in
    production sent a new device id with almost every attempt, so a
    device-scoped counter never filled up. One account syncing faster than
    this from several devices at once is not a workload worth protecting.
    """
    # The limits are per app, so a chatty release can be slowed from the
    # Settings page without touching anybody else's build.
    per_account = int(getattr(app_match, 'sync_max_per_minute', 0)
                      or _USER_SYNC_MAX_PER_WINDOW)
    per_address = int(getattr(app_match, 'sync_ip_max_per_minute', 0)
                      or _USER_SYNC_IP_MAX_PER_WINDOW)
    allowed, retry_after = _sync_rate_ok(f'usersync_rl:{email}', per_account)
    if not allowed:
        return _sync_too_fast_response(retry_after)
    allowed, retry_after = _sync_rate_ok(f'usersync_rl:ip:{_get_client_ip(request)}',
                                         per_address)
    if not allowed:
        return _sync_too_fast_response(retry_after)
    return None


def _sync_too_fast_response(retry_after):
    response = JsonResponse({
        'status': 'error',
        'message': 'Syncing too often. Please wait a moment and try again.',
        'retryable': True,
        'retryAfter': retry_after,
    }, status=429)
    response['Retry-After'] = str(retry_after)
    return response


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_sync(request):
    """POST /api/user/sync — bidirectional sync of user data between app and server."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    email = body.get('email', '').strip().lower()
    if not email:
        return JsonResponse({'status': 'error', 'message': 'Email is required'}, status=400)

    # Admin access control: full/app block refuses data sync.
    block_err = _enforce_user_block(email, app_match)
    if block_err:
        return block_err

    rate_err = _sync_rate_limited(request, email, body, app_match)
    if rate_err:
        return rate_err

    # Throttle full syncs per user. Between full syncs, answer from stored
    # cloud data with a single read — no writes, no TMDB calls. The payload
    # shape is identical so the app can treat the response the same way.
    throttle_key = f'usersync:{email}'
    if cache.get(throttle_key) is not None:
        recent_user = User.objects.filter(email__iexact=email).first()
        if recent_user:
            recent_cloud = UserCloudData.objects.filter(user=recent_user).first()
            if recent_cloud:
                recent_profile = SyncedUser.objects.filter(email__iexact=email).first()
                return JsonResponse({
                    'status': 'success',
                    'message': 'User data synced successfully',
                    'subscription': recent_profile.subscription_payload() if recent_profile else FREE_SUBSCRIPTION,
                    'userData': recent_cloud.get_cloud_payload(),
                })
    cache.set(throttle_key, timezone.now(),
              int(getattr(app_match, 'sync_min_interval_seconds', 0)
                  or _USER_SYNC_MIN_INTERVAL))

    # Create or get user + profile
    django_user, user_obj, created = _ensure_user_and_profile(email, body)

    # Merge incoming userData from app into cloud data
    incoming_data = body.get('userData', {})
    cloud, _ = UserCloudData.objects.get_or_create(user=django_user)
    cloud.merge_incoming(incoming_data)

    # Also pull web WatchList + PlayHistory into cloud so the app gets it
    cloud.sync_from_web_models()
    cloud.refresh_from_db()

    # Build response — subscription + merged cloud data for app to restore
    sub_payload = user_obj.subscription_payload() if user_obj else FREE_SUBSCRIPTION
    return JsonResponse({
        'status': 'success',
        'message': 'User data synced successfully',
        'subscription': sub_payload,
        'userData': cloud.get_cloud_payload(),
    })


# --- deletion sync (app -> server) ------------------------------------------
# The app calls these the moment the user deletes watch history, watchlist
# entries, custom playlists or individual playlist items — and when it clears
# history/watchlist wholesale. Each endpoint purges the cloud JSON, mirrors the
# deletion into the web-facing WatchList/PlayHistory rows, records tombstones
# (so no later sync, from any device, resurrects the items) and answers with
# the updated cloud payload so the app can reconcile immediately.

def _deletion_ids(body, *keys):
    """First non-empty list (or single scalar) under any of the given keys."""
    for key in keys:
        val = body.get(key)
        if isinstance(val, list) and val:
            return val
        if val and not isinstance(val, dict):
            return [val]
    return []


def _plain_id(item):
    """Media/playlist id out of one app-supplied entry (scalar or dict)."""
    if isinstance(item, dict):
        return item.get('mediaId') or item.get('id') or item.get('tmdbId') or item.get('playlistId')
    return item


def _int_ids(ids):
    """The subset of ids parseable as ints — for the web-row mirrors, whose
    tmdb_id columns are integers."""
    out = []
    for i in ids:
        try:
            out.append(int(i))
        except (TypeError, ValueError):
            continue
    return out


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_delete(request, kind):
    """POST /api/user/history|watchlist|playlist(/item)/delete|clear —
    real-time deletion notifications from the Android app.

    ``kind`` is one of: history, watchlist, playlist, playlist_item,
    history_clear, watchlist_clear. Deleting an id that is not present still
    records its tombstone (the item may exist on another device), so the
    call is idempotent and safe to retry.
    """
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    return _item_deletion_response(body, kind, app_match)


# Maps the app's ``deleteType`` onto the internal kind, so a call that routed
# itself through the generic /api/user/delete/ path still lands on the right
# branch.
_DELETE_TYPE_KINDS = {
    'history': 'history',
    'clear_history': 'history_clear',
    'watchlist': 'watchlist',
    'clear_watchlist': 'watchlist_clear',
    'playlist': 'playlist',
    'playlist_item': 'playlist_item',
}


def _item_deletion_response(body, kind, app_match=None, derive_from_body=False):
    """Apply one real-time item deletion and answer with the updated payload.

    Shared by the dedicated /api/user/<kind>/delete|clear/ routes and by the
    item mode of /api/user/delete/ — the app posts both shapes to that path.
    The caller has already validated the app credentials; ``body`` is the parsed
    request JSON.

    ``derive_from_body`` makes the kind optional: it is taken from the body's
    ``deleteType`` instead, which is how the generic delete path works. Without
    a usable ``deleteType`` that mode refuses rather than guessing, because
    guessing wrong here could clear far more than the app asked for.
    """
    email = (body.get('email') or '').strip().lower()
    if not email:
        return JsonResponse({'status': 'error', 'message': 'Email is required'}, status=400)

    # Admin access control: a full/app block stops all writes to this account.
    block_err = _enforce_user_block(email, app_match)
    if block_err:
        return block_err

    # If the app routed by `deleteType`, honor the matching kind so a generic
    # /delete/ call with deleteType=history lands on the history branch.
    dt = (body.get('deleteType') or '').strip().lower()
    _dt_kind = _DELETE_TYPE_KINDS.get(dt)
    if _dt_kind:
        kind = _dt_kind
    elif derive_from_body:
        return JsonResponse({
            'status': 'error',
            'message': ('deleteType is required to delete items. '
                        'Use one of: history, clear_history, watchlist, '
                        'clear_watchlist, playlist, playlist_item.'),
        }, status=400)

    # The generic /api/user/delete/ mode must not conjure an account into
    # existence — anything to delete has to belong to a real account in the
    # first place, and minting an active user from an unauthenticated body would
    # hand out free accounts. The dedicated per-kind routes keep their historical
    # auto-create behaviour so nothing the app already ships changes underneath it.
    if derive_from_body:
        account_exists = (User.objects.filter(email__iexact=email).exists()
                          or SyncedUser.objects.filter(email__iexact=email).exists())
        if not account_exists:
            return JsonResponse({
                'status': 'error',
                'message': 'No account found with this email address.',
            }, status=404)

    django_user, user_obj, _created = _ensure_user_and_profile(email, body)
    cloud, _ = UserCloudData.objects.get_or_create(user=django_user)
    from .models import PlayHistory, WatchList

    if kind == 'history':
        ids = [_plain_id(i) for i in _deletion_ids(body, 'ids', 'items', 'mediaIds', 'tmdbIds', 'mediaId')]
        ids = [i for i in ids if i not in (None, '')]
        removed = cloud.apply_deletions(deleted_history=ids)
        ints = _int_ids(ids)
        if ints:
            PlayHistory.objects.filter(user=django_user, tmdb_id__in=ints).delete()
        message = 'History deleted'

    elif kind == 'watchlist':
        ids = [_plain_id(i) for i in _deletion_ids(body, 'ids', 'items', 'mediaIds', 'tmdbIds', 'mediaId')]
        ids = [i for i in ids if i not in (None, '')]
        removed = cloud.apply_deletions(deleted_watchlist=ids)
        ints = _int_ids(ids)
        if ints:
            WatchList.objects.filter(user=django_user, tmdb_id__in=ints).delete()
        message = 'Watchlist items deleted'

    elif kind == 'playlist':
        ids = [_plain_id(i) for i in _deletion_ids(body, 'playlistIds', 'ids', 'items', 'playlistId')]
        ids = [i for i in ids if i not in (None, '')]
        removed = cloud.apply_deletions(deleted_playlists=ids)
        message = 'Playlists deleted'

    elif kind == 'playlist_item':
        keys = _deletion_ids(body, 'keys', 'itemKeys', 'items')
        keys = [k for k in keys if k not in (None, '')]
        # Accept the compact form {playlistId: X, itemIds: [..]} and the app's
        # exact shape {playlistId: 42, mediaId: 27205, isTv: false}.
        pid = body.get('playlistId') or body.get('playlist_id')
        if pid and not keys:
            item_ids = _deletion_ids(body, 'itemIds', 'mediaIds', 'ids', 'mediaId')
            keys = ['%s:%s' % (pid, _plain_id(i)) for i in item_ids if _plain_id(i) not in (None, '')]
        removed = cloud.apply_deletions(deleted_playlist_items=keys)
        message = 'Playlist items deleted'

    elif kind in ('history_clear', 'watchlist_clear'):
        if kind == 'history_clear':
            existing = [_plain_id(h) for h in (cloud.watch_history or [])]
            existing += [k.split('_')[1] for k in (cloud.playback_progress or {})
                         if len(k.split('_')) >= 2 and k.split('_')[1].isdigit()]
            existing = [i for i in existing if i not in (None, '')]
            removed = cloud.apply_deletions(deleted_history=existing)
            cloud.watch_history = []
            cloud.playback_progress = {}
            cloud.save()
            PlayHistory.objects.filter(user=django_user).delete()
            message = 'History cleared'
        else:
            existing = [_plain_id(w) for w in (cloud.watchlist_ids or [])]
            existing = [i for i in existing if i not in (None, '')]
            removed = cloud.apply_deletions(deleted_watchlist=existing)
            cloud.watchlist_ids = []
            cloud.save()
            WatchList.objects.filter(user=django_user).delete()
            message = 'Watchlist cleared'

    else:
        return JsonResponse({'status': 'error', 'message': 'Unknown deletion kind'}, status=404)

    # Let the next full sync merge for real instead of serving the throttled
    # cached response captured before this deletion.
    cache.delete(f'usersync:{email}')

    return JsonResponse({
        'status': 'success',
        'message': message,
        'deleted': removed,
        'userData': cloud.get_cloud_payload(),
    })


@csrf_exempt
@require_http_methods(["GET"])
@_guard_android_api_errors
def api_user_profile(request):
    """GET /api/user/profile/ — fetch user profile + subscription via JWT token."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    # Extract JWT from X-User-Token header (can't use Authorization: Bearer since Basic auth uses it)
    token = request.META.get('HTTP_X_USER_TOKEN', '')
    if not token:
        return JsonResponse({'status': 'error', 'message': 'Missing X-User-Token header'}, status=401)
    payload = decode_jwt_token(token)
    if not payload:
        return JsonResponse({'status': 'error', 'message': 'Invalid or expired token'}, status=401)
    django_user = User.objects.filter(pk=int(payload.get('sub', 0))).first()
    if not django_user:
        return JsonResponse({'status': 'error', 'message': 'User not found'}, status=404)
    user_obj = SyncedUser.objects.filter(user=django_user).first()
    return JsonResponse(_build_user_response(django_user, user_obj, {
        'message': 'Profile loaded successfully',
    }))


# ---------------------------------------------------------------------------
# User access control (admin-set block modes) + heartbeat telemetry
# ---------------------------------------------------------------------------

def _get_synced_profile(email):
    """SyncedUser row for an email (None when the account predates sync)."""
    if not email:
        return None
    return SyncedUser.objects.filter(email__iexact=email).first()


def _enforce_user_block(email, app_match=None, require_play=False, require_data=True):
    """Return an error JsonResponse when the admin has blocked this user, else None.

    Modes (SyncedUser.block_mode):
      'full' — stop access. Every API call is refused with 403 so the app
               shows "access blocked" and cannot pull any data.
      'app'  — app block. Data APIs answer 503 "server down" so the app's own
               heartbeat/offline logic flips to isServerOffline and falls back
               to downloaded-only playback (no data, connection-error UX).
      'play' — play block. Streaming/source endpoints refuse (403) so movies
               and series won't play; data browsing stays allowed.

    ``require_play`` marks a playback-only endpoint (only 'play'/'full' block
    it). ``require_data`` marks a data endpoint ('app'/'full' block it). An
    endpoint can pass both (blocked in every mode).
    """
    prof = _get_synced_profile(email)
    if prof is None or not prof.block_mode:
        return None
    mode = prof.block_mode
    if mode == 'full' or (mode == 'app' and require_data) or (mode == 'play' and require_play):
        if mode == 'app':
            # Mimic an unreachable server so the app shows its offline modal.
            return JsonResponse({'status': 'error', 'message': 'Server is unreachable. Downloaded items remain available.'}, status=503)
        reason = prof.blocked_reason or 'Contact the administrator for details.'
        return JsonResponse({'status': 'error', 'code': 'access_blocked', 'blockMode': mode,
                             'message': f'Your access has been restricted. {reason}'}, status=403)
    return None


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_heartbeat(request):
    """POST /api/user/heartbeat — 1-minute background heartbeat from the app.

    Records the current device + real-time activity so the admin panel can show
    who is online, what they are watching, and which app version they run. The
    response carries the email-verification state and the account's block flags
    so the app can react immediately (offline modal for 'app' block, playback
    refusal for 'play' block).
    """
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    email = (body.get('email') or '').strip().lower()
    if not email:
        return JsonResponse({'status': 'error', 'message': 'Email is required'}, status=400)

    block_err = _enforce_user_block(email, app_match)
    if block_err:
        return block_err

    activity = body.get('activity') or {}
    django_user = User.objects.filter(email__iexact=email).first()
    # The account's verification state is server-authoritative (is_active is
    # what /api/user/verify-email/ sets). Never trust the client's echo of it
    # here — the app gates playback on this value, so a tampered client could
    # otherwise bypass the "Verify Email to Play" requirement. Only fall back
    # to the client's flag when no account exists yet (nothing to check).
    verified = bool(django_user.is_active) if django_user else bool(body.get('isEmailVerified', True))

    def _int_or_none(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    hb, _created = UserHeartbeat.objects.update_or_create(
        email=email,
        defaults={
            'user': django_user,
            'app_user_id': str(body.get('userId') or ''),
            'is_logged_in': bool(body.get('isLoggedIn', True)),
            'is_email_verified': verified,
            'device_id': str(body.get('deviceId') or ''),
            'device_model': str(body.get('deviceModel') or ''),
            'os_version': str(body.get('osVersion') or ''),
            'app_version': str(body.get('appVersion') or ''),
            'current_screen': str(activity.get('currentScreen') or ''),
            'is_playing': bool(activity.get('isPlaying', False)),
            'playback_position_ms': _int_or_none(activity.get('playbackPositionMs')) or 0,
            'duration_ms': _int_or_none(activity.get('durationMs')) or 0,
            'media_title': str(activity.get('mediaTitle') or ''),
            'media_id': _int_or_none(activity.get('mediaId')),
            'media_is_tv': bool(activity.get('isTv', False)),
            'season': _int_or_none(activity.get('season')),
            'episode': _int_or_none(activity.get('episode')),
            'player_engine': str(activity.get('playerEngine') or ''),
            'recent_actions': (activity.get('recentActions') or [])[:50],
            'ip_address': _get_client_ip(request) or None,
        },
    )

    _record_heartbeat_activity(hb)
    _link_installs_for_heartbeat(hb)
    _remember_account_device(hb)

    prof = _get_synced_profile(email)
    return JsonResponse({
        'status': 'success',
        'isEmailVerified': verified,
        'blockMode': (prof.block_mode if prof else '') or '',
        'isSubscribed': bool(prof.is_subscribed) if prof else False,
        'timestamp': int(timezone.now().timestamp() * 1000),
    })


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_forgot_password(request):
    """POST /api/user/forgot-password — send password reset email."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)
    email = body.get('email', '').strip().lower()
    if not email:
        return JsonResponse({'status': 'error', 'message': 'Email is required'}, status=400)
    # Always return success to prevent email enumeration; the reset uid/token
    # (used by the app's token path) is only included when the user exists.
    django_user = User.objects.filter(email__iexact=email).first()
    resp = {'status': 'success', 'message': 'Password reset instructions sent to your email.'}
    if django_user and django_user.has_usable_password():
        # Email contains both the 6-digit reset code (for the app) and a browser link
        reset_info = _issue_password_reset_email(django_user, request)
        resp.update({
            # Include the OTP so the app can pre-match it against the user's input
            'otp': reset_info.get('otp'),
            'resetToken': reset_info.get('token'),
            'resetUid': reset_info.get('uid'),
            'resetUrl': reset_info.get('resetUrl'),
        })
    else:
        # No account (or Android-synced account without a password yet) — the
        # app still shows the OTP step, but there is nothing to validate.
        resp.update({'otp': None, 'resetToken': None, 'resetUid': None, 'resetUrl': None})
    return JsonResponse(resp)


def _profile_for_ip(ip_address):
    """The single account whose heartbeats come from this address, else None.

    Guest sessions are ignored: a device that is signed out and signed in
    heartbeats from the same address, and counting the guest as a second owner
    would make every address look shared. Two real accounts on one address
    (carrier NAT, an office) still resolve to None — guessing there would attach
    someone else's visits to an account.
    """
    from .models import SyncedUser, UserHeartbeat

    if not ip_address:
        return None
    emails = list(UserHeartbeat.objects.filter(ip_address=ip_address)
                  .values_list('email', flat=True).distinct()[:10])
    owners = {}
    for email in emails:
        profile = SyncedUser.objects.filter(email=email).exclude(device_id='').first()
        if profile is not None:
            owners[profile.pk] = profile
    if len(owners) != 1:
        return None
    return next(iter(owners.values()))


def _resolve_account_key(user_id, ip_address):
    """The account device id behind an install record, or '' when unknown.

    The app posts its account device id to sync and heartbeat but a different
    (per-install) id to the install/visit endpoint. When the two happen to be the
    same value the id itself is the key. Otherwise the visitor's IP bridges them:
    if every real account heartbeating from that address is the same one, it
    owns the install.
    """
    from .models import SyncedUser, UserHeartbeat

    user_id = (user_id or '').strip()
    if user_id and (SyncedUser.objects.filter(device_id=user_id).exists()
                    or UserHeartbeat.objects.filter(device_id=user_id).exists()):
        return user_id
    profile = _profile_for_ip(ip_address)
    return profile.device_id if profile else ''


def _link_installs_for_heartbeat(hb):
    """Join install records that hit this account's address to the account.

    Called on every heartbeat, when the address is current: an install that
    requested something from the same IP in the last hour is that account's
    device. Shared addresses are refused by ``_profile_for_ip``.
    """
    from .models import AndroidAppDevice, AndroidAppDeviceVisit

    if not hb.ip_address or not hb.device_id:
        return
    profile = _profile_for_ip(hb.ip_address)
    if profile is None or profile.device_id != hb.device_id:
        return
    since = timezone.now() - timezone.timedelta(hours=1)
    device_pks = list(AndroidAppDeviceVisit.objects
                      .filter(ip_address=hb.ip_address, visited_at__gte=since)
                      .values_list('device_id', flat=True).distinct()[:20])
    if device_pks:
        AndroidAppDevice.objects.filter(pk__in=device_pks).exclude(
            account_key=hb.device_id).update(account_key=hb.device_id)


def _remember_account_device(hb):
    """Keep the account's device list up to date from the heartbeat."""
    from .models import AccountDevice

    if not hb.email or not hb.device_id:
        return
    try:
        AccountDevice.objects.update_or_create(
            email=hb.email, device_id=hb.device_id,
            defaults={'device_model': hb.device_model, 'app_version': hb.app_version})
    except Exception as exc:  # pragma: no cover - never break a heartbeat
        logger.warning('account device not recorded for %s: %s', hb.email, exc)


def _log_subscription_event(profile, actor, action, before=None):
    """Record one subscription change; never let auditing break the save."""
    from .models import SubscriptionEvent

    before = before or {}
    try:
        SubscriptionEvent.objects.create(
            profile_id=profile.pk,
            email=profile.email,
            actor=(actor or '')[:150],
            action=(action or 'update')[:40],
            was_subscribed=bool(before.get('is_subscribed')),
            is_subscribed=bool(profile.is_subscribed),
            old_plan=(before.get('plan') or '')[:100],
            new_plan=(profile.plan or '')[:100],
            old_valid_until=before.get('valid_until'),
            new_valid_until=profile.valid_until,
        )
    except Exception as exc:  # pragma: no cover - auditing must not block a save
        logger.warning('subscription event not recorded for %s: %s', profile.email, exc)


def _record_heartbeat_activity(hb):
    """Append what the newest heartbeat changed, then keep the log short."""
    from .models import HeartbeatActivity

    previous = HeartbeatActivity.objects.filter(email=hb.email).order_by('-created_at').first()
    if previous and (previous.screen == hb.current_screen
                     and previous.media_title == hb.media_title
                     and previous.media_id == hb.media_id
                     and previous.is_playing == hb.is_playing):
        return
    HeartbeatActivity.objects.create(
        email=hb.email, device_id=hb.device_id, screen=hb.current_screen,
        media_title=hb.media_title, media_id=hb.media_id, is_tv=hb.media_is_tv,
        season=hb.season, episode=hb.episode, is_playing=hb.is_playing,
        position_ms=hb.playback_position_ms, duration_ms=hb.duration_ms)
    keep = HeartbeatActivity.ACTIVITY_KEEP
    stale = list(HeartbeatActivity.objects.filter(email=hb.email)
                 .order_by('-created_at').values_list('pk', flat=True)[keep:])
    if stale:
        HeartbeatActivity.objects.filter(pk__in=stale).delete()


def _get_client_ip(request):
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            or request.META.get('REMOTE_ADDR', ''))


def _otp_rate_limit_error(request, email, action):
    """Return a 429 response when this IP/email exceeds the attempt budget
    for `action`, otherwise None. Per-IP and per-email budgets are tracked
    independently so rotating IPs cannot brute-force a 6-digit code."""
    ip = _get_client_ip(request)
    allowed, _, retry_after = check_rate_limit(ip, action)
    if not allowed:
        return rate_limit_response(retry_after)
    if email:
        allowed_email, _, retry_email = check_rate_limit(email, f'{action}_email')
        if not allowed_email:
            return rate_limit_response(retry_email)
    return None


def _record_otp_attempt(request, email, action):
    ip = _get_client_ip(request)
    record_rate_limit_attempt(ip, action)
    if email:
        record_rate_limit_attempt(email, f'{action}_email')


def _reset_otp_rate_limit(request, email, action):
    ip = _get_client_ip(request)
    reset_rate_limit(ip, action)
    if email:
        reset_rate_limit(email, f'{action}_email')


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_verify_email(request):
    """POST /api/user/verify-email/ — verify email with a 6-digit OTP (Android app)
    or with the link token (browser). Payload: {email, otp} or {email?, token}."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)
    token = body.get('token', '').strip()
    otp_code = body.get('otp', '').strip()
    email = body.get('email', '').strip().lower()

    from .models import EmailVerification

    if otp_code and email:
        # Android OTP flow: {email, otp} — find the latest unverified code for this user
        # Rate-limit code attempts per IP and per email (10 tries / 15 min) so a
        # wrong-code flood can't brute-force the 6-digit code.
        rate_err = _otp_rate_limit_error(request, email, 'verify_otp')
        if rate_err:
            return rate_err
        django_user = User.objects.filter(email__iexact=email).first()
        if not django_user:
            _record_otp_attempt(request, email, 'verify_otp')
            return JsonResponse({'status': 'error', 'message': 'Invalid verification code.'}, status=400)
        ev = EmailVerification.objects.filter(
            user=django_user, otp=otp_code, verified=False,
        ).order_by('-created_at').first()
        if not ev:
            _record_otp_attempt(request, email, 'verify_otp')
            return JsonResponse({'status': 'error', 'message': 'Invalid verification code.'}, status=400)
        if ev.is_expired:
            _record_otp_attempt(request, email, 'verify_otp')
            return JsonResponse({'status': 'error', 'message': 'Verification code has expired. Please request a new code.'}, status=400)
    elif token:
        # Browser link flow: {token} or {email, token}
        ev = EmailVerification.objects.select_related('user').filter(token=token)
        if email:
            ev = ev.filter(user__email=email)
        ev = ev.first()
        if not ev:
            return JsonResponse({'status': 'error', 'message': 'Invalid verification link.'}, status=400)
        if ev.is_expired:
            return JsonResponse({'status': 'error', 'message': 'Verification link has expired. Please register again.'}, status=400)
    else:
        return JsonResponse({'status': 'error', 'message': 'Provide either (email + otp) or token.'}, status=400)

    # Valid code/token — clear any earlier failed attempts so a user who
    # mistyped a few times isn't left locked out.
    if otp_code and email:
        _reset_otp_rate_limit(request, email, 'verify_otp')
    if ev.verified:
        return JsonResponse({'status': 'success', 'message': 'Email already verified. You can now login.'})
    ev.verified = True
    ev.save(update_fields=['verified'])
    try:
        ev.user.is_active = True
        ev.user.save(update_fields=['is_active'])
    except Exception:
        pass  # user may have been deleted
    return JsonResponse({'status': 'success', 'message': 'Email verified successfully! You can now login.'})


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_resend_verification(request):
    """POST /api/user/resend-verification/ — send a fresh verification code/link.
    For users who registered (or reset from Android sync) but never received or
    never used the first email. Payload: {email}. Always returns success to avoid
    leaking which emails are registered; only registered, unverified users get mail."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)
    email = body.get('email', '').strip().lower()
    if not email:
        return JsonResponse({'status': 'error', 'message': 'Email is required'}, status=400)

    django_user = User.objects.filter(email__iexact=email).first()
    if not django_user or not django_user.has_usable_password():
        # Never confirm account existence — generic success for unknown emails too
        return JsonResponse({'status': 'success', 'message': 'If your email is registered, a new verification code has been sent to your inbox.'})
    if django_user.is_active:
        return JsonResponse({'status': 'success', 'message': 'This email is already verified. You can login now.'})
    _token, otp = _issue_verification_email(django_user, request, display_name=django_user.first_name)
    return JsonResponse({
        'status': 'success',
        'message': 'A new verification code has been sent to your inbox. It expires in 10 minutes.',
        'requiresVerification': True,
        'otp': otp,
    })


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_reset_password(request):
    """POST /api/user/reset-password/ — reset password via OTP (Android) or token (web)."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    new_password = body.get('newPassword', '')
    if not new_password:
        return JsonResponse({'status': 'error', 'message': 'newPassword is required'}, status=400)
    if len(new_password) < 6:
        return JsonResponse({'status': 'error', 'message': 'Password must be at least 6 characters'}, status=400)

    otp_code = body.get('otp', '').strip()
    email = body.get('email', '').strip().lower()
    uidb64 = body.get('uid', '').strip()
    token = body.get('token', '').strip()

    django_user = None

    # Flow 1: OTP (Android app) — {email, otp, newPassword}
    # Rate-limit code attempts per IP and per email (10 tries / 15 min) so a
    # wrong-code flood can't brute-force the 6-digit code.
    if otp_code and email:
        rate_err = _otp_rate_limit_error(request, email, 'reset_otp')
        if rate_err:
            return rate_err
        from .models import PasswordResetOTP
        django_user = User.objects.filter(email__iexact=email).first()
        if not django_user:
            _record_otp_attempt(request, email, 'reset_otp')
            return JsonResponse({'status': 'error', 'message': 'Invalid OTP or email.'}, status=400)
        otp_obj = PasswordResetOTP.objects.filter(
            user=django_user, otp=otp_code, used=False
        ).order_by('-created_at').first()
        if not otp_obj or otp_obj.is_expired:
            _record_otp_attempt(request, email, 'reset_otp')
            return JsonResponse({'status': 'error', 'message': 'Invalid or expired OTP.'}, status=400)
        otp_obj.used = True
        otp_obj.save(update_fields=['used'])

    # Flow 2: Token (web link backward compat) — {uid, token, newPassword}
    elif uidb64 and token:
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_decode
        try:
            uid = urlsafe_base64_decode(uidb64)
            django_user = User.objects.get(pk=uid)
        except (ValueError, User.DoesNotExist):
            return JsonResponse({'status': 'error', 'message': 'Invalid reset link.'}, status=400)
        if not default_token_generator.check_token(django_user, token):
            return JsonResponse({'status': 'error', 'message': 'Invalid or expired reset link.'}, status=400)

    else:
        return JsonResponse({'status': 'error', 'message': 'Provide either (email + otp) or (uid + token) with newPassword.'}, status=400)

    # Valid code/link — clear earlier failed attempts for this user/IP.
    if otp_code and email:
        _reset_otp_rate_limit(request, email, 'reset_otp')
    django_user.set_password(new_password)
    django_user.is_active = True
    django_user.save(update_fields=['password', 'is_active'])
    return JsonResponse({'status': 'success', 'message': 'Password reset successful! You can now login.'})


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_verify_reset_otp(request):
    """POST /api/user/verify-reset-otp/ — validate the reset OTP before the
    user types a new password. Payload: {email, otp}.

    Lets the app confirm the code is correct (without consuming it) before
    showing the new-password screen. The code is still verified again — and
    consumed — by /api/user/reset-password/ when the reset actually happens.
    """
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    otp_code = body.get('otp', '').strip()
    email = body.get('email', '').strip().lower()
    if not otp_code or not email:
        return JsonResponse({'status': 'error', 'message': 'Email and OTP are required'}, status=400)

    # Rate-limit code attempts per IP and per email (10 tries / 15 min) so a
    # wrong-code flood can't brute-force the 6-digit code.
    rate_err = _otp_rate_limit_error(request, email, 'verify_reset_otp')
    if rate_err:
        return rate_err

    from .models import PasswordResetOTP
    django_user = User.objects.filter(email__iexact=email).first()
    if not django_user:
        _record_otp_attempt(request, email, 'verify_reset_otp')
        return JsonResponse({'status': 'error', 'message': 'Invalid OTP or email.'}, status=400)
    otp_obj = PasswordResetOTP.objects.filter(
        user=django_user, otp=otp_code, used=False
    ).order_by('-created_at').first()
    if not otp_obj or otp_obj.is_expired:
        _record_otp_attempt(request, email, 'verify_reset_otp')
        return JsonResponse({'status': 'error', 'message': 'Invalid or expired OTP. Please request a new code.'}, status=400)

    # Valid code — clear earlier failed attempts so a mistyped code isn't a
    # lockout. The OTP is NOT marked used here; reset-password consumes it.
    _reset_otp_rate_limit(request, email, 'verify_reset_otp')
    return JsonResponse({'status': 'success', 'message': 'OTP verified. You can now set a new password.'})


@_guard_android_api_errors
@csrf_exempt
@require_http_methods(["POST"])
def api_user_refresh_token(request):
    """POST /api/user/refresh/ — get new access token using refresh token."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)
    refresh_token = body.get('refreshToken', '')
    if not refresh_token:
        return JsonResponse({'status': 'error', 'message': 'refreshToken is required'}, status=400)
    payload = decode_jwt_token(refresh_token)
    if not payload or payload.get('type') != 'refresh':
        return JsonResponse({'status': 'error', 'message': 'Invalid or expired refresh token'}, status=401)
    django_user = User.objects.filter(pk=int(payload.get('sub', 0))).first()
    if not django_user or not django_user.is_active:
        return JsonResponse({'status': 'error', 'message': 'User not found or inactive'}, status=401)
    tokens = create_user_token_pair(django_user)
    return JsonResponse({
        'status': 'success',
        'message': 'Token refreshed',
        'user': {
            'userId': f'usr_{django_user.pk}',
            'email': django_user.email,
            'token': tokens['token'],
            'refreshToken': tokens['refreshToken'],
            'tokenExpiresIn': tokens['expiresIn'],
        },
    })


def _purge_user_rows(django_user_id, *, also_emails=()):
    """Delete every row that hangs off a Django user, by id.

    ``on_delete`` is a promise the database cannot keep here. These models
    live on the external users DB while ``auth.User`` lives on ``default``,
    every link is declared with ``db_constraint=False``, and SQLite cannot
    cascade across two attached databases. So deleting the auth row leaves
    the profile, cloud data, history, watchlist, sessions and OTPs behind —
    and because Django reuses the lowest free primary key, the *next*
    account to be created inherits them.

    Every model that points at ``User`` is listed explicitly below and
    removed while its owner's id is still known. Some are additionally keyed
    by the email the deleted account held, because that is the only link
    those rows have.
    """
    from .models import (
        SyncedUser, UserProfile, UserCloudData, UserSession, PlayHistory,
        WatchList, EmailVerification, PasswordResetOTP, AccountDeletionOTP,
        UserActivity, UserHeartbeat, UserPageView, Subscriber,
        WebsiteVisitor,
    )

    if django_user_id:
        UserProfile.objects.filter(user_id=django_user_id).delete()
        UserCloudData.objects.filter(user_id=django_user_id).delete()
        UserSession.objects.filter(user_id=django_user_id).delete()
        PlayHistory.objects.filter(user_id=django_user_id).delete()
        WatchList.objects.filter(user_id=django_user_id).delete()
        EmailVerification.objects.filter(user_id=django_user_id).delete()
        PasswordResetOTP.objects.filter(user_id=django_user_id).delete()
        AccountDeletionOTP.objects.filter(user_id=django_user_id).delete()
        UserActivity.objects.filter(user_id=django_user_id).delete()
        UserHeartbeat.objects.filter(user_id=django_user_id).delete()
        UserPageView.objects.filter(user_id=django_user_id).delete()
        # The visitor log outlives the account that produced it: it is traffic
        # history, not personal data, so the row is unlinked rather than
        # deleted — which is what its declared SET_NULL asks for but cannot do
        # across two databases.
        WebsiteVisitor.objects.filter(user_id=django_user_id).update(user=None)

    # Dedupe: the same address can arrive from the auth row and the synced
    # profile, and each address triggers its own sweep.
    seen = set()
    for address in also_emails:
        address = (address or '').strip().lower()
        if not address or address in seen:
            continue
        seen.add(address)
        SyncedUser.objects.filter(email__iexact=address).delete()
        UserHeartbeat.objects.filter(email__iexact=address).delete()
        Subscriber.objects.filter(email__iexact=address).delete()
        for django_user in User.objects.filter(email__iexact=address):
            if django_user.pk == django_user_id:
                continue
            # A second auth row sharing the address (Google sign-in creates
            # the account by email): purge its dependants and the row itself.
            _purge_user_rows(django_user.pk, also_emails=())


def _permanently_delete_user(user_obj=None, email=''):
    """Permanently remove a user and ALL linked data keyed by email.

    Links every record — Django auth user, Android synced profile, cloud
    data, sessions, watchlist, play history, verification tokens, reset
    OTPs, and newsletter subscription — to the same email and deletes them
    all together. Call with either the SyncedUser or the Django User; the
    other half is located by email so nothing is orphaned.

    The dependant rows are removed explicitly (see ``_purge_user_rows``)
    rather than relying on CASCADE, which does not fire across the two
    databases this project splits user data over.
    """
    from .models import SyncedUser
    email = (email or '').strip().lower()
    if not email and user_obj:
        email = getattr(user_obj, 'email', '') or ''
        if not email and hasattr(user_obj, 'user') and user_obj.user_id:
            linked = User.objects.filter(pk=user_obj.user_id).first()
            email = linked.email if linked else ''
    email = (email or '').strip().lower()

    django_user = None
    if user_obj is not None and isinstance(user_obj, User):
        django_user = user_obj
    elif user_obj is not None and user_obj.user_id:
        django_user = User.objects.filter(pk=user_obj.user_id).first()
    if django_user is None and email:
        django_user = User.objects.filter(email=email).first()

    # Android synced profile and everything else keyed to this account.
    # Collected before the auth row goes away, because the id is what links
    # the two databases and it is gone the moment the row is.
    synced_user = None
    if email:
        synced_user = SyncedUser.objects.filter(email__iexact=email).first()
    if synced_user is None and django_user is not None and django_user.pk:
        synced_user = SyncedUser.objects.filter(user_id=django_user.pk).first()

    emails = [email]
    if django_user is not None:
        for linked_user in User.objects.filter(pk=django_user.pk):
            if linked_user.email:
                emails.append(linked_user.email)
    if synced_user is not None and synced_user.email:
        emails.append(synced_user.email)

    _purge_user_rows(
        django_user.pk if django_user is not None else None,
        also_emails=emails,
    )

    # The linked Django auth user itself.
    if django_user:
        django_user.delete()

    return email or 'unknown'


# ---------------------------------------------------------------------------
# Google Sign-In (POST /api/user/google-login)
# ---------------------------------------------------------------------------
# The Android app obtains a Google ID token (a signed JWT) after the user picks
# an account and posts it here. The token is the proof of identity; the email
# in the body is only a hint and is never trusted on its own. The token is
# verified against Google's public keys (via the tokeninfo endpoint, which
# checks the signature, audience, issuer and expiry for us) and the identity is
# taken from the token's claims.

# Web OAuth client ID the ID token's `aud` must equal. Overridable via the
# GOOGLE_OAUTH_CLIENT_ID setting / env var so a new client can be rotated
# without a code change.
GOOGLE_OAUTH_CLIENT_ID = getattr(
    settings, 'GOOGLE_OAUTH_CLIENT_ID',
    '621729506326-1nqvn8vsk5abvbm0hvd9mm4ijce3dva1.apps.googleusercontent.com',
)
GOOGLE_TOKENINFO_URL = 'https://oauth2.googleapis.com/tokeninfo'
GOOGLE_ISSUERS = ('accounts.google.com', 'https://accounts.google.com')


def _verify_google_id_token(id_token):
    """Verify a Google ID token and return its claims, or None when invalid.

    Uses Google's tokeninfo endpoint, which validates the JWT signature against
    Google's rotating public keys and returns the claims. We then re-check the
    audience, issuer, expiry and email_verified flag ourselves so a token minted
    for a different app (or an unverified address) can never sign anyone in.

    The accepted audience is whatever the operator configured for the site, and
    both the saved client ID and the deploy-time default are accepted so
    rotating the ID in the admin never locks out tokens from the other one.
    """
    if not id_token or not isinstance(id_token, str):
        return None
    try:
        resp = requests.get(GOOGLE_TOKENINFO_URL, params={'id_token': id_token}, timeout=8)
    except requests.RequestException as e:
        logger.warning('Google tokeninfo request failed: %s', e)
        return None
    if resp.status_code != 200:
        logger.info('Google tokeninfo rejected the token (HTTP %s)', resp.status_code)
        return None
    try:
        claims = resp.json()
    except ValueError:
        return None
    if not isinstance(claims, dict):
        return None

    # Audience must be a client ID we own.
    from .google_auth import resolve_client_id
    accepted_audiences = {GOOGLE_OAUTH_CLIENT_ID, resolve_client_id()}
    accepted_audiences.discard('')
    if claims.get('aud') not in accepted_audiences:
        logger.info('Google token audience mismatch: %s', claims.get('aud'))
        return None
    # Issuer must be Google.
    if claims.get('iss') not in GOOGLE_ISSUERS:
        logger.info('Google token issuer mismatch: %s', claims.get('iss'))
        return None
    # Expiry must be in the future.
    try:
        if int(claims.get('exp', 0)) <= int(timezone.now().timestamp()):
            return None
    except (TypeError, ValueError):
        return None
    # The address must be verified by Google.
    if str(claims.get('email_verified', '')).lower() not in ('true', '1'):
        return None
    if not claims.get('sub') or not claims.get('email'):
        return None
    return claims


@_guard_android_api_errors
@csrf_exempt
@_https_only
@_require_post
@require_http_methods(["POST"])
def api_user_google_login(request):
    """POST /api/user/google-login — sign in (or sign up) with a Google ID token.

    Verifies the token, then finds the account by Google ``sub`` (falling back
    to the email for accounts that registered with a password first, linking the
    ``sub`` to that record). Creates the account when it does not exist. Returns
    the same payload shape as /api/user/login so the app can restore history,
    favorites, watchlist and playlists from ``userData`` right after login.
    """
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    # The operator's switch governs every client, the app included.
    from .google_auth import google_login_enabled, email_domain_allowed
    if not google_login_enabled():
        return JsonResponse({'status': 'error', 'message': 'Google sign-in is disabled'}, status=403)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    id_token = body.get('idToken') or body.get('id_token') or ''
    claims = _verify_google_id_token(id_token)
    if not claims:
        return JsonResponse({'status': 'error', 'message': 'Google token verification failed'}, status=401)

    # Identity comes from the token's claims, never from the body.
    sub = str(claims.get('sub'))
    email = (claims.get('email') or '').strip().lower()
    display_name = (claims.get('name') or '').strip()
    photo_url = (claims.get('picture') or '').strip()

    if not email:
        return JsonResponse({'status': 'error', 'message': 'Google token verification failed'}, status=401)

    if not email_domain_allowed(email):
        return JsonResponse({'status': 'error', 'message': 'This Google account is not allowed to sign in here.'}, status=403)

    # Admin access control: a full/app block refuses login outright.
    block_err = _enforce_user_block(email, app_match)
    if block_err:
        return block_err

    django_user, user_obj, _created = _google_login_account(claims, device=body)

    UserCloudData.objects.get_or_create(user=django_user)

    _record_session(django_user, 'android', request,
                    device_model=body.get('deviceModel', ''),
                    os_version=body.get('osVersion', ''),
                    app_version=body.get('appVersion', ''))
    _lift_block_after_auth(request)

    return JsonResponse(_build_user_response(django_user, user_obj, {
        'message': 'Signed in with Google',
        'requiresVerification': False,
    }))


def _google_login_account(claims, *, device=None):
    """Resolve a verified Google token to a Django user, creating it if needed.

    Shared by the Android API and the website popup so both agree on identity:
    the Google ``sub`` is the stable key, the email is the fallback that lets an
    account which started with a password be linked to its Google identity on
    first Google sign-in. Returns ``(django_user, synced_user, created)``.

    ``device`` is an optional dict of Android device fields; the web path passes
    none and the SyncedUser row is still created so the account is visible to
    the rest of the app.
    """
    from .models import SyncedUser

    device = device or {}
    sub = str(claims.get('sub'))
    email = (claims.get('email') or '').strip().lower()
    display_name = (claims.get('name') or '').strip()
    photo_url = (claims.get('picture') or '').strip()

    # 1) The stable identifier.
    user_obj = (SyncedUser.objects.filter(google_sub=sub).first()
                or SyncedUser.objects.filter(google_id=sub).first())
    django_user = None
    if user_obj and user_obj.user_id:
        django_user = User.objects.filter(pk=user_obj.user_id).first()

    # 2) Fall back to the email (account created with a password first).
    if django_user is None:
        django_user = User.objects.filter(email__iexact=email).first()
    if user_obj is None and django_user is not None:
        user_obj = SyncedUser.objects.filter(email__iexact=email).first()

    created = False
    if django_user is None:
        # Google already verified the address, so no OTP step is needed.
        base_username = email.split('@')[0] or 'user'
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{counter}"
            counter += 1
        django_user = User.objects.create_user(username=username, email=email, password=None)
        django_user.first_name = display_name
        django_user.is_active = True
        django_user.save()
        created = True

    if not django_user.is_active:
        django_user.is_active = True
        django_user.save(update_fields=['is_active'])

    if user_obj is None:
        user_obj = SyncedUser.objects.create(
            user=django_user, email=email, display_name=display_name,
            photo_url=photo_url, google_sub=sub, google_id=sub,
            auth_provider='google', is_email_verified=True,
            device_id=device.get('deviceId', ''),
            device_model=device.get('deviceModel', ''),
            os_version=device.get('osVersion', ''),
            app_version=device.get('appVersion', ''),
            build_number=device.get('buildNumber', 0),
        )
    else:
        if not user_obj.user_id or user_obj.user_id != django_user.pk:
            user_obj.user = django_user
        user_obj.email = email
        user_obj.google_sub = sub
        user_obj.google_id = sub
        user_obj.auth_provider = 'google'
        # Google has already proven the address, so it is verified even when
        # the row was created by a password registration that never confirmed.
        user_obj.is_email_verified = True
        user_obj.display_name = display_name or user_obj.display_name
        user_obj.photo_url = photo_url or user_obj.photo_url
        if device.get('deviceId'):
            user_obj.device_id = device.get('deviceId', '')
            user_obj.device_model = device.get('deviceModel', '')
            user_obj.os_version = device.get('osVersion', '')
            user_obj.app_version = device.get('appVersion', '')
            user_obj.build_number = device.get('buildNumber', 0)
        user_obj.save()

    return django_user, user_obj, created


@require_http_methods(["POST"])
def ajax_google_login(request):
    """POST /ajax/google-login — the website's "Continue with Google" path.

    The browser hands over the ID token from Google Identity Services. This is
    the web sibling of ``/api/user/google-login``: no Android credentials are
    involved, the token is the whole proof of identity, and the resulting
    session is an ordinary Django session. Refuses outright when the operator
    has switched Google sign-in off, so hiding the button is not the only guard.
    """
    from .google_auth import google_login_enabled, email_domain_allowed

    if not google_login_enabled():
        return JsonResponse({'success': False, 'message': 'Google sign-in is disabled'}, status=403)

    id_token = (request.POST.get('credential') or request.POST.get('id_token')
                or request.POST.get('idToken') or '').strip()
    claims = _verify_google_id_token(id_token)
    if not claims:
        return JsonResponse({'success': False, 'message': 'Google sign-in could not be verified. Please try again.'}, status=401)

    email = (claims.get('email') or '').strip().lower()
    if not email_domain_allowed(email):
        return JsonResponse({'success': False, 'message': 'This Google account is not allowed to sign in here.'}, status=403)

    # Same block rules as a password login: a blocked address cannot get in.
    block_err = _enforce_user_block(email)
    if block_err:
        return block_err

    django_user, _user_obj, created = _google_login_account(claims)

    login(request, django_user, backend='django.contrib.auth.backends.ModelBackend')
    _record_session(django_user, 'web', request)
    _lift_block_after_auth(request)

    logger.info('Google web sign-in for %s (%s)', django_user.username, 'new account' if created else 'existing account')
    return JsonResponse({
        'success': True,
        'message': 'Account created - welcome!' if created else 'Signed in with Google',
        'created': created,
        'username': django_user.username,
        'email': django_user.email,
    })


# ---------------------------------------------------------------------------
# Account deletion with OTP (POST /api/user/delete/request-otp + /api/user/delete)
# ---------------------------------------------------------------------------
# Two-step flow the app relies on: request a code, then delete with that code.
# The delete endpoint is the single source of truth — if it returns an error the
# app keeps everything intact, so a failed call can never leave the device wiped
# while the account still exists. The OTP is validated server-side only.

# Rate limits for the delete-code request: 1 per 60 s per account, 5 per hour.
_DELETE_OTP_MIN_INTERVAL = 60
_DELETE_OTP_MAX_PER_HOUR = 5
_DELETE_OTP_HOUR_WINDOW = 3600


def _find_account_for_delete(user_id, email):
    """Locate (django_user, synced_user) by userId then email, or (None, None)."""
    from .models import SyncedUser
    django_user = None
    user_obj = None

    # userId is the app's 'usr_<pk>' string.
    if user_id:
        pk = None
        try:
            pk = int(str(user_id).replace('usr_', '').strip())
        except (TypeError, ValueError):
            pk = None
        if pk:
            django_user = User.objects.filter(pk=pk).first()
            if django_user:
                user_obj = SyncedUser.objects.filter(user=django_user).first()

    if django_user is None and email:
        django_user = User.objects.filter(email__iexact=email).first()
        if django_user:
            user_obj = SyncedUser.objects.filter(email__iexact=email).first()
    if user_obj is None and email:
        user_obj = SyncedUser.objects.filter(email__iexact=email).first()
        if user_obj and user_obj.user_id and django_user is None:
            django_user = User.objects.filter(pk=user_obj.user_id).first()

    return django_user, user_obj


def _delete_otp_rate_error(email):
    """Return a 429 response when the delete-code request budget is spent.

    Two windows per account: at most one request per 60 s, and at most five per
    hour. Kept in the cache so a throttled client costs one lookup.
    """
    now = timezone.now().timestamp()
    short_key = f'deleteotp:short:{email}'
    hour_key = f'deleteotp:hour:{email}'

    last = cache.get(short_key)
    if last and (now - last) < _DELETE_OTP_MIN_INTERVAL:
        wait = int(round(_DELETE_OTP_MIN_INTERVAL - (now - last)))
        return JsonResponse({
            'status': 'error',
            'message': f'Too many requests. Try again in {max(wait, 1)} seconds.',
        }, status=429)

    hour = cache.get(hour_key) or {'count': 0, 'reset': now + _DELETE_OTP_HOUR_WINDOW}
    if hour.get('reset', 0) <= now:
        hour = {'count': 0, 'reset': now + _DELETE_OTP_HOUR_WINDOW}
    if hour.get('count', 0) >= _DELETE_OTP_MAX_PER_HOUR:
        wait = max(1, int(round(hour['reset'] - now)))
        return JsonResponse({
            'status': 'error',
            'message': f'Too many requests. Try again in {wait} seconds.',
        }, status=429)

    # Record this request against both windows.
    cache.set(short_key, now, _DELETE_OTP_MIN_INTERVAL)
    hour['count'] = hour.get('count', 0) + 1
    cache.set(hour_key, hour, max(1, int(round(hour['reset'] - now))))
    return None


def _invalidate_user_sessions(django_user):
    """Mark every active session for a user as logged out.

    Deleting the account removes the rows anyway, but this is also used to
    revoke tokens explicitly so an old token cannot keep working.
    """
    from .models import UserSession
    try:
        UserSession.objects.filter(user=django_user, is_active=True).update(
            is_active=False, logged_out_at=timezone.now())
    except Exception:
        logger.warning('Could not invalidate sessions for user %s', django_user.pk, exc_info=True)


@_guard_android_api_errors
@csrf_exempt
@_https_only
@_require_post
@require_http_methods(["POST"])
def api_user_delete_request_otp(request):
    """POST /api/user/delete/request-otp — email a 6-digit deletion code.

    Nothing is deleted here. The code is stored with a 10-minute expiry and a
    maximum of 5 verification attempts; any previously issued deletion code for
    the account is invalidated first.
    """
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    email = (body.get('email') or '').strip().lower()
    user_id = (body.get('userId') or '').strip()

    django_user, user_obj = _find_account_for_delete(user_id, email)
    if django_user is None:
        return JsonResponse({'status': 'error', 'message': 'No account found with this email address.'}, status=404)

    account_email = (django_user.email or email).strip().lower()

    # Admin access control: a full/app block stops the delete flow too.
    block_err = _enforce_user_block(account_email, app_match)
    if block_err:
        return block_err

    rate_err = _delete_otp_rate_error(account_email)
    if rate_err:
        return rate_err

    from .models import AccountDeletionOTP, SiteSettings

    # Invalidate any previously issued deletion code for this account.
    AccountDeletionOTP.objects.filter(user=django_user, used=False).update(used=True)

    otp = AccountDeletionOTP.generate()
    AccountDeletionOTP.objects.create(user=django_user, otp=AccountDeletionOTP.hash_code(otp))

    brand = SiteSettings.get_settings().brand_name if hasattr(SiteSettings, 'get_settings') else 'NewMovies'
    try:
        send_configured_email(
            subject=f'Account deletion code - {brand}',
            message=(
                f'Hi {django_user.first_name or django_user.username},\r\n\r\n'
                f'Your account deletion code is: {otp}\r\n\r\n'
                f'This code is valid for 10 minutes. Enter it in the app to permanently delete your account.\r\n\r\n'
                f'If you did not request this, please ignore this email — your account is safe.'
            ),
            recipient_list=[account_email],
            purpose='verification',
            fail_silently=True,
        )
    except Exception as e:
        logger.warning(f'Failed to send account deletion email: {e}', exc_info=True)

    return JsonResponse({
        'status': 'success',
        'message': f'A 6-digit verification code has been sent to {account_email}.',
    })


@_guard_android_api_errors
@csrf_exempt
@_https_only
@_require_post
@require_http_methods(["POST"])
def api_user_delete_account(request):
    """POST /api/user/delete — account deletion *or* an item deletion.

    The app posts two unrelated shapes to this one path, so the body decides
    which flow runs:

    * ``confirmDelete`` true **and** an ``otp`` present → permanently delete the
      account after validating the emailed code (the flow documented below).
    * ``action == "delete"`` (no ``otp``) → a real-time item deletion, routed by
      ``deleteType`` onto the same code the dedicated
      /api/user/<kind>/delete|clear/ endpoints use.

    The order matters: the account branch is checked first and requires *both*
    a code and the explicit confirmation flag, so a per-item deletion can never
    destroy an account, and an account deletion can never be mistaken for one.
    Anything that fits neither shape is refused rather than guessed at.
    """
    app_match, err = _validate_android_auth(request)
    if err:
        return err

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    if not isinstance(body, dict):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    otp_code = (body.get('otp') or '').strip()
    confirms = body.get('confirmDelete') in (True, 'true', 'True', 1, '1')
    action = (body.get('action') or '').strip().lower()
    delete_type = (body.get('deleteType') or '').strip().lower()

    # Item-deletion mode. `action` is the app's marker for it; a body that
    # carries only a deleteType and no code/confirmation is treated the same
    # way so a client that omits `action` still cannot lose an account.
    if not otp_code and not confirms and (action == 'delete' or delete_type):
        return _item_deletion_response(body, None, app_match, derive_from_body=True)

    if not otp_code:
        return JsonResponse({'status': 'error', 'message': 'Verification code is required'}, status=400)

    if not confirms:
        # An account deletion must say so explicitly; a code alone is not
        # enough to tell the two flows apart.
        return JsonResponse({
            'status': 'error',
            'message': 'Confirmation is required. Send "confirmDelete": true with the verification code.',
        }, status=400)

    email = (body.get('email') or '').strip().lower()
    user_id = (body.get('userId') or '').strip()

    django_user, user_obj = _find_account_for_delete(user_id, email)
    if django_user is None:
        return JsonResponse({'status': 'error', 'message': 'No account found with this email address.'}, status=404)

    account_email = (django_user.email or email).strip().lower()

    block_err = _enforce_user_block(account_email, app_match)
    if block_err:
        return block_err

    from .models import AccountDeletionOTP

    otp_obj = AccountDeletionOTP.objects.filter(
        user=django_user, used=False,
    ).order_by('-created_at').first()

    if otp_obj is None or otp_obj.is_expired or otp_obj.too_many_attempts:
        return JsonResponse({'status': 'error', 'message': 'Incorrect verification code'}, status=400)

    if not otp_obj.verify_code(otp_code):
        # Count the failed attempt so the code cannot be brute-forced.
        otp_obj.attempts = otp_obj.attempts + 1
        otp_obj.save(update_fields=['attempts'])
        return JsonResponse({'status': 'error', 'message': 'Incorrect verification code'}, status=400)

    # Correct code — consume it and delete everything in one transaction.
    # The user row, the Android profile, cloud data, sessions, watchlist, play
    # history, verification/reset OTPs and the newsletter subscription all go in
    # a single commit: a half-finished deletion would leave a signed-in ghost
    # that can never be removed again.
    from django.db import transaction

    try:
        with transaction.atomic():
            otp_obj.used = True
            otp_obj.save(update_fields=['used'])

            # Revoke every active session token before the rows go away.
            _invalidate_user_sessions(django_user)

            # _permanently_delete_user removes the auth row, the Android
            # synced profile, cloud data, sessions, watchlist, play history,
            # verification/reset/deletion OTPs and the newsletter
            # subscription. It deletes each one explicitly — CASCADE does not
            # reach across the two databases this project splits them over.
            _permanently_delete_user(django_user, email=account_email)
    except DatabaseError:
        raise
    except Exception:
        logger.exception('Account deletion failed for %s', account_email)
        return JsonResponse({
            'status': 'error',
            'message': 'Could not delete the account. Please try again.',
        }, status=500)

    # Drop any cached copy of the account so a later request cannot be served
    # from it.
    try:
        cache.delete(f'usersync:{account_email}')
    except Exception:
        logger.warning('Could not clear the sync cache for %s', account_email, exc_info=True)

    logger.info('Account deleted from the app: %s', account_email)
    return JsonResponse({'status': 'success', 'message': 'Account deleted'})


# ---------------------------------------------------------------------------
# Account deletion with OTP — the website side of the same flow.
# ---------------------------------------------------------------------------
# The account is taken from the session, never from the request body, so a
# signed-in visitor can only ever delete their own account. Same model, same
# rate limit and same delete routine as the app, so both paths behave alike.

@login_required
@require_http_methods(["POST"])
def ajax_delete_account_request_otp(request):
    """POST /ajax/delete-account/request-otp/ — email a 6-digit deletion code.

    Nothing is deleted here; the code is the confirmation that the person at the
    keyboard owns the mailbox on the account.
    """
    django_user = request.user
    account_email = (django_user.email or '').strip().lower()
    if not account_email:
        return JsonResponse({'success': False, 'message': 'Your account has no email address on file, so a deletion code cannot be sent.'}, status=400)

    rate_err = _delete_otp_rate_error(account_email)
    if rate_err:
        return rate_err

    from .models import AccountDeletionOTP

    # Any earlier code for this account stops working.
    AccountDeletionOTP.objects.filter(user=django_user, used=False).update(used=True)

    otp = AccountDeletionOTP.generate()
    AccountDeletionOTP.objects.create(user=django_user, otp=AccountDeletionOTP.hash_code(otp))

    brand = SiteSettings.get_settings().brand_name
    try:
        send_configured_email(
            subject=f'Account deletion code - {brand}',
            message=(
                f'Hi {django_user.first_name or django_user.username},\r\n\r\n'
                f'Your account deletion code is: {otp}\r\n\r\n'
                f'This code is valid for 10 minutes. Enter it on your profile page to permanently delete your account. '
                f'Everything you have saved on this site will be removed and cannot be recovered.\r\n\r\n'
                f'If you did not request this, please ignore this email — your account is safe.'
            ),
            recipient_list=[account_email],
            purpose='verification',
            fail_silently=True,
        )
    except Exception as e:
        logger.warning(f'Failed to send account deletion email: {e}', exc_info=True)

    logger.info('Account deletion code requested from the web for %s', account_email)
    return JsonResponse({
        'success': True,
        'message': f'A 6-digit code has been sent to {account_email}. It expires in 10 minutes.',
    })


@login_required
@require_http_methods(["POST"])
def ajax_delete_account(request):
    """POST /ajax/delete-account/ — permanently delete the signed-in account.

    Requires the emailed code plus the literal word DELETE, so a stray click can
    never wipe an account. Every failure keeps the account and the session
    intact; only a correct code deletes anything.
    """
    django_user = request.user
    account_email = (django_user.email or '').strip().lower()

    otp_code = (request.POST.get('otp') or '').strip()
    confirm_text = (request.POST.get('confirm') or '').strip().upper()

    if confirm_text != 'DELETE':
        return JsonResponse({'success': False, 'message': 'Type DELETE in the confirmation box to continue.'}, status=400)
    if not otp_code:
        return JsonResponse({'success': False, 'message': 'Enter the 6-digit code sent to your email.'}, status=400)

    from .models import AccountDeletionOTP

    otp_obj = AccountDeletionOTP.objects.filter(
        user=django_user, used=False,
    ).order_by('-created_at').first()

    if otp_obj is None or otp_obj.is_expired or otp_obj.too_many_attempts:
        return JsonResponse({'success': False, 'message': 'Incorrect or expired code. Request a new one.'}, status=400)

    if not otp_obj.verify_code(otp_code):
        otp_obj.attempts = otp_obj.attempts + 1
        otp_obj.save(update_fields=['attempts'])
        return JsonResponse({'success': False, 'message': 'Incorrect verification code.'}, status=400)

    otp_obj.used = True
    otp_obj.save(update_fields=['used'])

    _invalidate_user_sessions(django_user)

    from django.contrib.auth import logout as _logout
    _logout(request)

    _permanently_delete_user(django_user, email=account_email)

    try:
        cache.delete(f'usersync:{account_email}')
    except Exception:
        logger.warning('Could not clear the sync cache for %s', account_email, exc_info=True)

    logger.info('Account deleted from the web: %s', account_email)
    return JsonResponse({'success': True, 'message': 'Your account has been permanently deleted.'})


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_users_list(request):
    """Admin page listing all synced Android users."""
    users = SyncedUser.objects.all().order_by('-last_synced_at')
    total = users.count()
    subscribed = users.filter(is_subscribed=True).count()
    free = total - subscribed
    # Latest heartbeat per user, keyed by email, so the table can show online
    # status, platform, app version and real-time activity.
    hb_map = {h.email: h for h in UserHeartbeat.objects.all()}
    return render(request, 'core/synced_users_list.html', {
        'users': users,
        'total': total,
        'subscribed': subscribed,
        'free': free,
        'hb_map': hb_map,
    })


# Media keys the Android app stores in the cloud payload. Three shapes are seen
# in real data: 'movie_<id>_<season>_<episode>', 'tv_<id>_<season>_<episode>' and
# 'seen_<id>_<isTv>_<season>_<episode>'. Plain ids are accepted too.
def _cloud_media_from_key(key):
    """Decode a cloud-data media key into (tmdb_id, is_tv, season, episode)."""
    if isinstance(key, bool):
        return None, None, None, None
    if isinstance(key, int):
        return (key, None, None, None)
    if isinstance(key, float):
        # Only an integral float is a usable id; never silently truncate.
        return ((int(key), None, None, None) if key.is_integer()
                else (None, None, None, None))
    if not isinstance(key, str):
        return None, None, None, None
    parts = key.strip().split('_')
    if not parts or not parts[0]:
        return None, None, None, None
    if parts[0].isdigit():
        return (int(parts[0]), None, None, None)
    if parts[0] not in ('movie', 'tv', 'seen') or len(parts) < 2:
        return None, None, None, None
    try:
        tmdb_id = int(parts[1])
    except (IndexError, ValueError):
        return None, None, None, None

    def _num(value):
        try:
            n = int(value)
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    if parts[0] == 'seen':
        is_tv = len(parts) > 2 and parts[2].lower() == 'true'
        rest = parts[3:]
    else:
        is_tv = parts[0] == 'tv'
        rest = parts[2:]
    season = _num(rest[0]) if rest else None
    episode = _num(rest[1]) if len(rest) > 1 else None
    return tmdb_id, is_tv, season, episode


def _cloud_item(raw=None, key=None, rating=None):
    """One cloud-sync entry as {key, tmdb_id, is_tv, season, episode, ...}.

    Values in the entry win over what the key says, because the app writes the
    authoritative mediaId/isTv/season/episode inside the entry itself.
    """
    data = raw if isinstance(raw, dict) else {}
    if key is None and not data and raw is not None:
        key = raw
    k_id, k_tv, k_season, k_episode = _cloud_media_from_key(key)

    def _pos(value):
        n = _safe_int(value)
        return n if n > 0 else None

    tmdb_id = (_pos(data.get('mediaId')) or _pos(data.get('tmdbId'))
               or _pos(data.get('id')) or k_id)
    is_tv = data.get('isTv')
    if not isinstance(is_tv, bool):
        is_tv = k_tv
    return {
        'key': key if key is not None else (str(tmdb_id) if tmdb_id else ''),
        'tmdb_id': tmdb_id,
        'is_tv': is_tv,
        'season': _pos(data.get('season')) or k_season,
        'episode': _pos(data.get('episode')) or k_episode,
        'title': (data.get('title') or data.get('name') or '').strip(),
        'poster_path': (data.get('posterPath') or data.get('poster_path') or '').strip(),
        'position_ms': _safe_int(data.get('positionMs')),
        'duration_ms': _safe_int(data.get('durationMs')),
        'last_updated': data.get('lastUpdated') or data.get('lastWatchedEpoch') or '',
        'rating': rating if rating is not None else data.get('rating'),
    }


def _ms_label(ms):
    """Milliseconds as a short human label ('1h 12m', '43s')."""
    try:
        total = int(ms) // 1000
    except (TypeError, ValueError):
        return ''
    if total <= 0:
        return ''
    if total < 60:
        return '%ss' % total
    if total < 3600:
        return '%sm %ss' % (total // 60, total % 60)
    return '%sh %sm' % (total // 3600, (total % 3600) // 60)


def _cloud_ts_label(value):
    """An app timestamp (epoch ms for most fields, epoch s or text otherwise)
    as a readable IST stamp."""
    if value in (None, '', 0, '0'):
        return ''
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)[:40]
    if n <= 0:
        return ''
    seconds = n / 1000.0 if n > 10 ** 11 else float(n)
    try:
        stamp = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
        return timezone.localtime(stamp).strftime('%b %d, %Y %H:%M')
    except Exception:
        return ''


# Highest TMDB id worth building a link for. The app sometimes syncs a key whose
# id is garbage (e.g. seen_2136685465_false_1_1); a link to it could only ever
# land on a 404, so those entries are listed without Details/Play buttons.
MAX_TMDB_ID = 10000000

# Keep a page render bounded even if a user's cloud payload is enormous.
# MAX_CLOUD_ITEMS is how many entries get titles/posters/links resolved;
# CLOUD_ITEMS_PER_SECTION is how many of those are actually written into the
# page. A heavy account stores 1,600+ playback entries and a thousand seen
# keys, and rendering all of them made the page 4.4 MB and cost almost three
# seconds of worker time to build — on a 1-vCPU box that is the difference
# between a snappy admin page and one that queues behind everything else.
MAX_CLOUD_ITEMS = 400
CLOUD_ITEMS_PER_SECTION = 50


def _cloud_sync_sections(cloud, linked_user=None):
    """The Cloud Sync Data boxes as clickable lists of openable items.

    Every box on the synced-user page becomes a section whose items carry the
    link to that title's details page and to the web player, so the admin goes
    from a stored key straight to the real movie/series and can play it. Titles
    and posters come from the entry itself when the app sent them, otherwise from
    that user's own PlayHistory rows (no network call).
    """
    if cloud is None:
        return []

    known = {}
    if linked_user is not None:
        try:
            for ph in PlayHistory.objects.filter(user=linked_user).order_by('-last_played_at')[:500]:
                if ph.tmdb_id and ph.tmdb_id not in known:
                    known[ph.tmdb_id] = {'title': ph.title or '', 'poster': ph.poster_path or ''}
        except Exception:
            logger.debug('cloud sync: play history lookup failed', exc_info=True)

    def _as_list(value):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):   # some app versions store a map here
            return list(value.values())
        return []

    def _finish(items):
        # Only ever called with at most CLOUD_ITEMS_PER_SECTION entries: the
        # title/poster lookup, the date labels and the reverse() calls cost more
        # than the markup, so enriching entries that are never rendered was the
        # real reason the page took seconds to build.
        for item in items:
            hit = known.get(item['tmdb_id']) if item['tmdb_id'] else None
            if hit:
                if not item['title']:
                    item['title'] = hit['title']
                if not item['poster_path']:
                    item['poster_path'] = hit['poster']
            # What to show when the app never sent a title: the type + id is far
            # more useful to the admin than the raw stored key.
            if item['title']:
                item['label'] = item['title']
            elif item['tmdb_id']:
                item['label'] = '%s #%s' % ('Series' if item['is_tv'] else 'Movie',
                                           item['tmdb_id'])
            else:
                item['label'] = item['key'] or '—'
            item['detail_url'] = ''
            item['play_url'] = ''
            item['position_label'] = _ms_label(item['position_ms'])
            item['duration_label'] = _ms_label(item['duration_ms'])
            item['updated_label'] = _cloud_ts_label(item['last_updated'])
            item['progress_pct'] = 0
            if item['duration_ms'] and item['position_ms']:
                item['progress_pct'] = min(100, round(item['position_ms'] * 100.0 / item['duration_ms']))
            if item['tmdb_id'] and item['tmdb_id'] <= MAX_TMDB_ID:
                is_tv = bool(item['is_tv'])
                try:
                    item['detail_url'] = reverse(
                        'series_detail_by_id' if is_tv else 'movie_detail_by_id',
                        args=[item['tmdb_id']])
                    params = {'tmdb_id': item['tmdb_id'], 'type': 'tv' if is_tv else 'movie'}
                    if is_tv:
                        params['season'] = item['season'] or 1
                        params['episode'] = item['episode'] or 1
                    item['play_url'] = reverse('videasy_player') + '?' + _urlencode(params)
                except Exception:
                    logger.debug('cloud sync: could not build links for %s', item['tmdb_id'],
                                 exc_info=True)
        return items

    progress = getattr(cloud, 'playback_progress', None)
    progress = progress if isinstance(progress, dict) else {}
    ratings = getattr(cloud, 'user_ratings', None)
    ratings = ratings if isinstance(ratings, dict) else {}

    sections = [
        ('playback', 'Playback Entries', 'play-circle', '#38bdf8',
         [_cloud_item(entry, key=key) for key, entry in progress.items()]),
        ('seen', 'Seen Items', 'eye', '#a855f7',
         [_cloud_item(key=key) for key in _as_list(getattr(cloud, 'seen_keys', None))]),
        ('history', 'Watch History', 'history', '#fbbf24',
         [_cloud_item(raw=entry) for entry in _as_list(getattr(cloud, 'watch_history', None))]),
        ('favorites', 'Favorites', 'heart', '#22c55e',
         [_cloud_item(raw=entry) for entry in _as_list(getattr(cloud, 'favorites', None))]),
        ('watchlist', 'Watchlist', 'bookmark', '#ef4444',
         [_cloud_item(raw=entry) for entry in _as_list(getattr(cloud, 'watchlist_ids', None))]),
        ('ratings', 'Ratings', 'star', '#94a3b8',
         [_cloud_item(raw=value if isinstance(value, dict) else None,
                      key=key if not isinstance(value, dict) else None,
                      rating=value if not isinstance(value, dict) else None)
          for key, value in ratings.items()]),
    ]

    rendered = []
    for key, label, icon, color, raw_items in sections:
        raw_items.sort(key=lambda i: _safe_int(i['last_updated']), reverse=True)
        # `count` stays the true total for the badge; `items` is only what the
        # page renders, and the template says "showing the first N of COUNT".
        rendered.append({
            'key': key, 'label': label, 'icon': icon, 'color': color,
            'items': _finish(raw_items[:CLOUD_ITEMS_PER_SECTION]),
            'count': len(raw_items),
        })
    return rendered


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_user_detail(request, user_id):
    """Show full details for a single synced user."""
    user_obj = get_object_or_404(SyncedUser, id=user_id)
    # Never dereference the .user relation blindly — sync-created rows can
    # hold a stale FK to a deleted Django user (User.DoesNotExist crash).
    linked_user = None
    if user_obj.user_id:
        linked_user = User.objects.filter(pk=user_obj.user_id).first()
    if not linked_user and user_obj.email:
        # Re-link by the same email when the FK is stale/missing.
        linked_user = User.objects.filter(email=user_obj.email).first()
        if linked_user and user_obj.user_id != linked_user.pk:
            user_obj.user = linked_user
            user_obj.save(update_fields=['user'])
    cloud_data = None
    if linked_user:
        cloud_data, _ = UserCloudData.objects.get_or_create(user=linked_user)
    heartbeat = UserHeartbeat.objects.filter(email__iexact=user_obj.email).first()

    # The Playback Progress table below the boxes shows the same data again, so
    # it gets the same bound instead of printing every stored entry.
    progress_map = getattr(cloud_data, 'playback_progress', None)
    progress_map = progress_map if isinstance(progress_map, dict) else {}
    progress_rows = sorted(
        progress_map.items(),
        key=lambda kv: _safe_int((kv[1] or {}).get('lastUpdated')
                                 if isinstance(kv[1], dict) else 0),
        reverse=True)

    return render(request, 'core/synced_user_detail.html', {
        'user_obj': user_obj,
        'linked_user': linked_user,
        'cloud_data': cloud_data,
        'heartbeat': heartbeat,
        # Clickable item lists behind each Cloud Sync box (details + play links).
        'cloud_sections': _cloud_sync_sections(cloud_data, linked_user),
        'max_cloud_items': MAX_CLOUD_ITEMS,
        'cloud_items_per_section': CLOUD_ITEMS_PER_SECTION,
        'progress_preview': progress_rows[:CLOUD_ITEMS_PER_SECTION],
        'progress_total': len(progress_rows),
    })


def _admin_resend_verification(django_user, request):
    """Admin resend of the verification email — always the same OTP-only
    content (6-digit code, no site link) used by registration/resend.
    Returns (ok, message)."""
    if not django_user:
        return False, 'No linked web account found for this email.'
    if not django_user.has_usable_password():
        return False, 'This account has no password yet (Android-only). Verification is only needed after registration sets a password.'
    if django_user.is_active:
        return True, 'This email is already verified and active — no verification needed.'
    _issue_verification_email(django_user, request, display_name=django_user.first_name or django_user.username)
    return True, f'Verification email with code sent to {django_user.email}.'


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def synced_user_resend_verification(request, user_id):
    """Admin: resend the verification code for a synced (Android) user."""
    user_obj = get_object_or_404(SyncedUser, id=user_id)
    linked_user = User.objects.filter(pk=user_obj.user_id).first() if user_obj.user_id else None
    if not linked_user and user_obj.email:
        linked_user = User.objects.filter(email=user_obj.email).first()
        if linked_user and user_obj.user_id != linked_user.pk:
            user_obj.user = linked_user
            user_obj.save(update_fields=['user'])
    ok, msg = _admin_resend_verification(linked_user, request)
    # Inline "Resend code" on the full-screen user list posts format=json so
    # the row can show the result in place instead of bouncing to the detail page.
    if request.POST.get('format') == 'json':
        return JsonResponse({'success': ok, 'message': msg})
    if ok:
        messages.success(request, msg)
    else:
        messages.error(request, msg)
    return redirect('synced_user_detail', user_id=user_obj.id)


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def synced_user_block(request, user_id):
    """Admin: set / lift a user's access block.

    POST block_mode = '' | 'full' | 'play' | 'app'
      ''     — restore normal access
      'full' — stop access (every API call 403)
      'play' — play block (movies/series won't play)
      'app'  — app block (no data, app shows connection error)
    """
    user_obj = get_object_or_404(SyncedUser, id=user_id)
    mode = (request.POST.get('block_mode') or '').strip().lower()
    if mode not in ('', 'full', 'play', 'app'):
        if request.POST.get('format') == 'json':
            return JsonResponse({'success': False, 'error': 'Invalid block mode.'}, status=400)
        messages.error(request, 'Invalid block mode.')
        return redirect('synced_user_detail', user_id=user_obj.id)
    user_obj.block_mode = mode
    user_obj.blocked_reason = (request.POST.get('reason') or '').strip()[:255] if mode else ''
    user_obj.blocked_at = timezone.now() if mode else None
    user_obj.save(update_fields=['block_mode', 'blocked_reason', 'blocked_at'])
    label = {'': 'Access restored', 'full': 'Full block applied',
             'play': 'Play block applied', 'app': 'App block applied'}[mode]
    # Inline block buttons on the analytics page post format=json and update
    # the row in place instead of navigating away to the detail page.
    if request.POST.get('format') == 'json':
        return JsonResponse({
            'success': True,
            'block_mode': mode,
            'label': label,
            'display': user_obj.get_block_mode_display() if mode else '',
            'reason': user_obj.blocked_reason,
            'blocked_at': timezone.localtime(user_obj.blocked_at).isoformat() if user_obj.blocked_at else None,
        })
    messages.success(request, f'{label} for {user_obj.email}.')
    return redirect('synced_user_detail', user_id=user_obj.id)


def _wipe_user_data(user_obj=None, email=''):
    """Erase one user's history/cloud data while KEEPING the accounts.

    Driven by the "Delete data" button on the Android analytics page — the
    confirmation asks first, then this clears watch history, playback
    progress/resume positions, ratings, favorites, watchlist, cloud downloads
    and playlists, the web play history, daily activity counters and visitor
    records.

    Deliberately untouched: the Django login, the Android synced profile,
    subscriptions, app preferences and the deletion tombstones. The
    ``deleted_*`` records are kept on purpose — they are what tells every
    signed-in device to purge its own copy on the next sync, otherwise the
    phone would simply re-upload everything it still holds.
    """
    from .models import (PlayHistory, UserActivity, UserCloudData,
                         WebsiteVisitor, WebsiteVisitorVisit, WatchList)

    email = (email or '').strip().lower()
    if not email and user_obj is not None:
        email = (getattr(user_obj, 'email', '') or '').strip().lower()

    django_user = None
    if user_obj is not None and getattr(user_obj, 'user_id', None):
        django_user = User.objects.filter(pk=user_obj.user_id).first()
    if django_user is None and email:
        django_user = User.objects.filter(email__iexact=email).first()

    counts = {'cloud_fields': 0, 'play_history': 0, 'watchlist': 0,
              'activity': 0, 'visitors': 0, 'visits': 0}
    if django_user is None:
        # Android-only profile with no linked account: nothing beyond the
        # synced row itself exists to clear.
        return counts

    cloud = UserCloudData.objects.filter(user=django_user).first()
    if cloud:
        changed = []
        for field in cloud._meta.fields:
            if field.primary_key or field.name in ('app_settings', 'data_version'):
                continue
            if field.name.startswith('deleted_'):
                continue
            current = getattr(cloud, field.name)
            if isinstance(current, dict):
                setattr(cloud, field.name, {})
                changed.append(field.name)
            elif isinstance(current, list):
                setattr(cloud, field.name, [])
                changed.append(field.name)
        if changed:
            cloud.data_version += 1
            cloud.save(update_fields=changed + ['data_version'])
        counts['cloud_fields'] = len(changed)

    counts['play_history'] = PlayHistory.objects.filter(user=django_user).delete()[0]
    counts['watchlist'] = WatchList.objects.filter(user=django_user).delete()[0]
    counts['activity'] = UserActivity.objects.filter(user=django_user).delete()[0]
    # Deliberately no visitor__user join — it would span two database aliases.
    visitor_ids = list(
        WebsiteVisitor.objects.filter(user=django_user).values_list('pk', flat=True)
    )
    if visitor_ids:
        counts['visits'] = WebsiteVisitorVisit.objects.filter(
            visitor_id__in=visitor_ids).delete()[0]
        counts['visitors'] = WebsiteVisitor.objects.filter(
            pk__in=visitor_ids).delete()[0]
    return counts


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def synced_user_wipe_data(request, user_id):
    """Admin: delete one user's data/history — the accounts stay.

    The "Delete data" button in the Users & Blocking table asks for
    confirmation first, then POSTs here with format=json so the row updates
    without a page reload.
    """
    user_obj = get_object_or_404(SyncedUser, id=user_id)
    email = (user_obj.email or '').strip()
    counts = _wipe_user_data(user_obj, email=email)
    total = sum(counts.values())
    from .audit import log_action
    log_action(request, 'wipe_data', email,
               'deleted %d record(s)/field(s): %s' % (total, counts))
    if request.POST.get('format') == 'json':
        return JsonResponse({
            'success': True,
            'email': email,
            'deleted': counts,
            'total': total,
            'label': f'Data deleted for {email} ({total} records/fields)',
        })
    messages.success(request, f'Data deleted for {email}: {counts}. Accounts kept.')
    return redirect('synced_user_detail', user_id=user_obj.id)


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_user_edit(request, user_id):
    """Admin: edit a synced user's subscription."""
    user_obj = get_object_or_404(SyncedUser, id=user_id)
    if request.method == 'POST':
        was_subscribed = user_obj.is_subscribed
        was_plan = user_obj.plan
        was_valid_until = user_obj.valid_until
        user_obj.is_subscribed = request.POST.get('is_subscribed') == 'on'
        user_obj.plan = request.POST.get('plan', '')
        valid_until = request.POST.get('valid_until', '')
        if valid_until:
            import datetime
            user_obj.valid_until = datetime.date.fromisoformat(valid_until)
        else:
            user_obj.valid_until = None
        features_raw = request.POST.get('features', '')
        user_obj.features = [f.strip() for f in features_raw.split('\n') if f.strip()]
        before = {'is_subscribed': was_subscribed, 'plan': was_plan,
                  'valid_until': was_valid_until}
        user_obj.save()
        _log_subscription_event(user_obj, request.user.get_username(), 'edit', before)
        return redirect('synced_users_list')
    from .models import SubscriptionEvent
    events = list(SubscriptionEvent.objects.filter(profile_id=user_obj.pk)[:20])
    return render(request, 'core/synced_user_edit.html',
                  {'synced_user': user_obj, 'subscription_events': events})


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def synced_user_subscription_action(request, user_id):
    """Admin: extend or revoke a subscription without leaving the device list.

    The full editor is still there for plan names and end dates; this is the
    common case (+7 / +30 / +90 days, or revoke) done in one request. Every
    change writes a ``SubscriptionEvent`` so the record survives the overwrite.
    """
    profile = get_object_or_404(SyncedUser, id=user_id)
    action = (request.POST.get('action') or '').strip().lower()
    today = timezone.localdate()
    before = {'is_subscribed': profile.is_subscribed, 'plan': profile.plan,
              'valid_until': profile.valid_until}
    label = ''

    if action in ('7', '30', '90'):
        days = int(action)
        start = profile.valid_until if (profile.valid_until and profile.valid_until > today) else today
        profile.valid_until = start + datetime.timedelta(days=days)
        profile.is_subscribed = True
        if not profile.plan:
            profile.plan = 'VIP'
        label = 'Extended by %d days — now until %s' % (days, profile.valid_until.isoformat())
    elif action == 'revoke':
        profile.is_subscribed = False
        profile.plan = ''
        profile.valid_until = None
        profile.features = []
        label = 'Subscription revoked'
    else:
        return JsonResponse({'ok': False, 'message': 'Unknown action'}, status=400)

    profile.save()
    _log_subscription_event(profile, request.user.get_username(), action, before)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'message': label,
                             'is_subscribed': profile.is_subscribed,
                             'plan': profile.plan or '',
                             'valid_until': profile.valid_until.isoformat() if profile.valid_until else ''})
    messages.success(request, label)
    nxt = request.POST.get('next') or ''
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect('synced_users_list')


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_user_delete(request, user_id):
    """Admin: permanently delete a synced user and ALL linked data by email."""
    user_obj = get_object_or_404(SyncedUser, id=user_id)
    if request.method == 'POST':
        email = _permanently_delete_user(user_obj)
        messages.success(request, f'User {email} and all linked data (profile, sessions, cloud data, watchlist, play history) permanently deleted.')
    return redirect('synced_users_list')


@login_required
@user_passes_test(is_staff_or_superuser)
def user_dashboard(request):
    """Beautiful dashboard showing all synced users with stats."""
    users = SyncedUser.objects.all().order_by('-last_synced_at')
    total = users.count()
    subscribed = users.filter(is_subscribed=True).count()
    free = total - subscribed

    # Device breakdown
    device_stats = (
        users.values('device_model')
        .annotate(count=models.Count('id'))
        .order_by('-count')[:10]
    )
    # OS version breakdown
    os_stats = (
        users.values('os_version')
        .annotate(count=models.Count('id'))
        .order_by('-count')[:10]
    )
    # App version breakdown
    version_stats = (
        users.values('app_version')
        .annotate(count=models.Count('id'))
        .order_by('-count')[:10]
    )
    # Plan breakdown
    plan_stats = (
        users.filter(is_subscribed=True)
        .values('plan')
        .annotate(count=models.Count('id'))
        .order_by('-count')[:10]
    )

    return render(request, 'core/user_dashboard.html', {
        'users': users,
        'total': total,
        'subscribed': subscribed,
        'free': free,
        'device_stats': device_stats,
        'os_stats': os_stats,
        'version_stats': version_stats,
        'plan_stats': plan_stats,
    })


from .models import PlayHistory


@require_http_methods(["POST"])
def ajax_record_play_progress(request):
    """Record play progress for a logged-in user."""
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Not authenticated'})

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'message': 'Invalid JSON'})

    tmdb_id = data.get('tmdb_id')
    media_type = data.get('media_type', 'movie')
    title = data.get('title', '')
    poster_path = data.get('poster_path', '')
    season_number = data.get('season_number')
    episode_number = data.get('episode_number')
    episode_title = data.get('episode_title', '')
    duration_seconds = int(data.get('duration_seconds', 0))
    total_duration_seconds = int(data.get('total_duration_seconds', 0))
    completed = data.get('completed', False)

    if not tmdb_id:
        return JsonResponse({'success': False, 'message': 'tmdb_id required'})

    history, created = PlayHistory.update_or_create_unique(
        defaults={
            'title': title,
            'poster_path': poster_path,
            'episode_title': episode_title,
            'duration_seconds': duration_seconds,
            'total_duration_seconds': total_duration_seconds,
            'completed': completed,
            # Field is not auto_now — record the real play moment.
            'last_played_at': timezone.now(),
        },
        user=request.user,
        tmdb_id=tmdb_id,
        media_type=media_type,
        season_number=season_number,
        episode_number=episode_number,
    )
    # Also sync to UserCloudData for Android app
    from .models import UserCloudData
    cloud, _ = UserCloudData.objects.get_or_create(user=request.user)
    is_tv = media_type == 'tv'
    s = season_number if season_number is not None else -1
    e = episode_number if episode_number is not None else -1
    key = f"{'tv' if is_tv else 'movie'}_{tmdb_id}_{s}_{e}"
    cloud.playback_progress[key] = {
        'mediaId': tmdb_id,
        'isTv': is_tv,
        'season': s,
        'episode': e,
        'positionMs': duration_seconds * 1000,
        'durationMs': total_duration_seconds * 1000,
        'title': title,
        'posterPath': poster_path or '',
        'episodeTitle': episode_title,
        'lastUpdated': int(timezone.now().timestamp() * 1000),
    }
    # Also add to watch history in cloud
    history_entry = {
        'mediaId': tmdb_id,
        'isTv': is_tv,
        'title': title,
        'posterPath': poster_path or '',
        'season': s,
        'episode': e,
        'lastUpdated': int(timezone.now().timestamp() * 1000),
    }
    existing_ids = {h.get('mediaId') for h in (cloud.watch_history or [])}
    if tmdb_id not in existing_ids:
        cloud.watch_history = [history_entry] + (cloud.watch_history or [])
    else:
        # Update existing entry position
        for h in (cloud.watch_history or []):
            if h.get('mediaId') == tmdb_id:
                h['lastUpdated'] = history_entry['lastUpdated']
                break
    cloud.data_version += 1
    cloud.save()
    return JsonResponse({'success': True, 'created': created})


@login_required
def user_profile(request):
    """User profile edit page."""
    from .models import UserProfile
    profile, created = UserProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        # Basic info
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        bio = request.POST.get('bio', '').strip()
        dob = request.POST.get('date_of_birth', '').strip()
        location = request.POST.get('location', '').strip()
        website = request.POST.get('website', '').strip()
        gender = request.POST.get('gender', '').strip()
        language = request.POST.get('language', 'en').strip()

        # Validate email uniqueness
        if email and email != request.user.email:
            if User.objects.filter(email=email).exclude(pk=request.user.pk).exists():
                messages.error(request, 'This email is already in use.')
                return redirect('user_profile')

        # Update user fields
        request.user.first_name = first_name
        request.user.last_name = last_name
        if email:
            request.user.email = email
        request.user.save(update_fields=['first_name', 'last_name', 'email'])

        # Update profile fields
        profile.phone = phone
        profile.bio = bio
        profile.location = location
        profile.website = website
        profile.gender = gender
        profile.language = language
        if dob:
            try:
                from datetime import date as _date
                profile.date_of_birth = _date.fromisoformat(dob)
            except (ValueError, TypeError):
                pass

        # Handle photo upload
        if 'photo' in request.FILES:
            photo_file = request.FILES['photo']
            # Validate file size (max 5MB)
            if photo_file.size > 5 * 1024 * 1024:
                messages.error(request, 'Photo must be under 5MB.')
                return redirect('user_profile')
            profile.photo = photo_file

        # Handle photo removal
        if request.POST.get('remove_photo') == '1':
            if profile.photo:
                profile.photo.delete(save=False)
                profile.photo = None

        profile.save()
        messages.success(request, 'Profile updated successfully.')
        return redirect('user_profile')

    return render(request, 'core/user_profile.html', {'profile': profile})


@login_required
def user_play_history(request):
    """Show the user's play history."""
    history = PlayHistory.objects.filter(user=request.user).order_by('-last_played_at')
    return render(request, 'core/user_play_history.html', {'history': history})


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_play_history(request, user_id):
    """Admin view of a specific user's play history (same look as the user's page)."""
    detail_user = get_object_or_404(User, id=user_id)
    history = PlayHistory.objects.filter(user=detail_user).order_by('-last_played_at')
    return render(request, 'core/user_play_history.html', {
        'history': history,
        'admin_viewing_user': detail_user,
    })


@require_http_methods(["GET"])
def ajax_play_progress(request):
    """Return play progress for a list of tmdb_ids (for poster progress bars)."""
    if not request.user.is_authenticated:
        return JsonResponse({'success': True, 'items': {}})
    ids_raw = request.GET.get('ids', '')
    if not ids_raw:
        return JsonResponse({'success': True, 'items': {}})
    try:
        tmdb_ids = [int(x.strip()) for x in ids_raw.split(',') if x.strip()]
    except ValueError:
        return JsonResponse({'success': True, 'items': {}})
    history = PlayHistory.objects.filter(
        user=request.user, tmdb_id__in=tmdb_ids
    ).order_by('tmdb_id', '-last_played_at')
    items = {}
    for h in history:
        key = str(h.tmdb_id)
        if key not in items:
            items[key] = {
                'tmdb_id': h.tmdb_id,
                'progress': h.progress_percent,
                'completed': h.completed,
                'duration': h.duration_seconds,
                'total': h.total_duration_seconds,
                'season': h.season_number,
                'episode': h.episode_number,
                'title': h.title,
            }
    return JsonResponse({'success': True, 'items': items})


@require_http_methods(["GET"])
def ajax_resume_position(request):
    """Return the last playback position for resume."""
    if not request.user.is_authenticated:
        return JsonResponse({'success': True, 'position': 0})
    tmdb_id = request.GET.get('tmdb_id', '')
    media_type = request.GET.get('media_type', 'movie')
    season = request.GET.get('season')
    episode = request.GET.get('episode')
    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        return JsonResponse({'success': True, 'position': 0})
    kwargs = {'user': request.user, 'tmdb_id': tmdb_id, 'media_type': media_type}
    if season:
        kwargs['season_number'] = int(season)
    if episode:
        kwargs['episode_number'] = int(episode)
    last = PlayHistory.objects.filter(**kwargs).order_by('-last_played_at').first()
    # Also check UserCloudData (Android-synced resume)
    cloud_pos = 0
    cloud_title = ''
    cloud_progress = 0
    from .models import UserCloudData
    cloud = UserCloudData.objects.filter(user=request.user).first()
    if cloud:
        is_tv = media_type == 'tv'
        s = int(season) if season else -1
        e = int(episode) if episode else -1
        key = f"{'tv' if is_tv else 'movie'}_{tmdb_id}_{s}_{e}"
        entry = (cloud.playback_progress or {}).get(key)
        if entry:
            cloud_pos = entry.get('positionMs', 0) // 1000
            dur = entry.get('durationMs', 0) // 1000
            cloud_title = entry.get('title', '')
            cloud_progress = round((cloud_pos / dur) * 100) if dur > 0 else 0
    # Use whichever has the higher position
    web_pos = last.duration_seconds if last and not last.completed and last.duration_seconds > 10 else 0
    if cloud_pos > web_pos:
        return JsonResponse({
            'success': True, 'position': cloud_pos,
            'progress': cloud_progress, 'title': cloud_title,
        })
    if web_pos > 0:
        return JsonResponse({
            'success': True, 'position': web_pos,
            'progress': last.progress_percent, 'title': last.title,
        })
    return JsonResponse({'success': True, 'position': 0})


# ---------------------------------------------------------------------------
# Page View & Time Tracking AJAX
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(["POST"])
def ajax_track_page_view(request):
    """Receive page view heartbeats from the browser."""
    import json
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    action = data.get('action', 'view')  # 'view', 'heartbeat', 'end'
    path = data.get('path', request.META.get('HTTP_REFERER', '/'))[:500]
    title = data.get('title', '')[:255]
    time_spent = int(data.get('time_spent', 0))
    scroll_depth = float(data.get('scroll_depth', 0))
    visitor_id = data.get('visitor_id', '')[:100]

    # Detect platform from User-Agent
    ua = request.META.get('HTTP_USER_AGENT', '')
    if 'Android' in ua or 'X-Android-App' in request.META.get('HTTP_X_ANDROID_APP', ''):
        platform = 'android'
    elif 'iPhone' in ua or 'iPad' in ua:
        platform = 'ios'
    else:
        platform = 'web'

    # Override platform if sent by client
    if data.get('platform'):
        platform = data['platform'][:20]

    client_ip = get_client_ip(request)
    user_obj = request.user if request.user.is_authenticated else None

    try:
        from .models import UserPageView

        if action == 'view':
            # Create new page view record
            pv = UserPageView.objects.create(
                user=user_obj,
                visitor_id=visitor_id,
                path=path,
                page_title=title,
                platform=platform,
                ip_address=client_ip,
                user_agent=ua[:500],
                referrer=data.get('referrer', '')[:500],
                scroll_depth=scroll_depth,
                is_active=True,
            )
            return JsonResponse({'status': 'ok', 'view_id': pv.id})

        elif action == 'heartbeat':
            # Update existing page view with latest time
            view_id = data.get('view_id')
            if view_id:
                updated = UserPageView.objects.filter(id=view_id, is_active=True).update(
                    time_spent_seconds=time_spent,
                    scroll_depth=scroll_depth,
                )
                if updated:
                    return JsonResponse({'status': 'ok'})
            # Fallback: find most recent active view for this path
            pv = UserPageView.objects.filter(
                user=user_obj, path=path, is_active=True
            ).order_by('-viewed_at').first()
            if pv:
                pv.time_spent_seconds = time_spent
                pv.scroll_depth = scroll_depth
                pv.save(update_fields=['time_spent_seconds', 'scroll_depth'])
                return JsonResponse({'status': 'ok', 'view_id': pv.id})
            return JsonResponse({'status': 'no_active_view'})

        elif action == 'end':
            view_id = data.get('view_id')
            if view_id:
                UserPageView.objects.filter(id=view_id).update(
                    time_spent_seconds=time_spent,
                    scroll_depth=scroll_depth,
                    is_active=False,
                    ended_at=timezone.now(),
                )
            else:
                UserPageView.objects.filter(
                    user=user_obj, path=path, is_active=True
                ).update(
                    time_spent_seconds=time_spent,
                    scroll_depth=scroll_depth,
                    is_active=False,
                    ended_at=timezone.now(),
                )
            return JsonResponse({'status': 'ok'})

    except Exception as e:
        logger.error('Track page view error: %s', e)
        return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'status': 'ignored'})


# ---------------------------------------------------------------------------
# Admin Analytics Dashboard
# ---------------------------------------------------------------------------

@login_required
@user_passes_test(is_staff_or_superuser)
def admin_analytics(request):
    """Admin page showing user analytics: page views, time spent, platforms."""
    from .models import UserPageView, UserSession, WebsiteVisitor
    from datetime import datetime, time as dtime
    from django.utils.dateparse import parse_date
    from django.db.models import Sum, Count, Avg, F, Q
    from django.utils import timezone

    # A preset (?days=7) or an explicit window (?start=YYYY-MM-DD&end=...).
    # The window is capped at a year so a typo cannot scan years of rows, and
    # the previous period of the same length is measured for the deltas.
    raw_start = (request.GET.get('start') or '').strip()
    raw_end = (request.GET.get('end') or '').strip()
    start_date, end_date = parse_date(raw_start), parse_date(raw_end)
    custom_range = bool(start_date and end_date)
    if custom_range:
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        if (end_date - start_date).days > 364:
            start_date = end_date - timezone.timedelta(days=364)
        tz = timezone.get_current_timezone()
        since = timezone.make_aware(datetime.combine(start_date, dtime.min), tz)
        until = timezone.make_aware(
            datetime.combine(end_date + timezone.timedelta(days=1), dtime.min), tz)
        if until > timezone.now():
            until = timezone.now()
        days = max(1, (end_date - start_date).days + 1)
    else:
        try:
            days = max(1, min(365, int(request.GET.get('days', 7))))
        except (TypeError, ValueError):
            days = 7
        since = timezone.now() - timezone.timedelta(days=days)
        until = timezone.now()

    range_label = '%s → %s' % (timezone.localtime(since).date().isoformat(),
                               timezone.localtime(until).date().isoformat())

    # Overview stats
    total_views = UserPageView.objects.filter(viewed_at__gte=since, viewed_at__lt=until).count()
    total_time = UserPageView.objects.filter(viewed_at__gte=since, viewed_at__lt=until).aggregate(
        total=Sum('time_spent_seconds'))['total'] or 0
    avg_time = UserPageView.objects.filter(viewed_at__gte=since, viewed_at__lt=until).aggregate(
        avg=Avg('time_spent_seconds'))['avg'] or 0
    unique_visitors = UserPageView.objects.filter(viewed_at__gte=since, viewed_at__lt=until).values(
        'visitor_id').distinct().count()
    unique_users = UserPageView.objects.filter(
        viewed_at__gte=since, viewed_at__lt=until, user__isnull=False).values('user').distinct().count()

    # Platform breakdown
    platform_data = list(UserPageView.objects.filter(viewed_at__gte=since, viewed_at__lt=until)
        .values('platform')
        .annotate(views=Count('id'), total_time=Sum('time_spent_seconds'))
        .order_by('-views'))

    # Top pages
    top_pages = list(UserPageView.objects.filter(viewed_at__gte=since, viewed_at__lt=until)
        .values('path', 'page_title')
        .annotate(views=Count('id'), total_time=Sum('time_spent_seconds'),
                  avg_time=Avg('time_spent_seconds'))
        .order_by('-views')[:20])

    # Most active users
    active_users = list(UserPageView.objects.filter(
        viewed_at__gte=since, viewed_at__lt=until, user__isnull=False)
        .values('user__username', 'user__email')
        .annotate(views=Count('id'), total_time=Sum('time_spent_seconds'))
        .order_by('-views')[:15])

    # Active sessions
    active_sessions = UserSession.objects.filter(is_active=True).select_related('user').order_by('-last_seen_at')[:20]

    # Daily views chart data (last 30 days)
    chart_days = min(days * 2, 30)
    daily_views = []
    for i in range(chart_days):
        d = timezone.localdate() - timezone.timedelta(days=i)
        count = UserPageView.objects.filter(viewed_at__date=d).count()
        daily_views.append({'date': d.isoformat(), 'views': count})
    daily_views.reverse()
    # The template draws each bar as a share of the busiest day, otherwise the
    # bar height would be the raw view count in pixels and burst out of the card.
    max_daily_views = max((d['views'] for d in daily_views), default=0)

    # Hourly heatmap data (today)
    hourly_views = list(UserPageView.objects.filter(
        viewed_at__date=timezone.localdate())
        .extra(select={'hour': 'strftime("%%H", viewed_at)'})
        .values('hour')
        .annotate(views=Count('id'))
        .order_by('hour'))

    # ---- previous period ---------------------------------------------------
    span = until - since
    prev_since, prev_until = since - span, since
    previous = UserPageView.objects.filter(viewed_at__gte=prev_since, viewed_at__lt=prev_until)
    prev_views = previous.count()
    prev_visitors = previous.values('visitor_id').distinct().count()
    prev_avg_time = previous.aggregate(avg=Avg('time_spent_seconds'))['avg'] or 0

    def _delta(now_value, before_value):
        if not before_value:
            return None
        return round(100.0 * (now_value - before_value) / before_value, 1)

    return render(request, 'core/admin_analytics.html', {
        'days': days,
        'custom_range': custom_range,
        'range_start': timezone.localtime(since).date().isoformat(),
        'range_end': timezone.localtime(until - timezone.timedelta(seconds=1)).date().isoformat(),
        'range_label': range_label,
        'prev_views': prev_views,
        'prev_visitors': prev_visitors,
        'prev_avg_time': prev_avg_time,
        'delta_views': _delta(total_views, prev_views),
        'delta_visitors': _delta(unique_visitors, prev_visitors),
        'delta_avg_time': _delta(avg_time, prev_avg_time),
        'prev_label': '%s → %s' % (timezone.localtime(prev_since).date().isoformat(),
                                   timezone.localtime(prev_until).date().isoformat()),
        'total_views': total_views,
        'total_time': total_time,
        'avg_time': avg_time,
        'unique_visitors': unique_visitors,
        'unique_users': unique_users,
        'platform_data': platform_data,
        'top_pages': top_pages,
        'active_users': active_users,
        'active_sessions': active_sessions,
        'daily_views': daily_views,
        'max_daily_views': max_daily_views,
        'daily_days': len(daily_views),
        'hourly_views': hourly_views,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_engagement_analytics(request):
    """User engagement analytics with Chart.js — daily active users, peak hours,
    most watched content, retention metrics.
    Supports platform (web/android) and registration (registered/unregistered) filters."""
    from .models import UserPageView, UserSession, PlayHistory, WebsiteVisitor, SyncedUser
    from django.db.models import Sum, Count, Avg, Q, F
    from django.contrib.auth.models import User
    from django.utils import timezone
    from django.db import connection

    days = int(request.GET.get('days', 30))
    platform_filter = request.GET.get('platform', 'all')  # all, web, android
    reg_filter = request.GET.get('reg', 'all')  # all, registered, unregistered
    since = timezone.now() - timezone.timedelta(days=days)
    today = timezone.localdate()

    # Determine DB alias
    user_db = 'default'
    try:
        from .models import DBRoutingConfig
        cfg = DBRoutingConfig.get_config()
        if cfg.use_external_db and cfg.external_db_ready:
            user_db = 'external'
    except Exception:
        pass

    def _ph_qs(**extra):
        qs = PlayHistory.objects.all()
        for db in ([user_db, 'default'] if user_db != 'default' else ['default']):
            try:
                qs = qs.using(db)
                break
            except Exception:
                continue
        return qs.filter(**extra)

    # Platform mapping: PlayHistory has no platform field, but UserPageView does.
    # For PlayHistory we infer from user: SyncedUser rows = android, others = web.
    registered_user_ids = set(User.objects.values_list('id', flat=True))
    android_user_ids = set()
    try:
        android_user_ids = set(
            SyncedUser.objects.using(user_db).filter(user__isnull=False)
            .values_list('user_id', flat=True)
        )
    except Exception:
        pass

    def _apply_ph_filters(qs):
        """Apply platform + registration filters to a PlayHistory queryset."""
        if platform_filter == 'android':
            qs = qs.filter(user_id__in=android_user_ids)
        elif platform_filter == 'web':
            qs = qs.exclude(user_id__in=android_user_ids)
        if reg_filter == 'registered':
            qs = qs.filter(user_id__in=registered_user_ids)
        elif reg_filter == 'unregistered':
            qs = qs.exclude(user_id__in=registered_user_ids)
        return qs

    def _apply_pv_filters(qs):
        """Apply platform + registration filters to a UserPageView queryset."""
        if platform_filter == 'android':
            qs = qs.filter(platform='android')
        elif platform_filter == 'web':
            qs = qs.filter(platform='web')
        if reg_filter == 'registered':
            qs = qs.filter(user__isnull=False)
        elif reg_filter == 'unregistered':
            qs = qs.filter(user__isnull=True)
        return qs

    # --- 1. Daily Active Users (DAU) chart ---
    dau_data = []
    for i in range(min(days, 60)):
        d = timezone.localdate() - timezone.timedelta(days=i)
        qs = UserPageView.objects.filter(viewed_at__date=d)
        qs = _apply_pv_filters(qs)
        dau = qs.values('user').distinct().count() if reg_filter != 'unregistered' else qs.count()
        dau_data.append({'date': d.isoformat(), 'dau': dau})
    dau_data.reverse()

    # --- 2. Peak Viewing Hours (play history by hour) ---
    hourly_plays = [0] * 24
    try:
        play_qs = _apply_ph_filters(_ph_qs(last_played_at__gte=since))
        for row in play_qs.extra(select={'hour': 'strftime("%%H", last_played_at)'}).values('hour').annotate(cnt=Count('id')):
            h = int(row['hour'])
            if 0 <= h < 24:
                hourly_plays[h] = row['cnt']
    except Exception:
        pass

    # --- 3. Most Watched Content ---
    most_watched = []
    try:
        play_qs = _apply_ph_filters(_ph_qs(last_played_at__gte=since))
        most_watched = list(
            play_qs.exclude(tmdb_id__lte=0)
                   .values('tmdb_id', 'title', 'media_type', 'poster_path')
                   .annotate(watches=Count('id'), unique_users=Count('user', distinct=True))
                   .order_by('-watches')[:15]
        )
    except Exception:
        pass

    # --- 4. Content Type Distribution (movie vs tv) ---
    content_types = []
    try:
        play_qs = _apply_ph_filters(_ph_qs(last_played_at__gte=since))
        content_types = list(
            play_qs.values('media_type')
                   .annotate(count=Count('id'))
                   .order_by('-count')
        )
    except Exception:
        pass

    # --- 5. Platform breakdown (Android vs Web) ---
    platform_breakdown = []
    try:
        play_qs = _ph_qs(last_played_at__gte=since)
        android_count = play_qs.filter(user_id__in=android_user_ids).count()
        web_count = play_qs.exclude(user_id__in=android_user_ids).count()
        platform_breakdown = [
            {'platform': 'Android', 'count': android_count},
            {'platform': 'Web', 'count': web_count},
        ]
    except Exception:
        pass

    # --- 6. Registration breakdown (Registered vs Unregistered) ---
    reg_breakdown = []
    try:
        play_qs = _ph_qs(last_played_at__gte=since)
        reg_count = play_qs.filter(user_id__in=registered_user_ids).count()
        unreg_count = play_qs.exclude(user_id__in=registered_user_ids).count()
        reg_breakdown = [
            {'type': 'Registered', 'count': reg_count},
            {'type': 'Unregistered', 'count': unreg_count},
        ]
    except Exception:
        pass

    # --- 7. Retention: users who returned after N days ---
    retention_data = []
    for back in [1, 3, 7, 14, 30]:
        day_a = today - timezone.timedelta(days=back)
        day_b = today - timezone.timedelta(days=back - 1)
        try:
            pv_a = UserPageView.objects.filter(viewed_at__date=day_a)
            pv_b = UserPageView.objects.filter(viewed_at__date=day_b)
            if reg_filter == 'registered':
                pv_a = pv_a.filter(user__isnull=False)
                pv_b = pv_b.filter(user__isnull=False)
            elif reg_filter == 'unregistered':
                pv_a = pv_a.filter(user__isnull=True)
                pv_b = pv_b.filter(user__isnull=True)
            if platform_filter == 'android':
                pv_a = pv_a.filter(platform='android')
                pv_b = pv_b.filter(platform='android')
            elif platform_filter == 'web':
                pv_a = pv_a.filter(platform='web')
                pv_b = pv_b.filter(platform='web')
            day_a_users = set(pv_a.values_list('user', flat=True).distinct())
            day_b_users = set(pv_b.values_list('user', flat=True).distinct())
            returned = len(day_a_users & day_b_users)
            rate = (returned / len(day_a_users) * 100) if day_a_users else 0
            retention_data.append({
                'label': f'{back}d→{back-1}d',
                'cohort_size': len(day_a_users),
                'returned': returned,
                'rate': round(rate, 1),
            })
        except Exception:
            retention_data.append({'label': f'{back}d→{back-1}d', 'cohort_size': 0, 'returned': 0, 'rate': 0})

    # --- 8. Daily plays chart ---
    daily_plays = []
    for i in range(min(days, 60)):
        d = timezone.localdate() - timezone.timedelta(days=i)
        try:
            qs = _apply_ph_filters(_ph_qs(last_played_at__date=d))
            cnt = qs.count()
        except Exception:
            cnt = 0
        daily_plays.append({'date': d.isoformat(), 'plays': cnt})
    daily_plays.reverse()

    # --- 9. Summary stats ---
    total_plays = 0
    unique_watching_users = 0
    avg_plays_per_user = 0
    try:
        play_qs = _apply_ph_filters(_ph_qs(last_played_at__gte=since))
        total_plays = play_qs.count()
        unique_watching_users = play_qs.values('user').distinct().count()
        avg_plays_per_user = round(total_plays / unique_watching_users, 1) if unique_watching_users else 0
    except Exception:
        pass

    # Avg session duration
    avg_session = 0
    try:
        pv_qs = UserPageView.objects.filter(viewed_at__gte=since)
        pv_qs = _apply_pv_filters(pv_qs)
        avg_session = pv_qs.aggregate(avg=Avg('time_spent_seconds'))['avg'] or 0
        avg_session = round(avg_session)
    except Exception:
        pass

    return render(request, 'core/admin_engagement_analytics.html', {
        'days': days,
        'platform_filter': platform_filter,
        'reg_filter': reg_filter,
        'total_plays': total_plays,
        'unique_watching_users': unique_watching_users,
        'avg_plays_per_user': avg_plays_per_user,
        'avg_session': avg_session,
        'dau_data': dau_data,
        'hourly_plays': hourly_plays,
        'most_watched': most_watched,
        'content_types': content_types,
        'platform_breakdown': platform_breakdown,
        'reg_breakdown': reg_breakdown,
        'retention_data': retention_data,
        'daily_plays': daily_plays,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_analytics_detail(request, user_id):
    """Detailed analytics for a single user."""
    from .models import UserPageView, UserSession
    from django.db.models import Sum, Count, Avg
    from django.utils import timezone

    target_user = get_object_or_404(User, id=user_id)
    days = int(request.GET.get('days', 30))
    since = timezone.now() - timezone.timedelta(days=days)

    # User's page views
    user_views = UserPageView.objects.filter(user=target_user, viewed_at__gte=since)
    total_views = user_views.count()
    total_time = user_views.aggregate(total=Sum('time_spent_seconds'))['total'] or 0

    # Top pages for this user
    top_pages = list(user_views
        .values('path', 'page_title')
        .annotate(views=Count('id'), total_time=Sum('time_spent_seconds'))
        .order_by('-views')[:20])

    # Platform breakdown
    platform_data = list(user_views
        .values('platform')
        .annotate(views=Count('id'), total_time=Sum('time_spent_seconds'))
        .order_by('-views'))

    # Daily activity
    daily_views = []
    for i in range(min(days, 30)):
        d = timezone.localdate() - timezone.timedelta(days=i)
        count = user_views.filter(viewed_at__date=d).count()
        daily_views.append({'date': d.isoformat(), 'views': count})
    daily_views.reverse()
    max_daily_views = max((d['views'] for d in daily_views), default=0)

    # Sessions
    sessions = UserSession.objects.filter(user=target_user).order_by('-logged_in_at')[:20]

    # Recent page views
    recent_views = user_views.select_related().order_by('-viewed_at')[:50]

    return render(request, 'core/admin_user_analytics_detail.html', {
        'target_user': target_user,
        'days': days,
        'total_views': total_views,
        'total_time': total_time,
        'top_pages': top_pages,
        'platform_data': platform_data,
        'daily_views': daily_views,
        'max_daily_views': max_daily_views,
        'sessions': sessions,
        'recent_views': recent_views,
    })


# ---------------------------------------------------------------------------
# Active / Inactive connection list (behind the analytics Web vs Android cards)
# ---------------------------------------------------------------------------

def _au_int_param(request, name, default, low, high):
    """Read a clamped integer query-string parameter."""
    try:
        value = int(request.GET.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


# Signatures of scanners, crawlers and command-line clients that hammer the
# site without ever being a real visitor. Single source of truth shared with the
# Probes & Bots page (a scanner should be classified the same in both places).
from .probes import (SCRAPER_UA_MARKERS as _AU_BOT_UA_MARKERS,
                     PROBE_PATH_MARKERS as _AU_BOT_PATH_MARKERS)


def _au_looks_like_bot(row):
    """Best-effort bot/scanner detection for an anonymous connection row.

    Logged-in identities are never treated as bots so a real user can never be
    hidden by a clever user-agent.
    """
    if row['user_id']:
        return False
    user_agent = (row['user_agent'] or '').strip().lower()
    if not user_agent:
        return True
    if any(marker in user_agent for marker in _AU_BOT_UA_MARKERS):
        return True
    path = (row['last_path'] or '').lower()
    if path and any(marker in path for marker in _AU_BOT_PATH_MARKERS):
        return True
    return False


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_active_users(request):
    """Live connection list reached from the analytics 'Web vs Android' cards.

    Aggregates every per-identity signal we have — login sessions, page views
    (with time spent and platform), anonymous website visitors, Android app
    devices and request-log engagement (clicks / time on page) — into one row
    per identity+platform. Each row is labelled Active or Inactive from how
    recently it was last seen, and the list can be filtered by platform
    (web / android / ios) and by active vs inactive status.
    """
    from .models import (UserPageView, UserSession, WebsiteVisitor,
                         AndroidAppDevice, AndroidAppDeviceVisit, RequestLog)
    from django.db.models import Sum, Count, Max
    from datetime import timedelta
    import csv

    platform = (request.GET.get('platform') or 'all').strip().lower()
    status = (request.GET.get('status') or 'all').strip().lower()
    if status not in ('all', 'active', 'inactive'):
        status = 'all'
    bots = (request.GET.get('bots') or 'hide').strip().lower()
    if bots not in ('hide', 'show', 'only'):
        bots = 'hide'
    hours = _au_int_param(request, 'hours', 24, 1, 720)
    minutes = _au_int_param(request, 'minutes', 15, 1, 1440)
    search = (request.GET.get('q') or '').strip()
    sort = (request.GET.get('sort') or 'last_seen').strip()
    direction = (request.GET.get('dir') or 'desc').strip().lower()
    if direction not in ('asc', 'desc'):
        direction = 'desc'

    now = timezone.now()
    since = now - timedelta(hours=hours)
    active_cutoff = now - timedelta(minutes=minutes)
    epoch = now - timedelta(days=3650)

    rows = {}

    def _ensure(platform_name, user_id=None, visitor_id='', ip='', device_uid=''):
        """Fetch (or create) the running row for one identity+platform pair."""
        if user_id:
            ident = 'u%s' % user_id
        elif device_uid:
            ident = 'd%s' % device_uid
        elif visitor_id:
            ident = 'v%s' % visitor_id
        elif ip:
            ident = 'i%s' % ip
        else:
            ident = 'x'
        plat = platform_name or 'web'
        key = ident + '|' + plat
        row = rows.get(key)
        if row is None:
            row = {
                'key': key, 'ident': ident, 'platform': plat,
                'user_id': user_id, 'visitor_id': visitor_id,
                'device_uid': device_uid, 'ip': '', 'ips': set(),
                'device_model': '', 'os_version': '', 'app_version': '',
                'app_name': '', 'user_agent': '', 'referrer': '',
                'last_path': '', 'last_title': '', 'views': 0, 'total_time': 0,
                'scroll': 0.0, 'clicks': 0, 'requests': 0, 'page_time_ms': 0,
                'logins': 0, 'visits': 0, 'device_visits': 0,
                'first_seen': None, 'last_seen': None, 'last_pv_at': None,
                'session_open': False, 'logged_out': False, 'on_page': False,
            }
            rows[key] = row
        return row

    # --- login sessions (web logins + android app logins) -------------------
    try:
        sessions = list(UserSession.objects.filter(
            Q(last_seen_at__gte=since) | Q(logged_in_at__gte=since)
        ).order_by('-last_seen_at')[:5000])
    except Exception as exc:
        logger.warning('active users: sessions query failed: %s', exc)
        sessions = []
    for s in sessions:
        row = _ensure(s.source, user_id=s.user_id, ip=s.ip_address or '')
        row['logins'] += 1
        if s.ip_address:
            row['ips'].add(s.ip_address)
        if s.last_seen_at and (row['last_seen'] is None or s.last_seen_at > row['last_seen']):
            row['last_seen'] = s.last_seen_at
            row['session_open'] = bool(s.is_active)
            row['logged_out'] = not s.is_active
            row['ip'] = s.ip_address or row['ip']
            row['user_agent'] = s.user_agent or row['user_agent']
            row['device_model'] = s.device_model or row['device_model']
            row['os_version'] = s.os_version or row['os_version']
            row['app_version'] = s.app_version or row['app_version']
        if s.logged_in_at and (row['first_seen'] is None or s.logged_in_at < row['first_seen']):
            row['first_seen'] = s.logged_in_at

    # --- page views (with time spent, platform, scroll depth) ---------------
    try:
        page_views = list(UserPageView.objects.filter(viewed_at__gte=since)
                          .order_by('-viewed_at')
                          .values('user_id', 'visitor_id', 'platform', 'path',
                                  'page_title', 'ip_address', 'user_agent', 'referrer',
                                  'time_spent_seconds', 'scroll_depth', 'is_active',
                                  'viewed_at')[:20000])
    except Exception as exc:
        logger.warning('active users: page view query failed: %s', exc)
        page_views = []
    for pv in page_views:
        row = _ensure(pv['platform'], user_id=pv['user_id'],
                      visitor_id=pv['visitor_id'] or '',
                      ip=pv['ip_address'] or '')
        when = pv['viewed_at']
        row['views'] += 1
        row['total_time'] += pv['time_spent_seconds'] or 0
        row['scroll'] = max(row['scroll'], pv['scroll_depth'] or 0)
        if pv['ip_address']:
            row['ips'].add(pv['ip_address'])
        # The newest page view decides the "last page" details, independently
        # of logins which may carry a fresher timestamp.
        if when and (row['last_pv_at'] is None or when > row['last_pv_at']):
            row['last_pv_at'] = when
            row['last_path'] = pv['path'] or ''
            row['last_title'] = pv['page_title'] or ''
            row['referrer'] = pv['referrer'] or row['referrer']
            if pv['ip_address']:
                row['ip'] = pv['ip_address']
            if pv['user_agent']:
                row['user_agent'] = pv['user_agent']
        if when and (row['last_seen'] is None or when > row['last_seen']):
            row['last_seen'] = when
        if when and (row['first_seen'] is None or when < row['first_seen']):
            row['first_seen'] = when
        if pv['is_active'] and when and when >= active_cutoff:
            row['on_page'] = True

    # --- anonymous website visitors (visitor_id cookie) ---------------------
    try:
        visitors = list(WebsiteVisitor.objects.filter(last_seen_at__gte=since).annotate(
            recent_visits=Count('visits', filter=Q(visits__visited_at__gte=since))
        ).order_by('-last_seen_at')[:5000])
    except Exception as exc:
        logger.warning('active users: visitor query failed: %s', exc)
        visitors = []
    for v in visitors:
        ua = v.user_agent or ''
        low_ua = ua.lower()
        if 'android' in low_ua:
            plat = 'android'
        elif 'iphone' in low_ua or 'ipad' in low_ua:
            plat = 'ios'
        else:
            plat = 'web'
        row = _ensure(plat, user_id=v.user_id, visitor_id=str(v.visitor_id),
                      ip=v.last_ip_address or '')
        row['visits'] += getattr(v, 'recent_visits', 0) or 0
        if v.last_path and not row['last_path']:
            row['last_path'] = v.last_path
        if v.last_ip_address:
            row['ips'].add(v.last_ip_address)
        if v.last_seen_at and (row['last_seen'] is None or v.last_seen_at > row['last_seen']):
            row['last_seen'] = v.last_seen_at
        if v.first_seen_at and (row['first_seen'] is None or v.first_seen_at < row['first_seen']):
            row['first_seen'] = v.first_seen_at
        if ua and not row['user_agent']:
            row['user_agent'] = ua

    # --- android app devices (installed apps that never logged in) ----------
    try:
        devices = list(AndroidAppDevice.objects.filter(last_seen_at__gte=since)
                       .select_related('android_app').order_by('-last_seen_at')[:2000])
    except Exception as exc:
        logger.warning('active users: device query failed: %s', exc)
        devices = []
    latest_visits = {}
    if devices:
        try:
            for visit in (AndroidAppDeviceVisit.objects.filter(visited_at__gte=since)
                          .order_by('-visited_at')
                          .values('device_id', 'ip_address', 'build_identifier',
                                  'device_model', 'os_version', 'visited_at')[:10000]):
                latest_visits.setdefault(visit['device_id'], visit)
        except Exception as exc:
            logger.warning('active users: device visit query failed: %s', exc)
    for d in devices:
        row = _ensure('android', device_uid=d.user_id or ('app%d' % d.android_app_id))
        row['device_visits'] += d.total_visits or 0
        if d.device_model and not row['device_model']:
            row['device_model'] = d.device_model
        if d.os_version and not row['os_version']:
            row['os_version'] = d.os_version
        try:
            if d.android_app and not row['app_name']:
                row['app_name'] = d.android_app.name
        except Exception:
            pass
        visit = latest_visits.get(d.id)
        if visit:
            if visit['ip_address']:
                row['ips'].add(visit['ip_address'])
                if not row['ip']:
                    row['ip'] = visit['ip_address']
            if visit['build_identifier'] and not row['app_version']:
                row['app_version'] = visit['build_identifier']
            if visit['device_model'] and not row['device_model']:
                row['device_model'] = visit['device_model']
            if visit['os_version'] and not row['os_version']:
                row['os_version'] = visit['os_version']
            if visit['visited_at'] and (row['last_seen'] is None or visit['visited_at'] > row['last_seen']):
                row['last_seen'] = visit['visited_at']
        if d.first_seen_at and (row['first_seen'] is None or d.first_seen_at < row['first_seen']):
            row['first_seen'] = d.first_seen_at

    # --- account details for the users we saw ------------------------------
    user_ids = {r['user_id'] for r in rows.values() if r['user_id']}
    users = {}
    if user_ids:
        users = {u.id: u for u in User.objects.filter(id__in=user_ids).only(
            'id', 'username', 'email', 'date_joined', 'last_login',
            'is_active', 'is_staff', 'is_superuser')}
    names_to_ids = {}
    for uid, u in users.items():
        names_to_ids.setdefault((u.username or '').lower(), set()).add(uid)

    # --- request-log engagement: clicks + time on page ---------------------
    ip_index = {}
    for row in rows.values():
        for addr in row['ips']:
            ip_index.setdefault(addr, []).append(row)
        if row['ip']:
            ip_index.setdefault(row['ip'], []).append(row)
    try:
        log_rows = list(RequestLog.objects.filter(
            created_at__gte=since, category=RequestLog.CAT_PAGE)
            .values('username', 'client_ip')
            .annotate(reqs=Count('id'), clicks=Sum('click_count'),
                      page_ms=Sum('time_on_page_ms'), last=Max('created_at'))[:5000])
    except Exception as exc:
        logger.warning('active users: request log query failed: %s', exc)
        log_rows = []
    for agg in log_rows:
        uname = (agg['username'] or '').strip().lower()
        if uname and uname in names_to_ids:
            wanted = names_to_ids[uname]
            targets = [r for r in rows.values() if r['user_id'] in wanted]
        elif agg['client_ip']:
            targets = ip_index.get(agg['client_ip'], [])
        else:
            targets = []
        if not targets:
            continue
        target = next((r for r in targets if r['platform'] == 'web'), targets[0])
        target['clicks'] += agg['clicks'] or 0
        target['requests'] += agg['reqs'] or 0
        target['page_time_ms'] += agg['page_ms'] or 0
        if agg['last'] and (target['last_seen'] is None or agg['last'] > target['last_seen']):
            target['last_seen'] = agg['last']

    # --- finalise rows ------------------------------------------------------
    for row in rows.values():
        last_seen = row['last_seen']
        row['is_active'] = bool(last_seen and last_seen >= active_cutoff)
        row['is_user'] = bool(row['user_id'])
        row['ips'] = sorted(row['ips'])
        row['ip'] = row['ip'] or (row['ips'][0] if row['ips'] else '')
        row['avg_time'] = round(row['total_time'] / row['views']) if row['views'] else 0
        row['time_on_page_s'] = round(row['page_time_ms'] / 1000) if row['page_time_ms'] else 0
        account = users.get(row['user_id']) if row['user_id'] else None
        row['username'] = account.username if account else ''
        row['email'] = account.email if account else ''
        row['date_joined'] = account.date_joined if account else None
        row['last_login'] = account.last_login if account else None
        row['account_active'] = account.is_active if account else None
        row['is_staff'] = account.is_staff if account else False
        row['is_bot'] = _au_looks_like_bot(row)
        if row['username']:
            row['display_name'] = row['username']
        elif row['device_uid']:
            row['display_name'] = 'Device ' + str(row['device_uid'])[:16]
        elif row['visitor_id']:
            row['display_name'] = 'Visitor ' + str(row['visitor_id'])[:8]
        else:
            row['display_name'] = 'Anonymous'

    all_rows = list(rows.values())

    # Every number on the page is counted from exactly the rows the table is
    # showing, so a card can never disagree with the list below it. Bots are
    # part of that population, which is why the Active card used to look too
    # high while bots were filtered out of the list.
    needle = search.lower()

    def _matches(row):
        if not needle:
            return True
        haystack = ' '.join(str(row.get(f) or '') for f in (
            'display_name', 'username', 'email', 'ip', 'device_model',
            'os_version', 'app_version', 'app_name', 'last_path',
            'last_title', 'user_agent', 'visitor_id', 'device_uid',
            'platform', 'referrer')).lower()
        return needle in haystack

    def _in_scope(row, apply_bots=True, apply_platform=True, apply_status=True):
        if apply_bots:
            if bots == 'hide' and row['is_bot']:
                return False
            if bots == 'only' and not row['is_bot']:
                return False
        if apply_platform and platform != 'all' and row['platform'] != platform:
            return False
        if apply_status:
            if status == 'active' and not row['is_active']:
                return False
            if status == 'inactive' and row['is_active']:
                return False
        return _matches(row)

    # --- filter / sort ------------------------------------------------------
    filtered = [r for r in all_rows if _in_scope(r)]

    # The platform chips describe what each platform would show under the
    # current filters, so they ignore the platform filter itself.
    platform_counts = {}
    active_platform = {}
    for row in all_rows:
        if not _in_scope(row, apply_platform=False):
            continue
        platform_counts[row['platform']] = platform_counts.get(row['platform'], 0) + 1
        if row['is_active']:
            active_platform[row['platform']] = active_platform.get(row['platform'], 0) + 1

    total_rows = len(filtered)
    active_total = sum(1 for r in filtered if r['is_active'])
    inactive_total = total_rows - active_total
    # While bots are hidden the card reports how many were kept out of the
    # list; otherwise it is just the number of bot rows on screen.
    hidden_bots = sum(1 for r in all_rows
                      if r['is_bot'] and _in_scope(r, apply_bots=False))
    bot_total = hidden_bots if bots == 'hide' else sum(1 for r in filtered if r['is_bot'])

    sort_keys = {
        'user': lambda r: (r['display_name'] or '').lower(),
        'platform': lambda r: r['platform'],
        'views': lambda r: r['views'],
        'time': lambda r: r['total_time'],
        'clicks': lambda r: r['clicks'],
        'first_seen': lambda r: r['first_seen'] or epoch,
        'last_seen': lambda r: r['last_seen'] or epoch,
    }
    filtered.sort(key=sort_keys.get(sort, sort_keys['last_seen']),
                  reverse=(direction != 'asc'))

    max_display = 500
    displayed = filtered[:max_display]

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="active_users.csv"'
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow(['Status', 'User', 'Email', 'Platform', 'IP', 'Device',
                         'OS', 'App version', 'Views', 'Total time (s)',
                         'Avg time (s)', 'Clicks', 'Requests', 'Scroll %',
                         'Last page', 'Logins', 'Visits', 'First seen',
                         'Last seen', 'User agent'])
        for row in filtered:
            writer.writerow([
                'Active' if row['is_active'] else 'Inactive', row['display_name'],
                row['email'], row['platform'], row['ip'], row['device_model'],
                row['os_version'], row['app_version'], row['views'],
                row['total_time'], row['avg_time'], row['clicks'],
                row['requests'], round(row['scroll']), row['last_path'],
                row['logins'], row['visits'] + row['device_visits'],
                row['first_seen'].strftime('%Y-%m-%d %H:%M:%S') if row['first_seen'] else '',
                row['last_seen'].strftime('%Y-%m-%d %H:%M:%S') if row['last_seen'] else '',
                row['user_agent'],
            ])
        return response

    from .ip_block import active_blocked_ips
    blocked = active_blocked_ips()

    return render(request, 'core/admin_requests.html', {
        'panel': 'users',
        'rows': displayed,
        'blocked_ips': blocked,
        'blocked_total': len(blocked),
        'shown': len(displayed),
        'row_total': len(filtered),
        'max_display': max_display,
        'total_rows': total_rows,
        'active_total': active_total,
        'inactive_total': inactive_total,
        'bot_total': bot_total,
        'bot_hidden': bots == 'hide',
        'bots': bots,
        'platform_counts': platform_counts,
        'active_platform': active_platform,
        'scope_total': sum(platform_counts.values()),
        'scope_active': sum(active_platform.values()),
        'total_time': sum(r['total_time'] for r in filtered),
        'total_clicks': sum(r['clicks'] for r in filtered),
        'platform': platform,
        'status': status,
        'hours': hours,
        'minutes': minutes,
        'search': search,
        'sort': sort,
        'dir': direction,
        'active_cutoff': active_cutoff,
        'now': now,
        'since': since,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def ip_block_action(request):
    """Block or unblock one IP, then return to the view the admin came from.

    One endpoint serves both the Requests & Users page (per-row button) and the
    Blocked IPs page (unblock / add): ``action`` is ``block`` or ``unblock``,
    ``ip`` the address and ``reason`` an optional note. ``next`` is honoured
    only when it is a local path, so a crafted form cannot bounce elsewhere.
    """
    from .ip_block import block_ip, unblock_ip, normalize_ip, set_permanent

    action = (request.POST.get('action') or '').strip()
    raw_ip = (request.POST.get('ip') or '').strip()
    ip = normalize_ip(raw_ip)
    reason = (request.POST.get('reason') or '').strip()[:300]
    who = (request.user.username or '')[:150]
    permanent = (request.POST.get('permanent') or '').strip().lower() in ('1', 'on', 'true', 'yes')

    if not raw_ip:
        messages.error(request, 'No IP address given.')
    elif not ip:
        messages.error(request, f'“{raw_ip[:64]}” is not a valid IP address — '
                                'use a single IPv4 or IPv6 address.')
    elif action == 'unblock':
        if unblock_ip(ip, who) is not None:
            messages.success(request, f'Unblocked {ip}. Requests from it are served again.')
        else:
            messages.warning(request, f'{ip} was not blocked.')
    elif action == 'block':
        row, created = block_ip(ip, who, reason, permanent=permanent)
        if row is not None and row.is_permanent:
            messages.success(request,
                             f'Blocked {ip} permanently. It gets the block page on every '
                             'request and signing in or registering will not lift it.')
        elif created:
            messages.success(request,
                             f'Blocked {ip}. It now sees only the sign-in / register page — '
                             'and its requests are still logged, so you can see what it tries next.')
        else:
            messages.success(request, f'Re-blocked {ip}. Its block clock was reset.')
    elif action == 'permanent':
        if set_permanent(ip, True, who) is not None:
            messages.success(request,
                             f'{ip} is now blocked permanently — signing in or registering '
                             'will not lift it.')
        else:
            messages.warning(request, f'{ip} is not blocked, so it cannot be made permanent.')
    elif action == 'temporary':
        if set_permanent(ip, False, who) is not None:
            messages.success(request,
                             f'{ip} is a normal block again — signing in or registering '
                             'will lift it.')
        else:
            messages.warning(request, f'{ip} is not blocked.')
    else:
        messages.error(request, 'Unknown action.')

    # One timeline entry per block/lift, for the Audit log page.
    if ip:
        from .audit import log_action
        log_action(request, 'block_ip' if action in ('block', 'permanent') else 'unblock_ip',
                   ip, 'action=%s permanent=%s reason=%s' % (action, permanent, reason))

    nxt = (request.POST.get('next') or '').strip()
    if not (nxt.startswith('/') and not nxt.startswith('//')):
        nxt = reverse('blocked_ips')
    return redirect(nxt)


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def trusted_ip_action(request):
    """Add or remove an address on the trusted (never-block) allow-list.

    The automatic guards — bot rate, unknown-page probes, script User-Agents —
    are all evidence-based, and a wrong call locks out a real person. The
    operator's own address is the one that must never be guessed wrong: running
    a script, a smoke test or a ``curl`` from it is normal work. Addresses listed
    here are skipped by every watch and by the block decision itself.

    ``action`` is ``add`` (default) or ``remove``; ``ip`` is one address. The
    list is stored on ``SiteSettings.allowed_ips`` as comma-separated text so it
    stays editable straight from the settings form as well.
    """
    from .ip_block import normalize_ip, parse_allowed_ips, reset_allowed_ips_cache, unblock_ip
    from .models import SiteSettings

    action = (request.POST.get('action') or 'add').strip().lower()
    raw_ip = (request.POST.get('ip') or '').strip()
    ip = normalize_ip(raw_ip)
    who = (request.user.username or '')[:150]

    if not raw_ip:
        messages.error(request, 'No IP address given.')
    elif not ip:
        messages.error(request, f'“{raw_ip[:64]}” is not a valid IP address — '
                                'use a single IPv4 or IPv6 address.')
    else:
        conf = SiteSettings.get_settings()
        current = parse_allowed_ips(conf.allowed_ips)
        if action == 'remove':
            if ip in current:
                current = [entry for entry in current if entry != ip]
                conf.allowed_ips = ', '.join(current)
                conf.save(update_fields=['allowed_ips'])
                reset_allowed_ips_cache()
                messages.success(request,
                                 f'Removed {ip} from the trusted list — the automatic '
                                 'guards apply to it again.')
            else:
                messages.warning(request, f'{ip} was not on the trusted list.')
        elif action == 'add':
            if ip in current:
                messages.info(request, f'{ip} is already trusted.')
            else:
                current.append(ip)
                conf.allowed_ips = ', '.join(current)
                conf.save(update_fields=['allowed_ips'])
                reset_allowed_ips_cache()
                messages.success(request,
                                 f'{ip} is trusted now — the bot, probe and script '
                                 'watchers will never block it.')
            # A trusted address that is currently blocked would otherwise keep
            # getting the block page, because the row still exists. Lift it.
            if unblock_ip(ip, who) is not None:
                messages.success(request, f'The existing block on {ip} was lifted as well.')
        else:
            messages.error(request, 'Unknown action.')

        from .audit import log_action
        log_action(request, 'trust_ip' if action == 'add' else 'untrust_ip', ip,
                   'action=%s' % action)

    nxt = (request.POST.get('next') or '').strip()
    if not (nxt.startswith('/') and not nxt.startswith('//')):
        nxt = reverse('traffic_control')
    return redirect(nxt)


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_blocked_ips(request):
    """Blocked IPs — every address the site refuses, and what it tried anyway.

    A blocked address answers 403 on every public request, but the request is
    still recorded: this page counts the requests it has made and the requests
    that kept arriving *after* the block, and lists the pages it asked for. A
    block can be added or lifted straight from here.
    """
    import csv
    from django.http import HttpResponse
    from .ip_block import blocked_ip_report

    show_all = (request.GET.get('show') or 'active') == 'all'
    search = (request.GET.get('q') or '').strip()[:64]
    rows, totals = blocked_ip_report(include_unblocked=show_all, search=search)

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="blocked_ips.csv"'
        # BOM so Excel opens the UTF-8 file without mangling accented data.
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow([
            'IP', 'Status', 'Permanent', 'Blocked times', 'Auto-bot',
            'Lifted by signup', 'Blocked at', 'Blocked by', 'Reason',
            'Unblocked at', 'Unblocked by', 'Requests (total)',
            'Requests after block', '403s served', 'Pages after block',
            'Distinct pages', 'First seen', 'Last seen',
            'Last seen after block', 'Users', 'User agent',
        ])

        def _stamp(value):
            return timezone.localtime(value).strftime('%Y-%m-%d %H:%M:%S') if value else ''

        for r in rows:
            writer.writerow([
                r['ip'], 'Blocked' if r['is_active'] else 'Unblocked',
                'Yes' if r['is_permanent'] else 'No', r['offense_count'],
                'Yes' if r['is_bot'] else 'No',
                'Yes' if r['lifted_by_signup'] else 'No',
                _stamp(r['blocked_at']), r['blocked_by'], r['reason'],
                _stamp(r['unblocked_at']), r['unblocked_by'], r['total'],
                r['after'], r['after_403'],
                ' | '.join(f"{p['path']} x{p['count']}" for p in r['paths']),
                r['path_total'], _stamp(r['first_seen']), _stamp(r['last_seen']),
                _stamp(r['last_after']), ', '.join(r['top_users']), r['user_agent'],
            ])
        return response

    from .request_log import request_total
    from .header_block import active_rules, rule_for_value

    # Each row's recorded User-Agent can be blocked as a header rule from right
    # here, and a row already covered by one is marked instead of offered twice.
    for row in rows:
        row['header_rule'] = rule_for_value(row['user_agent'])

    return render(request, 'core/admin_blocked_ips.html', {
        'rows': rows,
        'totals': totals,
        'show_all': show_all,
        'q': search,
        # Counts on this page are derived from the retained request log, so say
        # how big that window currently is.
        'total_stored': request_total(),
        'header_rules_total': len(active_rules()),
    })


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def header_block_action(request):
    """Block, lift, publish or delete one header rule, then return where we came from.

    One endpoint serves every page that shows a header: ``block`` takes the
    ``value`` (plus an optional ``header_name`` and ``reason``) and refuses every
    request carrying that text, whatever the address; ``unblock`` switches a rule
    off, ``delete`` removes it, and ``robots`` / ``no-robots`` publish it in
    robots.txt. ``next`` is honoured only when it is a local path.
    """
    from .header_block import (MIN_VALUE_LENGTH, block_header, delete_header_rule,
                               set_robots_disallow, unblock_header)

    action = (request.POST.get('action') or '').strip()
    who = (request.user.username or '')[:150]
    header_name = (request.POST.get('header_name') or 'User-Agent').strip()
    raw_value = (request.POST.get('value') or '').strip()
    reason = (request.POST.get('reason') or '').strip()[:300]
    robots = (request.POST.get('robots') or '').strip().lower() in ('1', 'on', 'true', 'yes')
    try:
        rule_id = int(request.POST.get('id') or 0)
    except (TypeError, ValueError):
        rule_id = 0

    if action == 'bots':
        # One click for "block every bot except Google": the whole profile of
        # known crawlers and scraper clients becomes a set of User-Agent rules.
        # Each one is an ordinary rule in the table below, so the admin can lift
        # a single bot (Bing, for instance) without touching the rest.
        from .header_block import block_known_bots
        created, already, skipped = block_known_bots(
            who, reason or 'bot profile: every known bot except Google')
        parts = [f'{created} new bot rule{"s" if created != 1 else ""} added']
        if already:
            parts.append(f'{already} already in place')
        message = 'Bot block is on — ' + ', '.join(parts) + '. Google crawlers stay allowed.'
        if skipped:
            message += ' Skipped (would also match Google): ' + ', '.join(skipped) + '.'
        messages.success(request, message)
    elif action == 'block':
        if not raw_value:
            messages.error(request, 'Enter the header text to block — for example ClaudeBot.')
        else:
            row, created = block_header(raw_value, header_name, who, reason,
                                        robots_disallow=robots)
            if row is None:
                messages.error(
                    request,
                    f'“{raw_value[:60]}” is too short or unusable as a rule — it needs at '
                    f'least {MIN_VALUE_LENGTH} characters, otherwise it would match everything.')
            elif created:
                extra = ' It is published in robots.txt as well.' if row.robots_disallow else ''
                messages.success(
                    request,
                    f'Blocked every request whose {row.header_name} contains '
                    f'“{row.value}”, whatever address it comes from.{extra}')
            else:
                messages.success(request, f'The rule for {row.header_name} “{row.value}” is on.')
    elif action == 'unblock':
        row = unblock_header(rule_id, who)
        if row is not None:
            messages.success(request, f'Lifted the rule on “{row.value}” — requests carrying it are served again.')
        else:
            messages.warning(request, 'That rule was not switched on.')
    elif action == 'delete':
        if delete_header_rule(rule_id):
            messages.success(request, 'Rule deleted.')
        else:
            messages.warning(request, 'That rule no longer exists.')
    elif action in ('robots', 'no-robots'):
        row = set_robots_disallow(rule_id, action == 'robots', who)
        if row is None:
            messages.warning(request, 'That rule no longer exists.')
        elif action == 'robots':
            messages.success(request, f'“{row.value}” is now listed in robots.txt as well.')
        else:
            messages.success(request, f'“{row.value}” is no longer listed in robots.txt.')
    else:
        messages.error(request, 'Unknown action.')

    # One timeline entry per rule change, for the Audit log page.
    from .audit import log_action
    audit_action = {
        'bots': 'bot_profile', 'block': 'block_header', 'unblock': 'unblock_header',
        'delete': 'delete_header', 'robots': 'robots_header',
        'no-robots': 'robots_header',
    }.get(action, '')
    if audit_action:
        target = ('bot profile (every rule)' if action == 'bots'
                  else '%s: %s' % (header_name, raw_value))
        log_action(request, audit_action, target,
                   'action=%s%s' % (action, (' reason=' + reason) if reason else ''))

    nxt = (request.POST.get('next') or '').strip()
    if not (nxt.startswith('/') and not nxt.startswith('//')):
        nxt = reverse('blocked_headers')
    return redirect(nxt)


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_blocked_headers(request):
    """Blocked headers — the header rules every request is read against.

    An IP block answers one address. A rule here answers the program, so a
    crawler that announces itself in its User-Agent is refused from any address,
    including one that has never been seen. A rule can also be published in
    robots.txt, which is the polite half of the same decision.
    """
    import csv
    from django.http import HttpResponse
    from .header_block import (BOT_BLOCK_PROFILE, COMMON_HEADERS, MIN_VALUE_LENGTH,
                               SUGGESTED_RULES, blocked_header_report)

    show_all = (request.GET.get('show') or 'active') == 'all'
    search = (request.GET.get('q') or '').strip()[:100]
    rows, totals = blocked_header_report(include_inactive=show_all, search=search)

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="blocked_headers.csv"'
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow([
            'Header', 'Value', 'Status', 'In robots.txt', 'Requests refused',
            'Counted', 'First refused', 'Last refused', 'Addresses seen',
            'Top addresses', 'Top pages', 'Blocked at', 'Blocked by', 'Reason',
            'Switched off at', 'Switched off by',
        ])

        def _stamp(value):
            return timezone.localtime(value).strftime('%Y-%m-%d %H:%M:%S') if value else ''

        for r in rows:
            writer.writerow([
                r['header_name'], r['value'],
                'Blocked' if r['is_active'] else 'Off',
                'Yes' if (r['robots_disallow'] and r['robots_token']) else 'No',
                r['refused'] if r['counted'] else '',
                'Yes' if r['counted'] else 'No', _stamp(r['first_refused']),
                _stamp(r['last_refused']), r['ip_total'],
                ', '.join(r['top_ips']), ' | '.join(r['top_paths']),
                _stamp(r['blocked_at']), r['blocked_by'], r['reason'],
                _stamp(r['unblocked_at']), r['unblocked_by'],
            ])
        return response

    from .request_log import request_total

    return render(request, 'core/admin_blocked_headers.html', {
        'rows': rows,
        'totals': totals,
        'show_all': show_all,
        'q': search,
        'common_headers': COMMON_HEADERS,
        'suggested': SUGGESTED_RULES,
        'min_value_length': MIN_VALUE_LENGTH,
        'bot_profile': BOT_BLOCK_PROFILE,
        'bot_profile_count': len(BOT_BLOCK_PROFILE),
        # The rule counts come from the retained request log, so say how big
        # that window currently is.
        'total_stored': request_total(),
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_probes(request):
    """Probes & Bots — who is asking for pages that are not on the website.

    One row per address: how many unknown pages it asked for (404s and scanner
    paths), exactly which ones, its request rate, and the signals that make it
    look automated. Every row can be blocked straight from here, and the bottom
    table shows which non-existent pages are being hunted for most.
    """
    import csv
    from django.http import HttpResponse
    from .probes import probe_report
    from .request_log import request_total

    hours = _au_int_param(request, 'hours', 24, 1, 720)
    min_hits = _au_int_param(request, 'min_hits', 1, 1, 100000)
    hide_blocked = request.GET.get('hide_blocked') == '1'
    search = (request.GET.get('q') or '').strip()[:64]
    rows, totals, top_paths = probe_report(
        hours=hours, min_hits=min_hits, hide_blocked=hide_blocked, search=search,
    )

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="probes.csv"'
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow([
            'IP', 'Blocked', 'Unknown pages', 'Distinct unknown pages',
            'Scanner paths', '404s', 'Requests in window', 'Per hour',
            'Signals', 'First seen', 'Last seen', 'User agent', 'Pages requested',
        ])

        def _stamp(value):
            return timezone.localtime(value).strftime('%Y-%m-%d %H:%M:%S') if value else ''

        for r in rows:
            writer.writerow([
                r['ip'], 'Yes' if r['blocked'] else 'No', r['unknown'],
                r['path_total'], r['probes'], r['not_found'], r['requests'],
                r['per_hour'], ', '.join(r['signals']), _stamp(r['first_seen']),
                _stamp(r['last_seen']), r['user_agent'],
                ' | '.join(f"{p['path']} x{p['count']}" for p in r['paths']),
            ])
        return response

    return render(request, 'core/admin_probes.html', {
        'rows': rows,
        'totals': totals,
        'top_paths': top_paths,
        'hours': hours,
        'min_hits': min_hits,
        'hide_blocked': hide_blocked,
        'q': search,
        'total_stored': request_total(),
    })


# ---------------------------------------------------------------------------
# Visitor journeys — one history per visitor, page by page
# ---------------------------------------------------------------------------

# A break longer than this starts a new visit (the usual session timeout).
JOURNEY_SESSION_GAP_SECONDS = 30 * 60
JOURNEY_MAX_HISTORY = 150
JOURNEY_MAX_LISTED = 300


def _describe_device(user_agent):
    """A short "browser on OS" label for one User-Agent string."""
    ua = (user_agent or '').strip()
    if not ua:
        return 'Unknown device'
    low = ua.lower()
    if any(m in low for m in ('bot', 'spider', 'crawl', 'curl/', 'wget',
                              'python-requests', 'go-http', 'headless')):
        return 'Bot / script'
    if 'okhttp' in low or 'x-android-app' in low:
        return 'Android app'
    if 'android' in low:
        os_name = 'Android'
    elif 'iphone' in low or 'ipad' in low:
        os_name = 'iOS'
    elif 'windows' in low:
        os_name = 'Windows'
    elif 'mac os x' in low or 'macintosh' in low:
        os_name = 'macOS'
    elif 'linux' in low:
        os_name = 'Linux'
    else:
        os_name = 'unknown OS'
    if 'edg/' in low:
        browser = 'Edge'
    elif 'opr/' in low or 'opera' in low:
        browser = 'Opera'
    elif 'samsungbrowser' in low:
        browser = 'Samsung Internet'
    elif 'firefox' in low or 'fxios' in low:
        browser = 'Firefox'
    elif 'chrome' in low or 'crios' in low:
        browser = 'Chrome'
    elif 'safari' in low:
        browser = 'Safari'
    else:
        browser = 'browser'
    return '%s on %s' % (browser, os_name)


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_visitor_journeys(request):
    """Every visitor's own history, from the page they started on to the last.

    Grouped by the device cookie (``visitor_id``) by default — one browser keeps
    one identity even when its IP changes — or by IP (``?group=ip``) when the
    question is who shares an address. Each visitor expands into their visits,
    and each visit into its pages in order, with the time spent on each page, so
    a journey reads start to finish.

    Only page views are used: they carry the time on page, the device and the
    referrer. A scanner that never loads a page produces none and so appears
    nowhere here — it is visible on Probes & Bots instead.
    """
    from .models import UserPageView

    hours = _au_int_param(request, 'hours', 24, 1, 720)
    group = 'ip' if (request.GET.get('group') or '').lower() == 'ip' else 'visitor'
    search = (request.GET.get('q') or '').strip()
    now = timezone.now()
    since = now - timezone.timedelta(hours=hours)

    views = UserPageView.objects.filter(viewed_at__gte=since).order_by('viewed_at')
    if search:
        from django.db.models import Q
        views = views.filter(Q(path__icontains=search) | Q(page_title__icontains=search)
                             | Q(visitor_id__icontains=search)
                             | Q(ip_address__icontains=search)
                             | Q(user_agent__icontains=search))
    rows = list(views.values(
        'visitor_id', 'user_id', 'path', 'page_title', 'platform', 'ip_address',
        'user_agent', 'referrer', 'time_spent_seconds', 'scroll_depth',
        'is_active', 'viewed_at', 'ended_at')[:20000])

    groups = {}
    for row in rows:
        ip = row['ip_address'] or ''
        if group == 'ip':
            key = ip or 'unknown'
        else:
            key = row['visitor_id'] or ('ip:' + (ip or 'unknown'))
        entry = groups.get(key)
        if entry is None:
            entry = groups[key] = {
                'key': key, 'rows': [], 'ips': [], 'devices': [],
                'user_ids': [], 'path': row['path'], 'title': row['page_title'],
                'referrer': row['referrer'], 'platform': row['platform'],
            }
        entry['rows'].append(row)
        if ip and ip not in entry['ips']:
            entry['ips'].append(ip)
        device = _describe_device(row['user_agent'])
        if device not in entry['devices']:
            entry['devices'].append(device)
        if row['user_id'] and row['user_id'] not in entry['user_ids']:
            entry['user_ids'].append(row['user_id'])

    journeys = []
    total_views = 0
    total_sessions = 0
    total_seconds = 0
    for entry in groups.values():
        history = entry['rows']
        trimmed = len(history) > JOURNEY_MAX_HISTORY
        if trimmed:
            history = history[:JOURNEY_MAX_HISTORY]
        previous = None
        session_no = 0
        session_start = None
        sessions = []
        for index, row in enumerate(history):
            gap = None
            if previous is not None:
                gap = (row['viewed_at'] - previous['viewed_at']).total_seconds()
            starts = previous is None or (gap or 0) > JOURNEY_SESSION_GAP_SECONDS
            if starts:
                session_no += 1
                session_start = row['viewed_at']
                sessions.append({
                    'no': session_no, 'started': row['viewed_at'],
                    'entry_path': row['path'], 'entry_title': row['page_title'],
                    'referrer': row['referrer'], 'pages': 0, 'seconds': 0,
                    'ended': row['viewed_at'],
                })
            reported = int(row['time_spent_seconds'] or 0)
            estimated = False
            if reported <= 0 and index + 1 < len(history):
                # No heartbeat landed: fall back to the gap to the next page,
                # capped so an idle tab cannot invent an hour of reading.
                dwell = int(min(max((history[index + 1]['viewed_at']
                                     - row['viewed_at']).total_seconds(), 0), 600))
                estimated = True
            else:
                dwell = reported
            row['dwell'] = dwell
            row['estimated'] = estimated
            row['gap'] = int(gap) if gap is not None else None
            row['device'] = _describe_device(row['user_agent'])
            row['session'] = session_no
            row['starts_session'] = starts
            current = sessions[-1]
            current['pages'] += 1
            current['seconds'] += dwell
            current['ended'] = row['viewed_at']
            total_seconds += dwell
            previous = row

        entry['history'] = history
        entry['sessions'] = sessions
        entry['trimmed'] = trimmed
        entry['views'] = len(entry['rows'])
        entry['first_seen'] = entry['rows'][0]['viewed_at']
        entry['last_seen'] = entry['rows'][-1]['viewed_at']
        entry['time'] = sum(row['dwell'] for row in history)
        entry['active'] = any(row['is_active'] for row in entry['rows'])
        del entry['rows']
        journeys.append(entry)
        total_views += entry['views']
        total_sessions += len(sessions)

    journeys.sort(key=lambda j: j['last_seen'], reverse=True)
    visitor_total = len(journeys)
    shown = journeys[:JOURNEY_MAX_LISTED]

    return render(request, 'core/admin_requests.html', {
        'panel': 'journeys',
        'journeys': shown,
        'visitor_total': visitor_total,
        'shown': len(shown),
        'row_total': visitor_total,
        'hours': hours,
        'group': group,
        'search': search,
        'total_views': total_views,
        'total_sessions': total_sessions,
        'total_seconds': total_seconds,
        'avg_page_seconds': int(total_seconds / total_views) if total_views else 0,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_active_users_redirect(request):
    """Legacy active-users URL → the merged Requests & Users page.

    Keeps old bookmarks (and the analytics cards' previous target) working
    without a second copy of the page.
    """
    # 'tab' first so the resulting link reads like the one the analytics
    # cards generate: ?tab=users&platform=web
    pairs = [('tab', 'users')]
    pairs += [(key, value) for key, value in request.GET.items() if key != 'tab']
    return redirect('%s?%s' % (reverse('request_log'), _urlencode(pairs)))


# ---------------------------------------------------------------------------
# Admin User Management (block, add, edit all fields except email)
# ---------------------------------------------------------------------------

@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_list(request):
    """Admin page listing all Django users with block/edit/add actions."""
    from .models import UserSession
    users = User.objects.all().order_by('-date_joined')
    search = request.GET.get('q', '').strip()
    if search:
        users = users.filter(
            models.Q(username__icontains=search) |
            models.Q(email__icontains=search) |
            models.Q(first_name__icontains=search)
        )
    total = users.count()
    active = users.filter(is_active=True).count()
    blocked = total - active
    # Filters
    filter_status = request.GET.get('status', '').strip()
    filter_sub = request.GET.get('subscription', '').strip()
    filter_source = request.GET.get('source', '').strip()

    if filter_status == 'active':
        users = users.filter(is_active=True)
    elif filter_status == 'blocked':
        users = users.filter(is_active=False)

    if filter_sub == 'subscribed':
        users = users.filter(synced_profiles__is_subscribed=True)
    elif filter_sub == 'free':
        users = users.filter(synced_profiles__isnull=True) | users.filter(synced_profiles__is_subscribed=False)

    if filter_source == 'android':
        users = users.filter(sessions__source='android').distinct()
    elif filter_source == 'web':
        users = users.exclude(sessions__source='android').distinct()

    # Deduplicate after OR queries
    users = users.distinct()

    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(users, 25)
    page = request.GET.get('page')
    users = paginator.get_page(page)

    all_users = User.objects.all()
    total = all_users.count()
    active = all_users.filter(is_active=True).count()
    blocked = total - active
    subscribed_count = SyncedUser.objects.filter(is_subscribed=True).count()

    # Session stats for each user on this page — attach as attributes
    user_ids = [u.id for u in users]
    if user_ids:
        from django.db.models import Count, Max, Q
        # Build lookup dicts
        active_map = {}
        logins_map = {}
        source_map = {}
        last_login_map = {}
        session_qs = UserSession.objects.filter(user_id__in=user_ids)
        for row in session_qs.filter(is_active=True).values('user_id').annotate(c=Count('id')):
            active_map[row['user_id']] = row['c']
        for row in session_qs.values('user_id').annotate(c=Count('id')):
            logins_map[row['user_id']] = row['c']
        # Most recent session per user (by logged_in_at), capturing the
        # login time, source, IP and app version for the Last Login column.
        latest_rows = (
            session_qs
            .order_by('user_id', '-logged_in_at')
            .values('user_id', 'source', 'logged_in_at', 'ip_address', 'app_version')
        )
        seen = set()
        for row in latest_rows:
            uid = row['user_id']
            if uid in seen:
                continue
            seen.add(uid)
            source_map[uid] = row.get('source') or ''
            last_login_map[uid] = {
                'time': row.get('logged_in_at'),
                'source': row.get('source') or '',
                'ip': row.get('ip_address') or '',
                'app_version': row.get('app_version') or '',
            }
        # Attach to each user object
        for u in users:
            u.session_active_count = active_map.get(u.id, 0)
            u.session_total_logins = logins_map.get(u.id, 0)
            u.session_last_source = source_map.get(u.id, '')
            u.session_last_login = last_login_map.get(u.id)

    # Compute totals
    total_active_sessions = UserSession.objects.filter(is_active=True).count()
    total_logins = UserSession.objects.count()
    android_users = UserSession.objects.filter(source='android').values('user_id').distinct().count()
    web_users = UserSession.objects.filter(source='web').values('user_id').distinct().count()

    return render(request, 'core/admin_user_list.html', {
        'users': users,
        'total': total,
        'active': active,
        'blocked': blocked,
        'subscribed_count': subscribed_count,
        'search': search,
        'filter_status': filter_status,
        'filter_sub': filter_sub,
        'filter_source': filter_source,
        'total_active_sessions': total_active_sessions,
        'total_logins': total_logins,
        'android_users': android_users,
        'web_users': web_users,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_block(request, user_id):
    """Toggle block/unblock a user."""
    user_obj = get_object_or_404(User, id=user_id)
    if request.method == 'POST':
        user_obj.is_active = not user_obj.is_active
        user_obj.save(update_fields=['is_active'])
        status = 'unblocked' if user_obj.is_active else 'blocked'
        return JsonResponse({'success': True, 'is_active': user_obj.is_active, 'message': f'User {status}'})
    return JsonResponse({'success': False, 'message': 'POST required'})


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_backfill_posters(request):
    """POST /admin-dashboard/ajax/backfill-posters/ — fetch missing posters from TMDB.

    Runs synchronously for up to 50 rows (quick enough for an AJAX call).
    Returns JSON with counts of updated/skipped/total rows.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST required'})
    model = request.POST.get('model', 'both')
    limit = min(int(request.POST.get('limit', 50)), 200)
    from .models import PlayHistory, WatchList
    from django.conf import settings as _s
    import time as _time

    def _fetch(tid, mtype):
        # Use the project's TMDBClient — it reads the API key from the
        # TMDBApiKey DB model (the primary key storage in this project).
        try:
            client = TMDBClient()
            path = '/movie' if mtype == 'movie' else '/tv'
            data = client._make_request(f'{path}/{tid}')
            if data:
                return data.get('poster_path') or '', data.get('title') or data.get('name') or ''
        except Exception:
            pass
        # Legacy fallbacks: settings / SiteSettings
        try:
            api_key = _s.TMDB_API_KEY
        except AttributeError:
            api_key = ''
        if not api_key:
            try:
                from .models import SiteSettings
                s = SiteSettings.get_settings()
                api_key = s.tmdb_api_key or ''
            except Exception:
                api_key = ''
        if not api_key:
            return None, None
        path = '/movie' if mtype == 'movie' else '/tv'
        try:
            resp = requests.get(
                f'https://api.themoviedb.org/3{path}/{tid}',
                params={'api_key': api_key, 'language': 'en-US'},
                timeout=8,
            )
            if resp.status_code == 200:
                d = resp.json()
                return d.get('poster_path') or '', d.get('title') or d.get('name') or ''
        except Exception:
            pass
        return None, None

    results = {'play_history': {'updated': 0, 'skipped': 0, 'total': 0},
               'watchlist': {'updated': 0, 'skipped': 0, 'total': 0}}

    if model in ('PlayHistory', 'both'):
        qs = PlayHistory.objects.filter(
            models.Q(poster_path='') | models.Q(poster_path__isnull=True)
            | models.Q(title='') | models.Q(title__isnull=True)
        ).exclude(tmdb_id__lte=0)[:limit]
        results['play_history']['total'] = qs.count()
        for h in qs:
            poster, title = _fetch(h.tmdb_id, h.media_type)
            changed = []
            if title and not h.title:
                h.title = title
                changed.append('title')
            if poster and not h.poster_path:
                h.poster_path = poster
                changed.append('poster_path')
            if changed:
                h.save(update_fields=changed)
                results['play_history']['updated'] += 1
            else:
                results['play_history']['skipped'] += 1
            _time.sleep(0.12)

    if model in ('WatchList', 'both'):
        qs = WatchList.objects.filter(
            models.Q(poster_path='') | models.Q(poster_path__isnull=True)
            | models.Q(title='') | models.Q(title__isnull=True)
        ).exclude(tmdb_id__lte=0)[:limit]
        results['watchlist']['total'] = qs.count()
        for w in qs:
            poster, title = _fetch(w.tmdb_id, w.media_type)
            changed = []
            if title and not w.title:
                w.title = title
                changed.append('title')
            if poster and not w.poster_path:
                w.poster_path = poster
                changed.append('poster_path')
            if changed:
                w.save(update_fields=changed)
                results['watchlist']['updated'] += 1
            else:
                results['watchlist']['skipped'] += 1
            _time.sleep(0.12)

    total_updated = results['play_history']['updated'] + results['watchlist']['updated']
    total_skipped = results['play_history']['skipped'] + results['watchlist']['skipped']
    return JsonResponse({
        'success': True,
        'message': f'Backfill complete: {total_updated} entries fixed, {total_skipped} skipped (no TMDB data).',
        'results': results,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_add(request):
    """Admin: add a new user."""
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip().lower()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        password = request.POST.get('password', '')
        is_staff = request.POST.get('is_staff') == 'on'
        is_superuser = request.POST.get('is_superuser') == 'on'
        is_active = request.POST.get('is_active', 'on') == 'on'
        
        if not username:
            messages.error(request, 'Username is required')
            return render(request, 'core/admin_user_add.html')
        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already exists')
            return render(request, 'core/admin_user_add.html')
        if email and User.objects.filter(email=email).exists():
            messages.error(request, 'Email already registered')
            return render(request, 'core/admin_user_add.html')
        
        user_obj = User.objects.create_user(
            username=username,
            email=email,
            password=password if password else None,
            first_name=first_name,
            last_name=last_name,
        )
        user_obj.is_staff = is_staff
        user_obj.is_superuser = is_superuser
        user_obj.is_active = is_active
        user_obj.save()
        messages.success(request, f'User {username} created successfully')
        return redirect('admin_user_list')
    return render(request, 'core/admin_user_add.html')


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_edit(request, user_id):
    """Admin: edit any user field except email."""
    user_obj = get_object_or_404(User, id=user_id)
    if request.method == 'POST':
        user_obj.username = request.POST.get('username', user_obj.username).strip()
        user_obj.first_name = request.POST.get('first_name', '').strip()
        user_obj.last_name = request.POST.get('last_name', '').strip()
        user_obj.is_staff = request.POST.get('is_staff') == 'on'
        user_obj.is_superuser = request.POST.get('is_superuser') == 'on'
        user_obj.is_active = request.POST.get('is_active') == 'on'
        # Email is read-only — do not update
        new_password = request.POST.get('password', '').strip()
        if new_password:
            user_obj.set_password(new_password)
        user_obj.save()
        messages.success(request, f'User {user_obj.username} updated successfully')
        return redirect('admin_user_list')
    synced_profile = SyncedUser.objects.filter(user=user_obj).first()
    return render(request, 'core/admin_user_edit.html', {
        'edit_user': user_obj,
        'synced_profile': synced_profile,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_delete(request, user_id):
    """Admin: permanently delete a user and ALL linked data by email."""
    user_obj = get_object_or_404(User, id=user_id)
    if request.method == 'POST':
        username = user_obj.username
        email = _permanently_delete_user(user_obj)
        messages.success(request, f'User {username} ({email}) and all linked data (profile, sessions, cloud data, watchlist, play history) permanently deleted.')
    return redirect('admin_user_list')


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def bulk_send_email(request):
    """Send email to multiple users at once."""
    try:
        data = json.loads(request.body)
        ids = data.get('ids', [])
        subject = data.get('subject', '').strip()
        body = data.get('body', '').strip()
        if not ids or not subject or not body:
            return JsonResponse({'data': {'success': False, 'message': 'Missing ids, subject, or body'}})
        users = User.objects.filter(id__in=ids, email__isnull=False).exclude(email='')
        sent = 0
        failed = 0
        from core.models import EmailAddress
        smtp_addr = EmailAddress.objects.filter(is_active=True).first()
        for u in users:
            try:
                msg = DjangoEmailMessage(
                    subject=subject, body=body,
                    from_email=smtp_addr.email if smtp_addr else settings.DEFAULT_FROM_EMAIL,
                    to=[u.email],
                )
                if smtp_addr:
                    msg.connection = smtp_addr.get_backend()
                msg.send(fail_silently=True)
                sent += 1
            except Exception:
                failed += 1
        return JsonResponse({'data': {'success': True, 'message': f'Email sent to {sent} user(s)' + (f', {failed} failed' if failed else '')}})
    except Exception as e:
        return JsonResponse({'data': {'success': False, 'message': str(e)}})


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def bulk_change_plan(request):
    """Change subscription plan for multiple users."""
    try:
        data = json.loads(request.body)
        ids = data.get('ids', [])
        plan = data.get('plan', '').strip()
        if not ids or not plan:
            return JsonResponse({'data': {'success': False, 'message': 'Missing ids or plan'}})
        updated = 0
        for uid in ids:
            try:
                u = User.objects.get(id=uid)
                synced = SyncedUser.objects.filter(user=u).first()
                if not synced and u.email:
                    synced = SyncedUser.objects.filter(email=u.email).first()
                if synced:
                    synced.is_subscribed = plan.lower() != 'standard free' and plan.lower() != 'free'
                    synced.plan = plan
                    synced.save(update_fields=['is_subscribed', 'plan'])
                    updated += 1
                else:
                    SyncedUser.objects.create(
                        user=u, email=u.email or '',
                        is_subscribed=plan.lower() != 'standard free' and plan.lower() != 'free',
                        plan=plan,
                    )
                    updated += 1
            except Exception:
                continue
        return JsonResponse({'data': {'success': True, 'message': f'Plan updated for {updated} user(s)'}})
    except Exception as e:
        return JsonResponse({'data': {'success': False, 'message': str(e)}})


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def bulk_delete_users(request):
    """Delete multiple users and all linked data."""
    try:
        data = json.loads(request.body)
        ids = data.get('ids', [])
        if not ids:
            return JsonResponse({'data': {'success': False, 'message': 'No users selected'}})
        deleted = 0
        for uid in ids:
            try:
                u = User.objects.get(id=uid)
                _permanently_delete_user(u)
                deleted += 1
            except Exception:
                continue
        return JsonResponse({'data': {'success': True, 'message': f'Deleted {deleted} user(s)'}})
    except Exception as e:
        return JsonResponse({'data': {'success': False, 'message': str(e)}})


import json as json_mod  # ensure json is available


# ---------------------------------------------------------------------------
# Content Management — Admin can add/edit/remove movies & TV shows directly
# ---------------------------------------------------------------------------

@login_required
@user_passes_test(is_staff_or_superuser)
def admin_content_list(request):
    """List all movies and TV shows with search, filter, and pagination."""
    from .models import TMDBMovie, TMDBTV
    from django.core.paginator import Paginator

    content_type = request.GET.get('type', 'all')  # all, movie, tv
    search = request.GET.get('q', '').strip()
    sort = request.GET.get('sort', '-popularity')

    movies = TMDBMovie.objects.all()
    tv_shows = TMDBTV.objects.all()

    if search:
        movies = movies.filter(
            Q(title__icontains=search) | Q(original_title__icontains=search) | Q(overview__icontains=search)
        )
        tv_shows = tv_shows.filter(
            Q(name__icontains=search) | Q(original_name__icontains=search) | Q(overview__icontains=search)
        )

    # Combine into a unified list
    items = []
    if content_type in ('all', 'movie'):
        for m in movies:
            items.append({
                'id': m.id, 'type': 'movie', 'title': m.title,
                'poster': m.poster_path, 'overview': (m.overview or '')[:150],
                'rating': m.vote_average, 'year': (m.release_date or '')[:4],
                'popularity': m.popularity or 0,
                'status': m.status or '',
            })
    if content_type in ('all', 'tv'):
        for t in tv_shows:
            items.append({
                'id': t.id, 'type': 'tv', 'title': t.name,
                'poster': t.poster_path, 'overview': (t.overview or '')[:150],
                'rating': t.vote_average, 'year': (t.first_air_date or '')[:4],
                'popularity': t.popularity or 0,
                'status': t.status or '',
            })

    # Sort
    if sort == '-popularity':
        items.sort(key=lambda x: x['popularity'], reverse=True)
    elif sort == 'popularity':
        items.sort(key=lambda x: x['popularity'])
    elif sort == '-rating':
        items.sort(key=lambda x: x['rating'] or 0, reverse=True)
    elif sort == 'title':
        items.sort(key=lambda x: x['title'].lower())
    elif sort == '-title':
        items.sort(key=lambda x: x['title'].lower(), reverse=True)

    total_movies = TMDBMovie.objects.count()
    total_tv = TMDBTV.objects.count()

    paginator = Paginator(items, 24)
    page = request.GET.get('page', 1)
    try:
        items = paginator.page(page)
    except Exception:
        items = paginator.page(1)

    return render(request, 'core/admin_content_list.html', {
        'items': items,
        'content_type': content_type,
        'search': search,
        'sort': sort,
        'total_movies': total_movies,
        'total_tv': total_tv,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_content_add(request):
    """Add a new movie or TV show directly."""
    from .models import TMDBMovie, TMDBTV, TMDBGenre

    if request.method == 'POST':
        content_type = request.POST.get('content_type', 'movie')
        title = request.POST.get('title', '').strip()
        if not title:
            messages.error(request, 'Title is required.')
            return render(request, 'core/admin_content_form.html', {
                'editing': False, 'content_type': content_type,
            })

        # Generate a unique negative ID to avoid TMDB ID conflicts
        import time
        custom_id = -int(time.time() * 1000) % 2147483647

        if content_type == 'tv':
            obj = TMDBTV.objects.create(
                id=custom_id,
                name=title,
                original_name=request.POST.get('original_name', title),
                overview=request.POST.get('overview', ''),
                poster_path=request.POST.get('poster_path', ''),
                backdrop_path=request.POST.get('backdrop_path', ''),
                first_air_date=request.POST.get('release_date', ''),
                genres=_parse_genres(request.POST.get('genres', '')),
                vote_average=_safe_float(request.POST.get('vote_average', '0')),
                vote_count=_safe_int(request.POST.get('vote_count', '0')),
                popularity=_safe_float(request.POST.get('popularity', '0')),
                status=request.POST.get('status', 'Returning Series'),
                tagline=request.POST.get('tagline', ''),
                homepage=request.POST.get('homepage', ''),
                original_language=request.POST.get('original_language', 'en'),
                number_of_seasons=_safe_int(request.POST.get('number_of_seasons', '0')),
                number_of_episodes=_safe_int(request.POST.get('number_of_episodes', '0')),
                type=request.POST.get('show_type', 'Scripted'),
            )
            messages.success(request, f'TV show "{title}" created successfully.')
            return redirect('admin_content_edit', content_type='tv', content_id=obj.id)
        else:
            obj = TMDBMovie.objects.create(
                id=custom_id,
                title=title,
                original_title=request.POST.get('original_name', title),
                overview=request.POST.get('overview', ''),
                poster_path=request.POST.get('poster_path', ''),
                backdrop_path=request.POST.get('backdrop_path', ''),
                release_date=request.POST.get('release_date', ''),
                genres=_parse_genres(request.POST.get('genres', '')),
                vote_average=_safe_float(request.POST.get('vote_average', '0')),
                vote_count=_safe_int(request.POST.get('vote_count', '0')),
                popularity=_safe_float(request.POST.get('popularity', '0')),
                status=request.POST.get('status', 'Released'),
                tagline=request.POST.get('tagline', ''),
                homepage=request.POST.get('homepage', ''),
                original_language=request.POST.get('original_language', 'en'),
                runtime=_safe_int(request.POST.get('runtime', '0')),
                budget=_safe_int(request.POST.get('budget', '0')),
                revenue=_safe_int(request.POST.get('revenue', '0')),
            )
            messages.success(request, f'Movie "{title}" created successfully.')
            return redirect('admin_content_edit', content_type='movie', content_id=obj.id)

    content_type = request.GET.get('type', 'movie')
    return render(request, 'core/admin_content_form.html', {
        'editing': False, 'content_type': content_type,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_content_edit(request, content_type, content_id):
    """Edit an existing movie or TV show."""
    from .models import TMDBMovie, TMDBTV

    if content_type == 'tv':
        obj = get_object_or_404(TMDBTV, id=content_id)
    else:
        obj = get_object_or_404(TMDBMovie, id=content_id)

    if request.method == 'POST':
        if content_type == 'tv':
            obj.name = request.POST.get('name', obj.name)
            obj.original_name = request.POST.get('original_name', obj.original_name)
            obj.overview = request.POST.get('overview', obj.overview)
            obj.poster_path = request.POST.get('poster_path', obj.poster_path)
            obj.backdrop_path = request.POST.get('backdrop_path', obj.backdrop_path)
            obj.first_air_date = request.POST.get('release_date', obj.first_air_date)
            obj.genres = _parse_genres(request.POST.get('genres', ''))
            obj.vote_average = _safe_float(request.POST.get('vote_average', str(obj.vote_average)))
            obj.vote_count = _safe_int(request.POST.get('vote_count', str(obj.vote_count)))
            obj.popularity = _safe_float(request.POST.get('popularity', str(obj.popularity)))
            obj.status = request.POST.get('status', obj.status)
            obj.tagline = request.POST.get('tagline', obj.tagline)
            obj.homepage = request.POST.get('homepage', obj.homepage)
            obj.original_language = request.POST.get('original_language', obj.original_language)
            obj.number_of_seasons = _safe_int(request.POST.get('number_of_seasons', str(obj.number_of_seasons or 0)))
            obj.number_of_episodes = _safe_int(request.POST.get('number_of_episodes', str(obj.number_of_episodes or 0)))
            obj.type = request.POST.get('show_type', obj.type)
            obj.save()
            messages.success(request, f'TV show "{obj.name}" updated.')
        else:
            obj.title = request.POST.get('name', obj.title)
            obj.original_title = request.POST.get('original_name', obj.original_title)
            obj.overview = request.POST.get('overview', obj.overview)
            obj.poster_path = request.POST.get('poster_path', obj.poster_path)
            obj.backdrop_path = request.POST.get('backdrop_path', obj.backdrop_path)
            obj.release_date = request.POST.get('release_date', obj.release_date)
            obj.genres = _parse_genres(request.POST.get('genres', ''))
            obj.vote_average = _safe_float(request.POST.get('vote_average', str(obj.vote_average)))
            obj.vote_count = _safe_int(request.POST.get('vote_count', str(obj.vote_count)))
            obj.popularity = _safe_float(request.POST.get('popularity', str(obj.popularity)))
            obj.status = request.POST.get('status', obj.status)
            obj.tagline = request.POST.get('tagline', obj.tagline)
            obj.homepage = request.POST.get('homepage', obj.homepage)
            obj.original_language = request.POST.get('original_language', obj.original_language)
            obj.runtime = _safe_int(request.POST.get('runtime', str(obj.runtime or 0)))
            obj.budget = _safe_int(request.POST.get('budget', str(obj.budget or 0)))
            obj.revenue = _safe_int(request.POST.get('revenue', str(obj.revenue or 0)))
            obj.save()
            messages.success(request, f'Movie "{obj.title}" updated.')

        return redirect('admin_content_edit', content_type=content_type, content_id=obj.id)

    return render(request, 'core/admin_content_form.html', {
        'editing': True, 'obj': obj, 'content_type': content_type,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def admin_content_delete(request, content_type, content_id):
    """Delete a movie or TV show."""
    from .models import TMDBMovie, TMDBTV

    if content_type == 'tv':
        obj = get_object_or_404(TMDBTV, id=content_id)
        name = obj.name
        obj.delete()
    else:
        obj = get_object_or_404(TMDBMovie, id=content_id)
        name = obj.title
        obj.delete()

    messages.success(request, f'{content_type.upper()} "{name}" deleted.')
    return redirect('admin_content_list')


def _parse_genres(genre_str):
    """Parse comma-separated genre names into a JSON list."""
    if not genre_str:
        return []
    return [{'name': g.strip()} for g in genre_str.split(',') if g.strip()]


def _safe_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val):
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def _admin_cloud_entry(item):
    """Normalize a raw cloud-data entry (dict / int / string) so templates never
    crash on a missing key (e.g. dicts that have 'mediaId' but no 'id').
    Django >= 6 raises VariableDoesNotExist when a dict key used as a filter
    argument is absent, so every key referenced as one in admin_user_detail.html
    is guaranteed to exist here."""
    out = dict(item) if isinstance(item, dict) else {}
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        out['id'] = item
        out['mediaId'] = item
    if 'mediaId' not in out and 'id' in out:
        out['mediaId'] = out['id']
    if 'id' not in out and 'mediaId' in out:
        out['id'] = out['mediaId']
    if 'id' not in out and 'mediaId' not in out:
        tmdb = out.get('tmdbId')
        out['id'] = tmdb
        out['mediaId'] = tmdb
    # Filter-argument keys in the template: always present, so the
    # |default: filter never has to resolve a missing dict key.
    out.setdefault('positionMs', 0)
    out.setdefault('durationMs', 0)
    out.setdefault('lastUpdated', '')
    return out


def _admin_cloud_view(cloud):
    """Return a render-safe snapshot of a UserCloudData row for the admin UI.
    The Android app's JSON can contain dicts without an 'id' key (only
    'mediaId') or plain ids; Django templates raise on missing keys used as
    filter arguments, so every entry is normalized here before rendering."""
    if cloud is None:
        return None

    def _as_list(value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return list(value.values())
        if isinstance(value, (tuple, set)):
            return list(value)
        return []

    pb = cloud.playback_progress or {}
    if isinstance(pb, list):
        pb = {}
    playback = {k: _admin_cloud_entry(v) for k, v in pb.items()}

    return {
        'data_version': cloud.data_version,
        'last_synced_at': cloud.last_synced_at,
        'playback_progress': playback,
        'watchlist_ids': [_admin_cloud_entry(v) for v in _as_list(cloud.watchlist_ids)],
        'seen_keys': _as_list(cloud.seen_keys),
        'favorites': [_admin_cloud_entry(v) for v in _as_list(cloud.favorites)],
        'watch_history': [_admin_cloud_entry(v) for v in _as_list(cloud.watch_history)],
        'user_ratings': cloud.user_ratings or {},
    }


def _history_calendar_payload(user_ids, year, month):
    """Build the calendar payload: per-day counts + up to 200 recent items.

    Grouped by local day of the given month so the calendar grid can show
    how many titles each user watched per day.

    Takes a plain list of user ids — NOT a User queryset. PlayHistory lives
    on the external DB while auth.User stays on the default one; passing a
    queryset would inline `user_id IN (SELECT id FROM auth_user)` and run
    it against the external DB's stale user copy, silently dropping most
    rows. Resolving ids in Python keeps every query single-DB.
    """
    from datetime import timedelta
    start = timezone.make_aware(timezone.datetime(year, month, 1))
    end = (start + timedelta(days=32)).replace(day=1)
    qs = PlayHistory.objects.filter(user_id__in=user_ids, last_played_at__gte=start, last_played_at__lt=end)

    # Lazy backfill: fetch missing titles/posters from TMDB so the
    # calendar always shows display data.
    missing = qs.filter(
        models.Q(title='') | models.Q(title__isnull=True)
    ).exclude(tmdb_id__lte=0)[:20]
    if missing:
        from core.models import _backfill_from_tmdb
        _backfill_from_tmdb(list(missing))
        # Refresh the queryset so the loop picks up new data
        qs = PlayHistory.objects.filter(user_id__in=user_ids, last_played_at__gte=start, last_played_at__lt=end)

    days = {}
    items = []
    for h in qs.order_by('-last_played_at')[:400]:
        try:
            local_dt = timezone.localtime(h.last_played_at)
        except Exception:
            continue
        day = local_dt.day
        day_info = days.setdefault(day, {'count': 0, 'tv': 0, 'movie': 0})
        day_info['count'] += 1
        is_tv = h.media_type == 'tv' or h.season_number is not None
        if is_tv:
            day_info['tv'] += 1
        else:
            day_info['movie'] += 1
        if len(items) < 200:
            user_info = None
            if h.user_id:
                try:
                    user_info = {
                        'id': h.user_id,
                        'name': (h.user.first_name or h.user.username),
                        'email': h.user.email,
                    }
                except Exception:
                    user_info = {'id': h.user_id, 'name': '', 'email': ''}
            items.append({
                'id': h.id,
                'tmdbId': h.tmdb_id or 0,
                'title': h.title or '',
                'poster': h.poster_path or '',
                'mediaType': h.media_type or 'movie',
                'season': h.season_number,
                'episode': h.episode_number,
                'episodeTitle': h.episode_title or '',
                'isTv': is_tv,
                'ts': timezone.localtime(h.last_played_at).isoformat() if h.last_played_at else '',
                'time': local_dt.strftime('%I:%M %p'),
                'day': day,
                'user': user_info,
            })
    return {'days': days, 'items': items}


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_history_calendar(request):
    """Admin calendar dashboard of play history across all users."""
    try:
        now = timezone.localtime()
        try:
            year = int(request.GET.get('year', now.year))
            month = int(request.GET.get('month', now.month))
        except (TypeError, ValueError):
            year, month = now.year, now.month
        try:
            user_id = int(request.GET.get('user_id') or 0)
        except (TypeError, ValueError):
            user_id = 0

        if user_id > 0:
            user_qs = User.objects.filter(id=user_id)
        else:
            user_qs = User.objects.all()
        user_ids = list(user_qs.values_list('id', flat=True))

        # User dropdown: users that actually have play history. Resolve the
        # history ids first (they read from the external DB) so the User
        # query uses a plain id list — a queryset __in would run the
        # playhistory subquery against the wrong database.
        history_user_ids = list(PlayHistory.objects.values_list('user_id', flat=True).distinct())
        users = User.objects.filter(id__in=history_user_ids).order_by('username')
        payload = _history_calendar_payload(user_ids, year, month)
        payload['year'] = year
        payload['month'] = month
        payload['totalEvents'] = sum(d['count'] for d in payload['days'].values())
        return render(request, 'core/admin_history_calendar.html', {
            'users': users,
            'selected_user_id': user_id,
            'year': year,
            'month': month,
            'initial_data': json.dumps(payload),
            'total_events': payload['totalEvents'],
        })
    except Exception as e:
        logger.warning(f'Calendar view error: {e}', exc_info=True)
        return render(request, 'core/admin_history_calendar.html', {
            'users': User.objects.none(),
            'selected_user_id': 0,
            'year': timezone.localtime().year,
            'month': timezone.localtime().month,
            'initial_data': json.dumps({'days': {}, 'items': []}),
            'total_events': 0,
        })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_history_day(request):
    """Full-page list of everything watched on a specific day (all users or one)."""
    now = timezone.localtime()
    try:
        year = int(request.GET.get('year', now.year))
        month = int(request.GET.get('month', now.month))
        day = int(request.GET.get('day', now.day))
        user_id = int(request.GET.get('user_id') or 0)
    except (TypeError, ValueError):
        return redirect('admin_history_calendar')
    if user_id > 0:
        user_ids = list(User.objects.filter(id=user_id).values_list('id', flat=True))
        detail_user = User.objects.filter(id=user_id).first()
    else:
        user_ids = list(User.objects.values_list('id', flat=True))
        detail_user = None
    try:
        start = timezone.make_aware(timezone.datetime(year, month, day))
    except ValueError:
        return redirect('admin_history_calendar')
    from datetime import timedelta
    end = start + timedelta(days=1)
    items = PlayHistory.objects.filter(
        user_id__in=user_ids, last_played_at__gte=start, last_played_at__lt=end,
    ).order_by('-last_played_at')

    rows = []
    for h in items:
        try:
            local_dt = timezone.localtime(h.last_played_at)
        except Exception:
            continue
        try:
            user_obj = h.user
        except Exception:
            user_obj = None
        rows.append({
            'h': h,
            'time': local_dt.strftime('%I:%M %p'),
            'user': user_obj,
            'is_tv': h.media_type == 'tv' or h.season_number is not None,
        })
    total = len(rows)
    movies = sum(1 for r in rows if not (r['h'].media_type == 'tv' or r['h'].season_number))
    tv = total - movies
    users_active = len({r['h'].user_id for r in rows if r['h'].user_id})
    titles_unique = len({(r['h'].tmdb_id, r['h'].media_type, r['h'].season_number, r['h'].episode_number) for r in rows})
    return render(request, 'core/admin_history_day.html', {
        'rows': rows,
        'detail_user': detail_user,
        'year': year,
        'month': month,
        'day': day,
        'total': total,
        'movies': movies,
        'tv': tv,
        'users_active': users_active,
        'titles_unique': titles_unique,
        'date_label': timezone.make_aware(timezone.datetime(year, month, day)).strftime('%B %d, %Y'),
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_history_calendar_data(request):
    """JSON data endpoint used by the calendar page when navigating months/users."""
    now = timezone.localtime()
    try:
        year = int(request.GET.get('year', now.year))
        month = int(request.GET.get('month', now.month))
    except (TypeError, ValueError):
        year, month = now.year, now.month
    try:
        user_id = int(request.GET.get('user_id') or 0)
    except (TypeError, ValueError):
        user_id = 0
    if user_id > 0:
        user_ids = list(User.objects.filter(id=user_id).values_list('id', flat=True))
    else:
        user_ids = list(User.objects.values_list('id', flat=True))
    payload = _history_calendar_payload(user_ids, year, month)
    payload['year'] = year
    payload['month'] = month
    payload['totalEvents'] = sum(d['count'] for d in payload['days'].values())
    return JsonResponse(payload)


@login_required
@user_passes_test(is_staff_or_superuser)
@require_POST
def admin_user_resend_verification(request, user_id):
    """Admin: resend the verification code for a web user."""
    detail_user = get_object_or_404(User, id=user_id)
    ok, msg = _admin_resend_verification(detail_user, request)
    if ok:
        messages.success(request, msg)
    else:
        messages.error(request, msg)
    return redirect('admin_user_detail', user_id=detail_user.id)


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_detail(request, user_id):
    """Full user detail page with subscription, activity, cloud data, session history."""
    from .models import UserSession
    detail_user = get_object_or_404(User, id=user_id)
    synced_profile = SyncedUser.objects.filter(user=detail_user).first()
    if not synced_profile and detail_user.email:
        # Re-link the Android profile to this Django user by the same email.
        synced_profile = SyncedUser.objects.filter(email=detail_user.email).first()
        if synced_profile and synced_profile.user_id != detail_user.pk:
            synced_profile.user = detail_user
            synced_profile.save(update_fields=['user'])
    cloud_data = UserCloudData.objects.filter(user=detail_user).first()
    play_history = PlayHistory.objects.filter(user=detail_user).order_by('-last_played_at')[:50]
    sessions = UserSession.objects.filter(user=detail_user).order_by('-logged_in_at')[:50]
    total_logins = UserSession.objects.filter(user=detail_user).count()
    active_sessions = UserSession.objects.filter(user=detail_user, is_active=True).count()
    last_source = sessions.first().source if sessions else ''
    return render(request, 'core/admin_user_detail.html', {
        'detail_user': detail_user,
        'synced_profile': synced_profile,
        'cloud_data': _admin_cloud_view(cloud_data),
        'play_history': play_history,
        'sessions': sessions,
        'total_logins': total_logins,
        'active_sessions': active_sessions,
        'last_source': last_source,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_subscription(request, user_id):
    """Save subscription plan for a user."""
    detail_user = get_object_or_404(User, id=user_id)
    if request.method != 'POST':
        return redirect('admin_user_detail', user_id=user_id)

    is_subscribed = request.POST.get('is_subscribed') == 'on'
    plan = request.POST.get('plan', 'Standard Free')
    custom_plan = request.POST.get('custom_plan', '').strip()
    if plan == 'Custom' and custom_plan:
        plan = custom_plan
    valid_until_str = request.POST.get('valid_until', '')
    features_raw = request.POST.get('features', '')
    features = [f.strip() for f in features_raw.split('\n') if f.strip()]

    # Get or create SyncedUser profile for subscription
    synced_profile, created = SyncedUser.objects.get_or_create(
        user=detail_user,
        defaults={'email': detail_user.email, 'display_name': detail_user.first_name},
    )
    was_subscribed, was_plan, was_valid_until = (
        synced_profile.is_subscribed, synced_profile.plan, synced_profile.valid_until)
    synced_profile.is_subscribed = is_subscribed
    synced_profile.plan = plan
    synced_profile.features = features
    if valid_until_str:
        import datetime as _dt
        try:
            synced_profile.valid_until = _dt.date.fromisoformat(valid_until_str)
        except ValueError:
            pass
    elif not is_subscribed:
        synced_profile.valid_until = None
    synced_profile.save()
    _log_subscription_event(synced_profile, request.user.get_username(), 'detail-page',
                            {'is_subscribed': was_subscribed, 'plan': was_plan,
                             'valid_until': was_valid_until})

    messages.success(request, f'Subscription updated for {detail_user.username}')
    return redirect('admin_user_detail', user_id=user_id)


# ---------------------------------------------------------------------------
# Database Connection Management
# ---------------------------------------------------------------------------


def _get_routing_config():
    try:
        from .models import DBRoutingConfig
        return DBRoutingConfig.get_config()
    except Exception:
        from .models import DBRoutingConfig
        return DBRoutingConfig(use_external_db=False)


@login_required
@user_passes_test(is_staff_or_superuser)
def db_connection_settings(request):
    """Admin page to manage external database connections."""
    from .models import DBConnectionConfig

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'save':
            conn_id = request.POST.get('conn_id', '')
            name = request.POST.get('name', '').strip()
            db_type = request.POST.get('db_type', 'mysql')
            host = request.POST.get('host', '127.0.0.1').strip()
            port = int(request.POST.get('port', 3306) or 3306)
            database_name = request.POST.get('database_name', '').strip()
            username = request.POST.get('username', '').strip()
            password = request.POST.get('password', '').strip()
            notes = request.POST.get('notes', '').strip()
            is_default = request.POST.get('is_default') == 'on'

            # If setting as default, unset others
            if is_default:
                DBConnectionConfig.objects.filter(is_default=True).update(is_default=False)

            if conn_id:
                conn = get_object_or_404(DBConnectionConfig, pk=conn_id)
                # Only update password if provided (don't clear it)
                if password:
                    conn.password = password
                conn.name = name
                conn.db_type = db_type
                conn.host = host
                conn.port = port
                conn.database_name = database_name
                conn.username = username
                conn.notes = notes
                conn.is_default = is_default
                conn.is_active = True
                conn.save()
            else:
                if not password:
                    messages.error(request, 'Password is required for new connections.')
                    connections = DBConnectionConfig.objects.all()
                    routing_cfg = _get_routing_config()
                    return render(request, 'core/db_connection_settings.html', {
                        'connections': connections,
                        'routing': routing_cfg,
                        'active_conn': None,
                        'dbs': {},
                        'back_url': 'admin_dashboard',
                    })
                conn = DBConnectionConfig.objects.create(
                    name=name, db_type=db_type, host=host, port=port,
                    database_name=database_name, username=username,
                    password=password, notes=notes,
                    is_default=is_default, is_active=True,
                )
            messages.success(request, f'Connection "{name}" saved successfully.')
            return _settings_redirect(request, 'db_connection_settings')

        elif action == 'delete':
            conn_id = request.POST.get('conn_id', '')
            if conn_id:
                conn = get_object_or_404(DBConnectionConfig, pk=conn_id)
                conn.delete()
                messages.success(request, 'Connection deleted.')
            return _settings_redirect(request, 'db_connection_settings')

        elif action == 'toggle_active':
            conn_id = request.POST.get('conn_id', '')
            if conn_id:
                conn = get_object_or_404(DBConnectionConfig, pk=conn_id)
                conn.is_active = not conn.is_active
                conn.save()
                status = 'activated' if conn.is_active else 'deactivated'
                messages.success(request, f'Connection "{conn.name}" {status}.')
            return _settings_redirect(request, 'db_connection_settings')

    connections = DBConnectionConfig.objects.all()
    routing = _get_routing_config()
    active_conn = DBConnectionConfig.objects.filter(is_active=True, is_default=True).first()
    dbs = {}
    dbs['default'] = _get_db_stats('default', 'Local Database (SQLite)')
    if routing.use_external_db and active_conn:
        try:
            dbs['external'] = _get_db_stats('external', 'External Database')
        except Exception:
            pass
    return render(request, 'core/db_connection_settings.html', {
        'connections': connections,
        'routing': routing,
        'active_conn': active_conn,
        'dbs': dbs,
        'back_url': 'admin_dashboard',
    })


def _get_db_conn_stats(conn):
    """Live stats for a saved connection, fetched directly with its own credentials."""
    import time
    info = {
        'status': 'error', 'engine': conn.get_db_type_display(),
        'host': conn.host, 'port': conn.port, 'database': conn.database_name,
        'version': '', 'latency_ms': 0, 'db_size': '', 'uptime': '',
        'tables': [], 'table_count': 0, 'total_rows': 0,
    }
    try:
        if conn.db_type == 'mysql':
            import MySQLdb
            t0 = time.time()
            db = MySQLdb.connect(host=conn.host, port=conn.port, user=conn.username,
                                 passwd=conn.password, db=conn.database_name or None,
                                 connect_timeout=8, **conn.extra_params)
            info['latency_ms'] = round((time.time() - t0) * 1000)
            cur = db.cursor()
            cur.execute('SELECT VERSION()')
            info['version'] = cur.fetchone()[0]
            cur.execute('SHOW STATUS')
            status = {r[0]: r[1] for r in cur.fetchall()}
            info['uptime'] = _format_uptime(int(status.get('Uptime', 0)))
            info['connections'] = int(status.get('Threads_connected', 0))
            info['questions'] = int(status.get('Questions', 0))
            info['slow_queries'] = int(status.get('Slow_queries', 0))
            info['queries_per_sec'] = round(info['questions'] / max(1, int(status.get('Uptime', 1))), 1)
            info['bytes_sent'] = _format_bytes(int(status.get('Bytes_sent', 0)))
            info['bytes_received'] = _format_bytes(int(status.get('Bytes_received', 0)))
            info['aborted_connects'] = int(status.get('Aborted_connects', 0))
            info['max_used_connections'] = int(status.get('Max_used_connections', 0))
            dbname = conn.database_name or ''
            if dbname:
                cur.execute('SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) '
                            'FROM information_schema.tables WHERE table_schema = %s', (dbname,))
                row = cur.fetchone()
                info['db_size'] = f'{row[0]} MB' if row and row[0] else '0 MB'
                cur.execute('SELECT table_name, table_rows, '
                            'ROUND((data_length + index_length) / 1024, 1) AS size_kb '
                            'FROM information_schema.tables WHERE table_schema = %s '
                            'ORDER BY (data_length + index_length) DESC', (dbname,))
                for t_name, t_rows, t_size in cur.fetchall():
                    t_rows = t_rows or 0
                    info['total_rows'] += t_rows
                    info['tables'].append({
                        'name': t_name,
                        'rows': t_rows,
                        'size': f'{t_size} KB' if t_size else '0 KB',
                        'size_bytes': (t_size or 0) * 1024,
                    })
                info['table_count'] = len(info['tables'])
            cur.close()
            db.close()
            info['status'] = 'healthy'
        elif conn.db_type == 'postgresql':
            import psycopg2
            t0 = time.time()
            db = psycopg2.connect(host=conn.host, port=conn.port, user=conn.username,
                                  password=conn.password, dbname=conn.database_name or 'postgres',
                                  connect_timeout=8)
            info['latency_ms'] = round((time.time() - t0) * 1000)
            cur = db.cursor()
            cur.execute('SELECT version()')
            info['version'] = cur.fetchone()[0]
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' ORDER BY table_name")
            for (t_name,) in cur.fetchall():
                try:
                    cur.execute(f'SELECT COUNT(*) FROM "{t_name}"')
                    t_rows = cur.fetchone()[0]
                except Exception:
                    t_rows = 0
                info['total_rows'] += t_rows
                info['tables'].append({'name': t_name, 'rows': t_rows, 'size': '', 'size_bytes': 0})
            info['table_count'] = len(info['tables'])
            cur.close()
            db.close()
            info['status'] = 'healthy'
        elif conn.db_type == 'sqlite':
            import sqlite3
            t0 = time.time()
            db = sqlite3.connect(conn.database_name or ':memory:', timeout=8)
            info['latency_ms'] = round((time.time() - t0) * 1000)
            info['version'] = 'SQLite'
            cur = db.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")
            for (t_name,) in cur.fetchall():
                try:
                    cur.execute(f'SELECT COUNT(*) FROM "{t_name}"')
                    t_rows = cur.fetchone()[0]
                except Exception:
                    t_rows = 0
                info['total_rows'] += t_rows
                info['tables'].append({'name': t_name, 'rows': t_rows, 'size': '', 'size_bytes': 0})
            info['table_count'] = len(info['tables'])
            cur.close()
            db.close()
            info['status'] = 'healthy'
        else:
            info['error'] = 'Live stats are not supported for this database type.'
    except Exception as e:
        info['status'] = 'error'
        info['error'] = str(e)
    return info


@login_required
@user_passes_test(is_staff_or_superuser)
def db_connection_detail(request, pk):
    """Admin page with full details and live stats for one saved DB connection."""
    from .models import DBConnectionConfig

    conn = get_object_or_404(DBConnectionConfig, pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'delete':
            name = conn.name
            conn.delete()
            messages.success(request, f'Connection "{name}" deleted.')
            return _settings_redirect(request, 'db_connection_settings')
        if action == 'toggle_active':
            conn.is_active = not conn.is_active
            conn.save()
            state = 'activated' if conn.is_active else 'deactivated'
            messages.success(request, f'Connection "{conn.name}" {state}.')
            return redirect('db_connection_detail', pk=conn.pk)
        if action == 'test':
            success, message = conn.test_connection()
            if success:
                messages.success(request, f'Connection test successful: {message}')
            else:
                messages.error(request, f'Connection test failed: {message}')
            return redirect('db_connection_detail', pk=conn.pk)

    routing = _get_routing_config()
    stats = _get_db_conn_stats(conn)
    is_live_external = bool(routing.use_external_db and conn.is_active and conn.is_default)
    return render(request, 'core/db_connection_detail.html', {
        'conn': conn,
        'routing': routing,
        'stats': stats,
        'is_live_external': is_live_external,
    })


# ---------------------------------------------------------------- settings hub
# Thirteen single-purpose settings pages became three, each showing one panel at
# a time. The panels *are* those pages: the hub asks the existing view to render
# with ``_settings_embed`` set, which makes its template extend the bare
# fragment base instead of the admin shell, and then either returns that
# fragment (a tab switch) or drops it inside the hub's own page (first paint).
# Nothing about a settings view's behaviour changed, and every original URL
# still renders that page on its own.
# ---------------------------------------------------------------- settings overview

SETTINGS_OVERVIEW_DAYS = 7
SETTINGS_OVERVIEW_CACHE_KEY = 'settings_overview_v1'
SETTINGS_OVERVIEW_CACHE_SECONDS = 300


def _day_series(model, date_field, days=SETTINGS_OVERVIEW_DAYS, **filters):
    """Rows per local day for the last `days` days, zero-filled and chart-ready.

    The range is a datetime comparison, not ``__date``: a function on the column
    cannot use its index, which cost over a second per series on the live log
    tables. Only the day grouping runs over the seven days that remain.
    """
    today = timezone.localdate()
    start = today - datetime.timedelta(days=days - 1)
    start_at = timezone.make_aware(datetime.datetime.combine(start, datetime.time.min))
    rows = (model.objects.filter(**{date_field + '__gte': start_at}, **filters)
            .annotate(day=TruncDate(date_field, tzinfo=timezone.get_current_timezone()))
            .values('day').annotate(total=Count('id')))
    counts = {row['day']: row['total'] for row in rows}
    series = []
    for i in range(days):
        day = start + datetime.timedelta(days=i)
        series.append({'day': day, 'count': counts.get(day, 0)})
    peak = max([point['count'] for point in series] + [1])
    for point in series:
        point['pct'] = round(point['count'] * 100.0 / peak, 1)
    return series


def _settings_health():
    """One row per settings area: a value it holds, or the value it is missing.

    Read-only: it never prints a secret, only whether one is set, so the
    overview can be shared on a screen without leaking a key or a password.
    """
    ss = SiteSettings.get_settings()

    def hub(group, tab):
        return reverse('settings_hub', args=[group]) + '?tab=' + tab

    def state(value):
        return 'ok' if value else 'warn'

    active_keys = TMDBApiKey.objects.filter(is_active=True).count()
    allowed = [ip for ip in (ss.allowed_ips or '').replace(',', ' ').split() if ip]
    rows = [
        {'group': 'Site', 'label': 'General', 'url': hub('site', 'general'),
         'detail': ('Analytics ' + ss.google_analytics_id) if ss.google_analytics_id else 'No analytics ID',
         'state': state(ss.google_analytics_id)},
        {'group': 'Site', 'label': 'Branding', 'url': hub('site', 'branding'),
         'detail': ss.brand_name or 'No brand name', 'state': state(ss.brand_name)},
        {'group': 'Data & APIs', 'label': 'Data source', 'url': hub('data', 'source'),
         'detail': ss.get_data_source_display(), 'state': 'ok'},
        {'group': 'Data & APIs', 'label': 'API keys', 'url': hub('data', 'keys'),
         'detail': f'{active_keys} active key{"" if active_keys == 1 else "s"}',
         'state': state(active_keys)},
        {'group': 'Data & APIs', 'label': 'TMDB database', 'url': hub('data', 'tmdb'),
         'detail': ss.tmdb_db_host or 'No host set', 'state': state(ss.tmdb_db_host)},
        {'group': 'Delivery', 'label': 'Email', 'url': hub('delivery', 'email'),
         'detail': ss.email_host_user or 'No sender address', 'state': state(ss.email_host_user)},
        {'group': 'Delivery', 'label': 'Player', 'url': hub('delivery', 'player'),
         'detail': ss.active_movie_player or 'No player chosen', 'state': state(ss.active_movie_player)},
        {'group': 'Security', 'label': 'Protection', 'url': hub('security', 'protection'),
         'detail': (f'{len(allowed)} trusted address{"" if len(allowed) == 1 else "es"}'
                    if allowed else 'No trusted addresses'),
         'state': state(allowed)},
        {'group': 'Security', 'label': 'URL blocking', 'url': hub('security', 'blocking'),
         'detail': 'Blocking on' if ss.enable_url_blocking else 'Blocking off',
         'state': 'ok'},
    ]
    return rows


def _new_error_views(hours=24):
    """View names failing now that were not failing in the previous week."""
    from .models import ErrorLog

    now = timezone.now()
    recent = set(ErrorLog.objects.filter(created_at__gte=now - timezone.timedelta(hours=hours))
                 .values_list('view_name', flat=True).distinct())
    before = set(ErrorLog.objects.filter(
        created_at__lt=now - timezone.timedelta(hours=hours),
        created_at__gte=now - timezone.timedelta(days=7))
        .values_list('view_name', flat=True).distinct())
    return len(recent - before)


def _overview_context():
    """Everything the overview shows, built once so it can be cached whole.

    ``DataSourceUsageLog`` holds hundreds of thousands of rows, and summing
    them is the one expensive read on this page, so the result is held for
    ``SETTINGS_OVERVIEW_CACHE_SECONDS`` rather than paid on every open.
    """
    from .models import (TMDBMovie, TMDBTV, Subscriber, BlockedIP, TMDBApiKey,
                         DataSourceUsageLog, UserPageView, RequestLog, ErrorLog,
                         UserHeartbeat)

    page_series = _day_series(UserPageView, 'viewed_at')
    request_series = _day_series(RequestLog, 'created_at')
    blocked_series = _day_series(RequestLog, 'created_at', status_code__gte=400)

    week_start = timezone.localdate() - datetime.timedelta(days=SETTINGS_OVERVIEW_DAYS - 1)
    week_start_at = timezone.make_aware(datetime.datetime.combine(week_start, datetime.time.min))
    mix_rows = (DataSourceUsageLog.objects.filter(created_at__gte=week_start_at)
                .values('source').annotate(total=Sum('usage_count')).order_by('-total'))
    mix_total = sum(row['total'] or 0 for row in mix_rows) or 1
    mix = [{'label': dict(DataSourceUsageLog.SOURCE_CHOICES).get(row['source'], row['source']),
            'count': row['total'] or 0,
            'pct': round((row['total'] or 0) * 100.0 / mix_total, 1)} for row in mix_rows]

    week_views = sum(point['count'] for point in page_series)
    week_requests = sum(point['count'] for point in request_series)
    week_errors = sum(point['count'] for point in blocked_series)

    # One row per day so the template can show requests and errors together.
    traffic_series = [
        {'day': request_point['day'],
         'requests': request_point['count'], 'req_pct': request_point['pct'],
         'errors': error_point['count'], 'err_pct': error_point['pct']}
        for request_point, error_point in zip(request_series, blocked_series)
    ]

    return {
        'health': _settings_health(),
        'page_series': page_series,
        'request_series': request_series,
        'error_series': blocked_series,
        'traffic_series': traffic_series,
        'mix': mix,
        'days': SETTINGS_OVERVIEW_DAYS,
        'week_views': week_views,
        'week_requests': week_requests,
        'week_errors': week_errors,
        'tiles': [
            {'label': 'Movies', 'value': TMDBMovie.objects.count(), 'icon': 'fa-film'},
            {'label': 'Series', 'value': TMDBTV.objects.count(), 'icon': 'fa-tv'},
            {'label': 'Page views / 7d', 'value': week_views, 'icon': 'fa-eye'},
            {'label': 'Requests / 7d', 'value': week_requests, 'icon': 'fa-bolt'},
            {'label': 'Errors / 7d', 'value': week_errors, 'icon': 'fa-triangle-exclamation'},
            {'label': 'Active blocks', 'value': BlockedIP.objects.filter(is_active=True).count(), 'icon': 'fa-ban'},
            {'label': 'Active API keys', 'value': TMDBApiKey.objects.filter(is_active=True).count(), 'icon': 'fa-key'},
            {'label': 'Subscribers', 'value': Subscriber.objects.count(), 'icon': 'fa-envelope'},
            {'label': 'Silent mid-play',
             'value': UserHeartbeat.objects.filter(
                 is_playing=True,
                 last_seen_at__lt=timezone.now() - timezone.timedelta(minutes=30),
                 last_seen_at__gte=timezone.now() - timezone.timedelta(hours=24)).count(),
             'icon': 'fa-plug-circle-xmark'},
            {'label': 'New error types / 24h', 'value': _new_error_views(), 'icon': 'fa-bug'},
        ],
    }


@login_required
@user_passes_test(is_staff_or_superuser)
@cache_control(private=True, no_store=True)
def settings_overview(request):
    """The settings dashboard: configuration health, traffic and source mix.

    Every number here is read from the tables that already record it, so this
    page duplicates no setting and no count that another page owns; it links to
    the page that owns each one instead.
    """
    stats = cache.get(SETTINGS_OVERVIEW_CACHE_KEY)
    if stats is None:
        stats = _overview_context()
        cache.set(SETTINGS_OVERVIEW_CACHE_KEY, stats, SETTINGS_OVERVIEW_CACHE_SECONDS)
    return render(request, 'core/settings_overview.html', stats)


SETTINGS_HUB = {
    'overview': {
        'title': 'Settings',
        'subtitle': 'The whole server in one place: what is configured, what it is '
                    'doing this week, and what needs a look.',
        'tabs': [
            ('health', 'Overview', 'Health, traffic and data-source use', 'settings_overview'),
        ],
    },
    'site': {
        'title': 'Site',
        'subtitle': 'Identity, measurements, branding and the footer. Every value '
                    'here is owned by exactly one panel.',
        'tabs': [
            ('general', 'General', 'Identity, analytics, Google sign-in and ads', 'general_settings'),
            ('branding', 'Branding', 'Brand name, tagline and colour', 'branding_settings'),
            ('display', 'Display', 'Items per row, card size, list length', 'display_settings'),
            ('footer', 'Footer', 'Footer sections, links and copyright', 'footer_settings'),
        ],
    },
    'data': {
        'title': 'Data & APIs',
        'subtitle': 'Where content comes from, which database serves it, and what '
                    'that is costing.',
        'tabs': [
            ('source', 'Data source', 'TMDB API or the local database', 'data_source_settings'),
            ('tmdb', 'TMDB database', 'Connection for the local TMDB mirror', 'tmdb_db_settings'),
            ('keys', 'API keys', 'Keys in use, their state and last use', 'data_source_config'),
            ('usage', 'Usage', 'Which source answered, and how often', 'data_source_usage_stats'),
            ('db', 'DB connections', 'External database connections', 'db_connection_settings'),
            ('routing', 'DB routing', 'Which database holds which data', 'db_routing_settings'),
        ],
    },
    'delivery': {
        'title': 'Delivery',
        'subtitle': 'How pages reach a visitor: the player and the mail settings.',
        'tabs': [
            ('player', 'Player', 'Default movie and TV player', 'player_settings'),
            ('email', 'Email', 'SMTP addresses, purposes and templates', 'email_settings'),
        ],
    },
    'security': {
        'title': 'Security',
        'subtitle': 'Who is never blocked, who is treated as a bot, and what the '
                    'site refuses to serve.',
        'tabs': [
            ('protection', 'Protection', 'Trusted addresses, bot signatures, DevTools rule', 'security_protection_settings'),
            ('blocking', 'URL blocking', 'Blocked URLs and redirects', 'url_blocking_settings'),
        ],
    },
}


def _settings_redirect(request, fallback):
    """Send a saved settings form back to the tab it came from.

    A panel in a hub knows its own URL, so a save there returns to that tab
    instead of dumping the admin on the dashboard. Forms also carry a hidden
    ``next`` field, which covers a panel posted straight to its own view.
    Only ever redirects to a path on this site.
    """
    target = getattr(request, '_settings_hub', '') or ''
    if not target:
        target = (request.POST.get('next') or request.GET.get('next') or '')
    # Same site only: browsers turn "\\" into "/", so /\evil.example is an
    # off-site target even though it starts with a single slash.
    if (target.startswith('/') and not target.startswith('//')
            and '\\' not in target and not any(ch in target for ch in '\r\n\t')):
        return redirect(target)
    return redirect(fallback)


@login_required
@user_passes_test(is_staff_or_superuser)
def settings_root(request):
    """Bare /settings/ opens the overview, which links to every group."""
    return redirect(reverse('settings_hub', args=['overview']))


@login_required
@user_passes_test(is_staff_or_superuser)
@cache_control(private=True, no_store=True)
def settings_hub(request, group):
    """One settings group, one panel at a time.

    ``?tab=`` picks the panel and ``?partial=1`` asks for just that panel, which
    is what the tab strip fetches — the same fragment swap the users page uses,
    so switching tabs keeps the page, its scroll and its state.
    """
    conf = SETTINGS_HUB.get(group)
    if conf is None:
        raise Http404('Unknown settings group')

    tabs = conf['tabs']
    # A panel's form posts to the hub path, which carries no query string, so the
    # panel also sends its own key; without that a save on the third tab would
    # be handed to the first tab's form.
    active = (request.GET.get('tab') or request.POST.get('tab')
              or tabs[0][0]).strip().lower()
    tab = next((t for t in tabs if t[0] == active), tabs[0])
    key, label, hint, view_name = tab

    hub_path = reverse('settings_hub', args=[group])
    tab_url = hub_path + '?tab=' + key

    target_view = globals().get(view_name)
    if target_view is None:
        raise Http404('That settings page is missing')

    # Ask the existing page to render as a bare panel, and to come back here
    # after a save.
    request._settings_embed = True
    request._settings_hub = tab_url
    response = target_view(request)

    if getattr(response, 'status_code', 200) in (301, 302, 303, 307, 308):
        return response      # a save already decided where to go
    if request.GET.get('partial') == '1':
        return response

    from django.utils.safestring import mark_safe
    body = response.content.decode('utf-8', 'replace') if hasattr(response, 'content') else ''
    return render(request, 'core/settings_hub.html', {
        'group': group,
        'group_title': conf['title'],
        'group_subtitle': conf.get('subtitle', ''),
        'hub_url': hub_path,
        'tabs': [
            {'key': t[0], 'label': t[1], 'hint': t[2],
             'url': hub_path + '?tab=' + t[0]}
            for t in tabs
        ],
        'active_key': key,
        'active_label': label,
        'active_hint': hint,
        'tab_url': tab_url,
        'panel': mark_safe(body),
    })



@login_required
@user_passes_test(is_staff_or_superuser)
def ajax_test_db_connection(request):
    """AJAX endpoint to test a DB connection by ID or inline params."""
    from .models import DBConnectionConfig
    import json

    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        data = {}

    conn_id = data.get('conn_id') or request.POST.get('conn_id', '')

    if conn_id:
        try:
            conn = DBConnectionConfig.objects.get(pk=conn_id)
        except DBConnectionConfig.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Connection not found.'})
    else:
        # Test with inline params (no save)
        db_type = data.get('db_type', request.POST.get('db_type', 'mysql'))
        host = data.get('host', request.POST.get('host', '127.0.0.1'))
        port = int(data.get('port', request.POST.get('port', 3306)) or 3306)
        database_name = data.get('database_name', request.POST.get('database_name', ''))
        username = data.get('username', request.POST.get('username', ''))
        password = data.get('password', request.POST.get('password', ''))
        conn = DBConnectionConfig(
            name='Test', db_type=db_type, host=host, port=port,
            database_name=database_name, username=username, password=password,
        )

    success, message = conn.test_connection()
    return JsonResponse({
        'success': success,
        'message': message,
        'status': conn.last_test_status,
        'tested_at': conn.last_tested_at.strftime('%Y-%m-%d %H:%M:%S') if conn.last_tested_at else None,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def ajax_write_db_config(request):
    """AJAX endpoint to write server_config.py for the active connection."""
    from .models import DBConnectionConfig

    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})

    conn_id = request.POST.get('conn_id', '')
    if not conn_id:
        return JsonResponse({'success': False, 'message': 'No connection ID provided.'})

    try:
        conn = DBConnectionConfig.objects.get(pk=conn_id)
    except DBConnectionConfig.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Connection not found.'})

    try:
        path = conn.write_server_config()
        return JsonResponse({
            'success': True,
            'message': f'Server config written to: {path}',
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': f'Failed to write config: {e}',
        })


@login_required
@user_passes_test(is_staff_or_superuser)
def db_routing_settings(request):
    """Admin page to control where user data is stored (local vs external DB)."""
    from .models import DBRoutingConfig, DBConnectionConfig
    from django.utils import timezone as tz

    routing = DBRoutingConfig.get_config()
    active_conn = DBConnectionConfig.objects.filter(is_active=True, is_default=True).first()

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'toggle_external':
            routing.use_external_db = not routing.use_external_db
            if routing.use_external_db and not active_conn:
                messages.error(request, 'No active default connection found. Configure one in Database Connections first.')
                routing.use_external_db = False
            routing.save()
            status = 'ENABLED' if routing.use_external_db else 'DISABLED'
            messages.success(request, f'External DB routing {status}. Restart server for changes to take full effect.')
            return _settings_redirect(request, 'db_routing_settings')

        elif action == 'migrate_data':
            direction = request.POST.get('direction', '')  # 'to_external' or 'to_local'
            try:
                result = _migrate_user_data(direction, routing)
                routing.last_migrated_at = tz.now()
                routing.last_migration_status = 'success'
                routing.last_migration_message = result
                routing.save(update_fields=['last_migrated_at', 'last_migration_status', 'last_migration_message'])
                messages.success(request, f'Data migration complete: {result}')
            except Exception as e:
                routing.last_migrated_at = tz.now()
                routing.last_migration_status = 'failed'
                routing.last_migration_message = str(e)
                routing.save(update_fields=['last_migrated_at', 'last_migration_status', 'last_migration_message'])
                messages.error(request, f'Migration failed: {e}')
            return _settings_redirect(request, 'db_routing_settings')

    # Get counts from both databases
    from django.db import connections
    from django.contrib.auth.models import User
    from .models import WatchList, PlayHistory, UserCloudData, SyncedUser, UserSession

    local_user_count = User.objects.using('default').count()
    external_user_count = 0

    try:
        if routing.use_external_db and routing.external_db_ready and 'external' in connections:
            external_user_count = User.objects.using('external').count()
    except Exception:
        pass

    # Count user-related data on default DB
    local_data = {
        'watchlist': WatchList.objects.using('default').count(),
        'play_history': PlayHistory.objects.using('default').count(),
        'cloud_data': UserCloudData.objects.using('default').count(),
        'synced_users': SyncedUser.objects.using('default').count(),
        'sessions': UserSession.objects.using('default').count(),
    }
    external_data = {}
    try:
        if routing.use_external_db and routing.external_db_ready and 'external' in connections:
            external_data = {
                'watchlist': WatchList.objects.using('external').count(),
                'play_history': PlayHistory.objects.using('external').count(),
                'cloud_data': UserCloudData.objects.using('external').count(),
                'synced_users': SyncedUser.objects.using('external').count(),
                'sessions': UserSession.objects.using('external').count(),
            }
    except Exception:
        pass

    return render(request, 'core/db_routing_settings.html', {
        'routing': routing,
        'active_conn': active_conn,
        'local_user_count': local_user_count,
        'external_user_count': external_user_count,
        'local_data': local_data,
        'external_data': external_data,
        'back_url': 'admin_dashboard',
    })


def _migrate_user_data(direction, routing):
    """Migrate user data between local and external databases.
    direction: 'to_external' or 'to_local'
    Returns a summary string."""
    from django.db import connections
    from django.contrib.auth.models import User
    from .models import (
        SyncedUser, UserSession, PlayHistory, UserCloudData,
        EmailVerification, WatchList, UserActivity, EmailSendLog,
    )

    source = 'default' if direction == 'to_external' else 'external'
    target = 'external' if direction == 'to_external' else 'default'

    # Verify both DBs are accessible
    for db_name in [source, target]:
        if db_name not in connections:
            raise Exception(f'Database "{db_name}" is not configured. Save a default connection first.')
        try:
            connections[db_name].ensure_connection()
        except Exception as e:
            raise Exception(f'Cannot connect to {db_name} database: {e}')

    counts = {}
    model_map = [
        ('Users', User),
        ('Watchlist', WatchList),
        ('Play History', PlayHistory),
        ('Cloud Data', UserCloudData),
        ('Synced Users', SyncedUser),
        ('Sessions', UserSession),
        ('Activities', UserActivity),
        ('Verifications', EmailVerification),
    ]

    for label, model in model_map:
        try:
            source_qs = model.objects.using(source)
            target_qs = model.objects.using(target)
            source_count = source_qs.count()
            migrated = 0

            for obj in source_qs.iterator():
                # Check if already exists in target (by pk)
                if not target_qs.filter(pk=obj.pk).exists():
                    obj.save(using=target)
                    migrated += 1

            counts[label] = f'{migrated} migrated ({source_count} total)'
        except Exception as e:
            counts[label] = f'Error: {e}'

    summary = '; '.join(f'{k}: {v}' for k, v in counts.items())
    return summary or 'No data to migrate'


# ---------------------------------------------------------------------------
# Database Dashboard
# ---------------------------------------------------------------------------


@login_required
@user_passes_test(is_staff_or_superuser)
def db_dashboard(request):
    """Database dashboard showing health, size, tables, and stats for all DBs."""
    import time, os, platform
    from django.db import connections
    from django.contrib.auth.models import User
    from .models import (
        WatchList, PlayHistory, UserCloudData, SyncedUser, UserSession,
        DBConnectionConfig, DBRoutingConfig,
    )

    dbs = {}
    routing = DBRoutingConfig.get_config()

    # --- DEFAULT DB (Local SQLite) ---
    local_info = _get_db_stats('default', 'Local Database (SQLite)')
    dbs['default'] = local_info

    # --- EXTERNAL DB (MySQL/Oracle if configured) ---
    if routing.use_external_db and routing.external_db_ready:
        try:
            ext_info = _get_db_stats('external', 'External Database')
            dbs['external'] = ext_info
        except Exception as e:
            dbs['external'] = {
                'name': 'External Database',
                'status': 'error',
                'error': str(e),
                'engine': '',
                'host': '',
                'tables': [],
                'total_rows': 0,
                'total_size': 'N/A',
                'db_size': 'N/A',
                'uptime': 'N/A',
                'version': 'N/A',
                'connections': 0,
                'questions': 0,
            }
    else:
        dbs['external'] = {
            'name': 'External Database',
            'status': 'disabled',
            'engine': '',
            'host': '',
            'tables': [],
            'total_rows': 0,
            'total_size': 'N/A',
            'db_size': 'N/A',
            'uptime': 'N/A',
            'version': 'N/A',
            'connections': 0,
            'questions': 0,
        }

    # Active connection config
    active_conn = DBConnectionConfig.objects.filter(is_active=True, is_default=True).first()

    # System info
    try:
        import psutil
        disk = psutil.disk_usage('/')
        system_info = {
            'disk_total': _format_bytes(disk.total),
            'disk_used': _format_bytes(disk.used),
            'disk_free': _format_bytes(disk.free),
            'disk_percent': disk.percent,
            'cpu_percent': psutil.cpu_percent(interval=0.5),
            'memory_total': _format_bytes(psutil.virtual_memory().total),
            'memory_used': _format_bytes(psutil.virtual_memory().used),
            'memory_percent': psutil.virtual_memory().percent,
        }
    except Exception:
        system_info = {}

    return render(request, 'core/db_dashboard.html', {
        'dbs': dbs,
        'routing': routing,
        'active_conn': active_conn,
        'system_info': system_info,
        'back_url': 'admin_dashboard',
    })


def _get_db_stats(db_alias, display_name):
    """Collect stats for a given database alias."""
    from django.db import connections
    import time

    info = {
        'name': display_name,
        'status': 'error',
        'engine': '',
        'host': '',
        'version': '',
        'uptime': '',
        'db_size': '',
        'total_size': '',
        'connections': 0,
        'questions': 0,
        'tables': [],
        'total_rows': 0,
    }

    try:
        conn = connections[db_alias]
        t0 = time.time()
        conn.ensure_connection()
        latency = round((time.time() - t0) * 1000)
        info['status'] = 'healthy'
        info['latency_ms'] = latency
        cursor = conn.cursor()

        engine = conn.settings_dict.get('ENGINE', '')
        info['engine'] = engine.split('.')[-1] if engine else 'unknown'
        info['host'] = conn.settings_dict.get('HOST', '')

        if 'mysql' in engine:
            # MySQL stats
            cursor.execute('SELECT VERSION()')
            info['version'] = cursor.fetchone()[0]

            cursor.execute('SHOW STATUS')
            status = {row[0]: row[1] for row in cursor.fetchall()}
            info['uptime'] = _format_uptime(int(status.get('Uptime', 0)))
            info['connections'] = int(status.get('Threads_connected', 0))
            info['questions'] = int(status.get('Questions', 0))
            info['slow_queries'] = int(status.get('Slow_queries', 0))
            info['queries_per_sec'] = round(
                info['questions'] / max(1, int(status.get('Uptime', 1))), 1
            )
            info['bytes_sent'] = _format_bytes(int(status.get('Bytes_sent', 0)))
            info['bytes_received'] = _format_bytes(int(status.get('Bytes_received', 0)))
            info['aborted_connects'] = int(status.get('Aborted_connects', 0))
            info['aborted_clients'] = int(status.get('Aborted_clients', 0))
            info['max_connections'] = int(status.get('Max_used_connections', 0))

            # DB size
            db_name = conn.settings_dict.get('NAME', '')
            if db_name:
                cursor.execute(
                    'SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) '
                    'FROM information_schema.tables WHERE table_schema = %s',
                    (db_name,)
                )
                row = cursor.fetchone()
                info['db_size'] = f'{row[0]} MB' if row and row[0] else '0 MB'

                # Total disk size
                cursor.execute(
                    'SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) '
                    'FROM information_schema.tables'
                )
                row = cursor.fetchone()
                info['total_size'] = f'{row[0]} MB' if row and row[0] else '0 MB'

            # Table list
            cursor.execute(
                'SELECT table_name, table_rows, '
                'ROUND((data_length + index_length) / 1024, 1) as size_kb '
                'FROM information_schema.tables '
                'WHERE table_schema = %s ORDER BY (data_length + index_length) DESC',
                (db_name,)
            )
            tables = []
            total_rows = 0
            for row in cursor.fetchall():
                t_name, t_rows, t_size = row
                t_rows = t_rows or 0
                total_rows += t_rows
                tables.append({
                    'name': t_name,
                    'rows': t_rows,
                    'size': f'{t_size} KB' if t_size else '0 KB',
                    'size_bytes': (t_size or 0) * 1024,
                })
            info['tables'] = tables
            info['total_rows'] = total_rows
            info['table_count'] = len(tables)

        elif 'sqlite' in engine:
            # SQLite stats
            info['version'] = 'SQLite'
            db_path = conn.settings_dict.get('NAME', '')
            if db_path and os.path.exists(db_path):
                db_size = os.path.getsize(db_path)
                info['db_size'] = _format_bytes(db_size)
                info['total_size'] = info['db_size']
            info['uptime'] = 'N/A'
            info['latency_ms'] = latency

            # Table list
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = []
            total_rows = 0
            for row in cursor.fetchall():
                t_name = row[0]
                try:
                    cursor.execute(f'SELECT COUNT(*) FROM "{t_name}"')
                    t_rows = cursor.fetchone()[0]
                except Exception:
                    t_rows = 0
                total_rows += t_rows
                tables.append({
                    'name': t_name,
                    'rows': t_rows,
                    'size': '',
                    'size_bytes': 0,
                })
            info['tables'] = tables
            info['total_rows'] = total_rows
            info['table_count'] = len(tables)

    except Exception as e:
        info['status'] = 'error'
        info['error'] = str(e)

    return info


def _format_bytes(n):
    """Format bytes to human readable."""
    if not n or n == 0:
        return '0 B'
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    n = float(n)
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f'{n:.1f} {units[i]}'


def _format_uptime(seconds):
    """Format seconds to uptime string."""
    if not seconds:
        return 'N/A'
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    mins = (seconds % 3600) // 60
    if days > 0:
        return f'{days}d {hours}h {mins}m'
    elif hours > 0:
        return f'{hours}h {mins}m'
    return f'{mins}m'


# ---------------------------------------------------------------------------
# Traffic Control, audit log, per-app settings and health alerts.
# ---------------------------------------------------------------------------


def _admin_int(value, default=0, low=0, high=1000000):
    """An int from a POST field, clamped, falling back to ``default``."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@login_required
@user_passes_test(is_staff_or_superuser)
def traffic_control(request):
    """One page for everything that answers "who is hitting us and what is blocked".

    The dashboard used to spread this over three pages — Blocked IPs, Blocked
    Headers and Probes & Bots — which answered the same question separately.
    Here the three sit together: the bot profile as a checklist you can switch
    per program, the blocked addresses, the probes, and one queue of every
    active block with a single Lift button each. The actions still post to the
    existing endpoints (``ip_block_action``, ``header_block_action``), so the
    history and validation of each are unchanged.
    """
    from .header_block import BOT_BLOCK_PROFILE, blocked_header_report
    from .health import five_xx_rate
    from .ip_block import allowed_ip_list, blocked_ip_report, client_ip_of
    from .models import BlockedHeader
    from .probes import probe_report
    from .request_log import request_total

    rule_ids = {r.value.lower(): r.id for r in BlockedHeader.objects.filter(is_active=True)}

    groups, by_name = [], {}
    for token, label in BOT_BLOCK_PROFILE:
        category = (label.split(':')[0] or 'other').strip().title()
        if category not in by_name:
            by_name[category] = {'name': category, 'items': []}
            groups.append(by_name[category])
        by_name[category]['items'].append({
            'token': token, 'label': label,
            'on': token.lower() in rule_ids,
            'rule_id': rule_ids.get(token.lower(), 0),
        })

    ip_rows, ip_totals = blocked_ip_report()
    header_rows, header_totals = blocked_header_report()
    probe_rows, probe_totals, top_paths = probe_report(hours=24, min_hits=1)

    queue = []
    for row in ip_rows:
        queue.append({'kind': 'ip', 'target': row['ip'], 'reason': row['reason'],
                      'since': row['blocked_at'], 'by': row['blocked_by']})
    for row in header_rows:
        queue.append({
            'kind': 'header',
            'rule_id': row['id'],
            'target': '%s: %s' % (row['header_name'], row['value']),
            'reason': row['reason'], 'since': row['blocked_at'],
            'by': row['blocked_by'],
        })
    queue.sort(key=lambda entry: entry['since'] or timezone.now(), reverse=True)

    pct5, bad5, total5 = five_xx_rate(60)
    trusted = allowed_ip_list()
    return render(request, 'core/admin_traffic_control.html', {
        'bot_groups': groups,
        'bot_on': sum(1 for g in groups for i in g['items'] if i['on']),
        'bot_total': len(BOT_BLOCK_PROFILE),
        'ip_rows': ip_rows,
        'ip_totals': ip_totals,
        'header_rows': header_rows[:80],
        'header_totals': header_totals,
        'probe_rows': probe_rows[:40],
        'probe_totals': probe_totals,
        'top_paths': top_paths[:8],
        'queue': queue[:150],
        'five_xx_pct': pct5,
        'five_xx_bad': bad5,
        'five_xx_total': total5,
        'total_stored': request_total(),
        'trusted_ips': trusted,
        'request_ip': client_ip_of(request),
        'request_ip_trusted': bool(client_ip_of(request)) and client_ip_of(request) in trusted,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_audit_log(request):
    """Every admin action in one timeline: who, what, when, from where."""
    from django.core.paginator import Paginator
    from .audit import action_label, action_options, audit_report

    action = (request.GET.get('action') or '').strip()
    username = (request.GET.get('user') or '').strip()
    search = (request.GET.get('q') or '').strip()
    rows = audit_report(action=action, username=username, search=search)
    page = Paginator(rows, 100).get_page(request.GET.get('page') or 1)
    return render(request, 'core/admin_audit_log.html', {
        'page_obj': page,
        'rows': page.object_list,
        'action': action,
        'username': username,
        'q': search,
        'action_options': action_options(),
        'action_label': action_label,
        'total': len(rows),
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def android_app_settings(request, app_id):
    """This app's settings, in one place: retention, sync limits, version gate, ads.

    The same numbers existed before but were spread over the analytics page and
    a form that also edited the app's credentials. This page is only about the
    behaviour: how long data is kept, how hard the app may poll the sync
    endpoint, which builds are allowed in, and the ad switches that decide what
    the app's players show.
    """
    from .audit import log_action

    app_obj = get_object_or_404(AndroidApp, id=app_id)
    site = SiteSettings.get_settings()

    if request.method == 'POST':
        section = (request.POST.get('section') or 'app').strip()
        if section == 'ads':
            site.adsterra_clicks_required = _admin_int(
                request.POST.get('adsterra_clicks_required'), 5, 0, 1000)
            site.adsterra_show_banner = request.POST.get('adsterra_show_banner') == 'on'
            site.enable_adsterra_links = request.POST.get('enable_adsterra_links') == 'on'
            site.enable_autoclick_ads = request.POST.get('enable_autoclick_ads') == 'on'
            site.adsense_client_id = (request.POST.get('adsense_client_id') or '').strip()[:100]
            site.adsense_verification_meta = (
                request.POST.get('adsense_verification_meta') or '').strip()[:200]
            site.save()
            log_action(request, 'app_settings', app_obj.name,
                       'ad switches: banner=%s links=%s autoclick=%s clicks=%s' % (
                           site.adsterra_show_banner, site.enable_adsterra_links,
                           site.enable_autoclick_ads, site.adsterra_clicks_required))
            messages.success(request, 'Ad settings saved.')
        else:
            app_obj.data_retention_days = _admin_int(
                request.POST.get('data_retention_days'), app_obj.data_retention_days, 1, 3650)
            app_obj.log_retention_days = _admin_int(
                request.POST.get('log_retention_days'), app_obj.log_retention_days, 1, 3650)
            app_obj.sync_min_interval_seconds = _admin_int(
                request.POST.get('sync_min_interval_seconds'),
                app_obj.sync_min_interval_seconds, 5, 3600)
            app_obj.sync_max_per_minute = _admin_int(
                request.POST.get('sync_max_per_minute'), app_obj.sync_max_per_minute, 1, 600)
            app_obj.sync_ip_max_per_minute = _admin_int(
                request.POST.get('sync_ip_max_per_minute'),
                app_obj.sync_ip_max_per_minute, 1, 6000)
            app_obj.log_collection_enabled = request.POST.get('log_collection_enabled') == 'on'
            app_obj.is_active = request.POST.get('is_active') == 'on'
            app_obj.allowed_endpoint = (request.POST.get('allowed_endpoint') or '').strip()[:500]
            app_obj.allowed_build_id = (request.POST.get('allowed_build_id') or '').strip()[:255]
            app_obj.save()
            log_action(request, 'app_settings', app_obj.name,
                       'retention=%sd/%sd sync=%ss,%s/%smin active=%s builds=%s' % (
                           app_obj.data_retention_days, app_obj.log_retention_days,
                           app_obj.sync_min_interval_seconds, app_obj.sync_max_per_minute,
                           app_obj.sync_ip_max_per_minute, app_obj.is_active,
                           app_obj.allowed_build_id or 'any'))
            messages.success(request, 'App settings saved.')
        return redirect('android_app_settings', app_id=app_obj.id)

    return render(request, 'core/admin_app_settings.html', {
        'selected_app': app_obj,
        'site': site,
    })

@login_required
@user_passes_test(is_staff_or_superuser)
def admin_alerts(request):
    """Health alerts: what is checked, who is told, and a button to try it.

    Recipients are ordinary email addresses the operator adds here — they do
    not have to be admin accounts — and Telegram is optional. "Run check now"
    reports what the checks see without sending anything; "Send a test alert"
    delivers a message through every enabled channel, so the SMTP and bot
    settings are proven before a real outage.
    """
    from . import health
    from .audit import log_action
    from .models import AlertIncident, AlertRecipient, AlertSettings

    conf = AlertSettings.get_settings()
    findings = None
    delivery = None

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip()

        if action == 'save':
            conf.enabled = request.POST.get('enabled') == 'on'
            conf.check_5xx = request.POST.get('check_5xx') == 'on'
            try:
                threshold = float((request.POST.get('five_xx_threshold') or '2').replace(',', '.'))
            except (TypeError, ValueError):
                threshold = 2.0
            conf.five_xx_threshold = max(0.1, min(100.0, threshold))
            conf.five_xx_window_minutes = _admin_int(
                request.POST.get('five_xx_window_minutes'), 15, 1, 1440)
            conf.check_disk = request.POST.get('check_disk') == 'on'
            conf.disk_threshold = _admin_int(request.POST.get('disk_threshold'), 85, 1, 100)
            conf.check_workers = request.POST.get('check_workers') == 'on'
            conf.min_workers = _admin_int(request.POST.get('min_workers'), 2, 0, 100)
            conf.notify_email = request.POST.get('notify_email') == 'on'
            conf.notify_telegram = request.POST.get('notify_telegram') == 'on'
            conf.telegram_bot_token = (request.POST.get('telegram_bot_token') or '').strip()[:200]
            conf.telegram_chat_id = (request.POST.get('telegram_chat_id') or '').strip()[:100]
            conf.repeat_minutes = _admin_int(request.POST.get('repeat_minutes'), 30, 1, 10080)
            conf.quiet_start = _admin_hhmm(request.POST.get('quiet_start'))
            conf.quiet_end = _admin_hhmm(request.POST.get('quiet_end'))
            conf.save()
            log_action(request, 'alert_settings', 'health alerts',
                       'email=%s telegram=%s repeat=%smin' % (
                           conf.notify_email, conf.notify_telegram, conf.repeat_minutes))
            messages.success(request, 'Alert settings saved.')
            return redirect('admin_alerts')

        if action == 'add_recipient':
            email = (request.POST.get('email') or '').strip().lower()
            name = (request.POST.get('name') or '').strip()[:100]
            routing = _admin_check_routing(request.POST.getlist('checks'))
            if '@' not in email or len(email) < 5:
                messages.error(request, 'Enter a valid email address.')
            else:
                row, created = AlertRecipient.objects.get_or_create(
                    email=email, defaults={'name': name, 'is_active': True, 'checks': routing})
                if not created:
                    row.is_active = True
                    row.name = name or row.name
                    if routing:
                        row.checks = routing
                    row.save(update_fields=['is_active', 'name', 'checks'])
                log_action(request, 'alert_recipient', email,
                           'added' if created else 'switched back on')
                messages.success(request, '%s will receive the alerts.' % email)
            return redirect('admin_alerts')

        if action in ('remove_recipient', 'toggle_recipient'):
            rid = _admin_int(request.POST.get('id'), 0)
            row = AlertRecipient.objects.filter(id=rid).first()
            if row is None:
                messages.warning(request, 'That recipient no longer exists.')
            elif action == 'remove_recipient':
                log_action(request, 'alert_recipient', row.email, 'removed')
                row.delete()
                messages.success(request, '%s will no longer receive the alerts.' % row.email)
            else:
                row.is_active = not row.is_active
                row.save(update_fields=['is_active'])
                log_action(request, 'alert_recipient', row.email,
                           'on' if row.is_active else 'off')
                messages.success(
                    request, '%s is now %s.' % (row.email, 'on' if row.is_active else 'off'))
            return redirect('admin_alerts')

        if action == 'ack_incident':
            inc = AlertIncident.objects.filter(
                id=_admin_int(request.POST.get('id'), 0)).first()
            if inc is None:
                messages.warning(request, 'That incident no longer exists.')
            else:
                inc.acknowledged_at = timezone.now()
                inc.acknowledged_by = (request.user.get_username() or '')[:150]
                inc.save(update_fields=['acknowledged_at', 'acknowledged_by'])
                log_action(request, 'alert_ack', inc.title, 'incident acknowledged')
                messages.success(request, 'Incident acknowledged as %s.' % inc.acknowledged_by)
            return redirect('admin_alerts')

        if action == 'recipient_checks':
            row = AlertRecipient.objects.filter(
                id=_admin_int(request.POST.get('id'), 0)).first()
            if row is None:
                messages.warning(request, 'That recipient no longer exists.')
            else:
                row.checks = _admin_check_routing(request.POST.getlist('checks'))
                row.save(update_fields=['checks'])
                log_action(request, 'alert_recipient', row.email,
                           'routing=%s' % (row.checks or 'all checks'))
                messages.success(request, 'Routing saved for %s.' % row.email)
            return redirect('admin_alerts')

        if action == 'run':
            findings = health.run_checks()
            log_action(request, 'health_run', 'health checks',
                       '; '.join('%s=%s' % (f['title'], f['detail']) for f in findings))

        if action == 'test':
            delivery = health.dispatch(force=True)
            findings = delivery['findings']
            log_action(request, 'alert_test', 'test alert',
                       'email=%d telegram=%s errors=%s' % (
                           delivery['sent_email'], delivery['sent_telegram'],
                           '; '.join(delivery['errors']) or 'none'))
            if delivery['errors']:
                messages.warning(request, 'Test alert: ' + '; '.join(delivery['errors']))
            else:
                messages.success(
                    request,
                    'Test alert delivered to %d email(s)%s.' % (
                        delivery['sent_email'],
                        ' and Telegram' if delivery['sent_telegram'] else ''))

    incidents_open = list(AlertIncident.objects.filter(
        resolved_at__isnull=True).order_by('opened_at'))
    return render(request, 'core/admin_alerts.html', {
        'conf': conf,
        'recipients': list(AlertRecipient.objects.all()),
        'findings': findings,
        'delivery': delivery,
        'recipients_active': AlertRecipient.objects.filter(is_active=True).count(),
        'incidents_open': incidents_open,
        'incidents_recent': list(AlertIncident.objects.filter(
            resolved_at__isnull=False).order_by('-resolved_at')[:10]),
        'incident_keys': ['5xx', 'disk', 'workers'],
    })



def _admin_hhmm(value):
    """A 24h HH:MM string, or '' when the input is not a valid time."""
    value = (value or '').strip()
    if len(value) == 5 and value[2] == ':' and value[:2].isdigit() and value[3:].isdigit():
        hour, minute = int(value[:2]), int(value[3:])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return value
    return ''


def _admin_check_routing(values):
    """Comma-separated check keys from a list of submitted checkboxes."""
    order = ['5xx', 'disk', 'workers']
    picked = [k for k in order if k in set(values or [])]
    return ','.join(picked)[:60]


# ------------------------------------------------------------------ storage
# The tables that grow without being asked. Kept as data so the page, the
# purge and its preview all read the same list.
DATA_VOLUME_TABLES = (
    ('request_log', 'Request log', 'RequestLog', 'created_at', 'Every page and API request'),
    ('api_errors', 'API errors', 'ErrorLog', 'created_at', 'Failed 4xx/5xx responses'),
    ('app_logs', 'App logs', 'AndroidAppLog', 'timestamp', 'Logs posted by the Android app'),
    ('app_access', 'App access', 'AndroidAppAccessLog', 'access_date', 'Daily access counts per app'),
    ('app_builds', 'App builds', 'AndroidAppBuildLog', 'access_date', 'Daily build counts per app'),
    ('app_visitors', 'Daily visitors', 'AndroidAppDailyUniqueVisitor', 'access_date', 'Unique visitors per day'),
    ('app_failed', 'Failed attempts', 'AndroidAppFailedAttempt', 'attempted_at', 'Rejected API attempts'),
    ('app_devices', 'Device visits', 'AndroidAppDeviceVisit', 'visited_at', 'Device-level visits'),
    ('audit', 'Admin audit', 'AdminAuditLog', 'created_at', 'Admin actions recorded here'),
)


def _data_volume_rows(days):
    """Per-table row count, oldest row and how many rows a purge would delete."""
    from django.apps import apps
    from django.utils import timezone
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=max(1, int(days or 30)))
    rows = []
    for key, label, model_name, field, note in DATA_VOLUME_TABLES:
        try:
            model = apps.get_model('core', model_name)
        except LookupError:
            continue
        entry = {'key': key, 'label': label, 'note': note, 'count': 0,
                 'oldest': None, 'preview': 0, 'error': ''}
        try:
            qs = model.objects.all()
            entry['count'] = qs.count()
            oldest = qs.order_by(field).values_list(field, flat=True).first()
            entry['oldest'] = oldest
            entry['preview'] = qs.filter(**{'%s__lt' % field: cutoff}).count()
        except Exception as exc:  # a table that cannot be read is reported, not fatal
            entry['error'] = str(exc)[:120]
        rows.append(entry)
    return rows


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_data_volume(request):
    """What the growing tables cost, and a previewed purge for each of them."""
    from django.apps import apps
    from django.utils import timezone
    from datetime import timedelta
    from .audit import log_action

    try:
        days = int(request.GET.get('days') or request.POST.get('days') or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(3650, days))

    if request.method == 'POST' and (request.POST.get('action') or '') == 'purge':
        key = (request.POST.get('table') or '').strip()
        target = next((t for t in DATA_VOLUME_TABLES if t[0] == key), None)
        if target is None:
            messages.error(request, 'Unknown table.')
            return redirect('admin_data_volume')
        _, label, model_name, field, _note = target
        try:
            model = apps.get_model('core', model_name)
            cutoff = timezone.now() - timedelta(days=days)
            deleted = model.objects.filter(**{'%s__lt' % field: cutoff}).delete()[0]
            log_action(request, 'data_purge', label,
                       'removed %d row(s) older than %d day(s)' % (deleted, days))
            messages.success(request, 'Purged %d row(s) from %s (older than %d days).'
                             % (deleted, label, days))
        except Exception as exc:
            logger.warning('data volume: purge failed', exc_info=True)
            messages.error(request, 'Could not purge %s: %s' % (label, exc))
        return redirect('%s?days=%d' % (reverse('admin_data_volume'), days))

    rows = _data_volume_rows(days)
    db_size = None
    try:
        from django.conf import settings as dj_settings
        engine = dj_settings.DATABASES['default'].get('ENGINE', '')
        name = dj_settings.DATABASES['default'].get('NAME') or ''
        if 'sqlite' in engine and isinstance(name, str) and os.path.exists(name):
            db_size = os.path.getsize(name)
    except Exception:
        db_size = None

    return render(request, 'core/admin_data_volume.html', {
        'rows': rows,
        'days': days,
        'db_size': db_size,
        'total_rows': sum(r['count'] for r in rows),
        'total_preview': sum(r['preview'] for r in rows),
    })


# ------------------------------------------------------------------ peeks
# Fragments for the admin drawer. Same guard as the pages they belong to:
# staff only, and they only read.

@login_required
@user_passes_test(is_staff_or_superuser)
def request_peek(request, pk):
    """Drawer fragment: one recorded request in full."""
    from .models import RequestLog
    rec = get_object_or_404(RequestLog, pk=pk)
    return render(request, 'core/_peek_request.html', {'rec': rec})


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_user_peek(request, user_id):
    """Drawer fragment: one synced app account, with its reachable actions."""
    from .models import SyncedUser, UserHeartbeat, UserCloudData
    u = get_object_or_404(SyncedUser, pk=user_id)

    heartbeat = None
    try:
        heartbeat = UserHeartbeat.objects.filter(email=u.email).first()
    except Exception:
        logger.debug('peek: heartbeat lookup failed', exc_info=True)

    cloud = None
    try:
        cloud = UserCloudData.objects.filter(email=u.email).first()
    except Exception:
        logger.debug('peek: cloud lookup failed', exc_info=True)

    return render(request, 'core/_peek_synced_user.html', {
        'u': u,
        'heartbeat': heartbeat,
        'cloud': cloud,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def blocked_ip_peek(request, ip):
    """Drawer fragment: one blocked address and what it did."""
    from .models import BlockedIP, RequestLog
    row = BlockedIP.objects.filter(ip_address=ip).first()
    if row is None:
        raise Http404('No block for that address')

    try:
        after = RequestLog.objects.filter(client_ip=ip, created_at__gte=row.blocked_at).count()
    except Exception:
        after = 0
    try:
        recent = list(RequestLog.objects.filter(client_ip=ip).order_by('-created_at')[:10])
    except Exception:
        recent = []

    return render(request, 'core/_peek_blocked_ip.html', {
        'row': row,
        'ip': ip,
        'after_block': after,
        'recent': recent,
    })
