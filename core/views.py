
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden, StreamingHttpResponse
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
from django.db.models import Sum, Count
from django.db.models.functions import TruncDate
from django.views.decorators.http import require_http_methods, require_GET
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt
from django.db import models
import http.cookiejar
import urllib.request as _urlreq
import urllib.error as _urlerr
from urllib.parse import urlparse as _urlparse
import json
import logging
import datetime
import requests
import calendar
import base64
import secrets
from .middleware import get_client_ip
from bs4 import BeautifulSoup
import psutil
import platform
from .models import (SiteSettings, ContentRow, WatchList, PlayerConfiguration, TMDBApiKey, NavbarItem, DataSourceUsageLog, ProviderItem, ProviderRegionAvailability, WatchRegion, CalendarMonthCache, AndroidApp, AndroidAppAccessLog, AndroidAppBuildLog, AndroidAppFailedAttempt, AndroidAppDevice, AndroidAppDailyUniqueVisitor, AndroidAppDeviceVisit, AndroidAppLog, WebsiteVisitor, WebsiteVisitorVisit, Ad, AdImpression, UserActivity)
from .tmdb_client import get_data_client, get_tmdb_db_connection, TMDBClient
from .utils import normalize_movie_item, normalize_series_item, normalize_movie_detail, normalize_series_detail

logger = logging.getLogger(__name__)
from .forms import (
    SiteSettingsForm, ContentRowForm, PlayerConfigurationForm, TMDBApiKeyForm, TMDBApiKeyEditForm, NavbarItemForm, ProviderItemForm, WatchRegionForm,
    BrandingSettingsForm, DisplaySettingsForm, FooterSettingsForm, DataSourceSettingsForm, TMDBDBSettingsForm,
    PlayerSettingsForm, URLBlockingSettingsForm, EmailSettingsForm, AndroidAppForm, AdForm
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
    today = datetime.date.today()
    base_month = today.year * 12 + today.month - 1
    for offset in range(-2, 3):
        target = base_month + offset
        year = target // 12
        month = (target % 12) + 1
        if not CalendarMonthCache.objects.filter(year=year, month=month).exists():
            get_calendar_month_data(year, month)


def get_calendar_month_data(year, month):
    """Get movies and series for a specific month with caching"""
    cache_key = f"calendar_{year}_{month}"
    cached_data = cache.get(cache_key)
    if cached_data:
        return cached_data

    client = get_data_client()
    if hasattr(client, 'get_calendar_month_data'):
        calendar_data = client.get_calendar_month_data(year, month)
        if calendar_data:
            cache.set(cache_key, calendar_data, 86400)
            _store_calendar_month_data(year, month, calendar_data)
            return calendar_data
    
    # Calculate date range for the month
    first_day = datetime.date(year, month, 1)
    if month == 12:
        last_day = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    
    # Fetch movies released in this month
    movies = []
    try:
        movie_params = {
            'primary_release_date.gte': first_day.strftime('%Y-%m-%d'),
            'primary_release_date.lte': last_day.strftime('%Y-%m-%d'),
            'sort_by': 'primary_release_date.desc'
        }
        movie_data = client.discover_movies(movie_params)
        for item in movie_data.get('results', []):
            movies.append(normalize_movie_item(item))
    except Exception as e:
        logger.error("fetching calendar movies: %s", e)
    
    # Fetch series with episodes airing in this month
    series = []
    try:
        series_params = {
            'air_date.gte': first_day.strftime('%Y-%m-%d'),
            'air_date.lte': last_day.strftime('%Y-%m-%d'),
            'sort_by': 'first_air_date.desc'
        }
        series_data = client.discover_series(series_params)
        for item in series_data.get('results', []):
            series.append(normalize_series_item(item))
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


def calendar_month_data(request):
    """AJAX endpoint to get calendar data for a specific month"""
    year = int(request.GET.get('year', datetime.date.today().year))
    month = int(request.GET.get('month', datetime.date.today().month))
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
        'year': datetime.date.today().year,
        'month': datetime.date.today().month,
        'month_name': calendar.month_name[datetime.date.today().month],
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

def _strip_videasy_custom_urls(all_players):
    """Force server 0 (Videasy) to always use extraction — strip any custom URLs from DB."""
    players = list(all_players)
    if players:
        v = players[0]
        v.custom_movie_iframe_url = ''
        v.custom_iframe_url = ''
        v.custom_iframe_html = ''
        v.custom_movie_iframe_html = ''
        v.custom_tv_iframe_url = ''
        v.custom_tv_iframe_html = ''
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
    if cached:
        return JsonResponse(cached)
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

    today = timezone.now().date()

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

    return render(request, 'core/admin_dashboard.html', {
        'site_settings': site_settings,
        'visitors_today': visitors_today,
        'user_count': user_count,
        'plays_today': plays_today,
        'db_size': db_size,
        'ext_db_size': ext_db_size,
        'active_db': user_db,
    })


# Helper functions for ads
def get_user_today_clicks(user, ip_address):
    today = timezone.now().date()
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
@login_required
@user_passes_test(is_staff_or_superuser)
def ad_list(request):
    ads = Ad.objects.all().order_by('order', 'name')
    return render(request, 'core/ad_list.html', {'ads': ads})


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
    return render(request, 'core/ad_delete.html', {'ad': ad})


@login_required
@user_passes_test(is_staff_or_superuser)
def ad_toggle(request, ad_id):
    ad = get_object_or_404(Ad, id=ad_id)
    if request.method == 'POST':
        ad.is_active = not ad.is_active
        ad.save()
        return redirect('ad_list')
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


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
    today = timezone.now().date()
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
        return d > _date.today()
    except Exception:
        return False


def _render_movie_cards(movies):
    """Render movie cards HTML for AJAX load-more and catalog grids."""
    parts = []
    for m in movies:
        rd = m.get('release_date', '') or m.get('year', '')
        if _is_future_date(rd):
            badge = '<div class="card-rating-badge" style="background:rgba(39,174,96,.85);color:#fff">Coming Soon</div>'
        else:
            badge = '<div class="card-rating-badge">{rating}</div>'.format(rating=m.get('vote_average', ''))
        parts.append(
            '<div class="card-wrapper"><a href="/movies/{slug}/" class="movie-card" title="{title}">'
            '<div class="card-image-container">'
            '<img class="card-image" src="{img}" alt="{title}" loading="lazy">'
            '{badge}'
            '<div class="card-overlay"><div class="card-overlay-title">{title}</div>'
            '<div class="card-overlay-year">{year}</div></div>'
            '</div></a></div>'.format(
                slug=m.get('slug', ''),
                title=m.get('title', ''),
                img=m.get('cover_url', ''),
                badge=badge,
                year=m.get('year', ''),
            )
        )
    return ''.join(parts)


def _render_series_cards(series_list):
    """Render series cards HTML for AJAX load-more and catalog grids."""
    parts = []
    for s in series_list:
        fad = s.get('first_air_date', '') or s.get('year', '')
        if _is_future_date(fad):
            badge = '<div class="card-rating-badge" style="background:rgba(39,174,96,.85);color:#fff">Coming Soon</div>'
        else:
            badge = '<div class="card-rating-badge">{rating}</div>'.format(rating=s.get('vote_average', ''))
        parts.append(
            '<div class="card-wrapper"><a href="/series/id/{sid}/" class="movie-card" title="{title}">'
            '<div class="card-image-container">'
            '<img class="card-image" src="{img}" alt="{title}" loading="lazy">'
            '{badge}'
            '<div class="card-overlay"><div class="card-overlay-title">{title}</div>'
            '<div class="card-overlay-year">{year}</div></div>'
            '</div></a></div>'.format(
                sid=s.get('id', ''),
                title=s.get('title', ''),
                img=s.get('cover_url', ''),
                badge=badge,
                year=s.get('year', ''),
            )
        )
    return ''.join(parts)


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
    result = {'cast': [], 'crew': [], 'directors': '', 'trailers': [], 'collection': None, 'collection_movies': []}
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
                    params={'api_key': api_key.key, 'append_to_response': 'credits,videos'},
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
    cache_key = f'tmdb_extra_ajax_{media_type}_{tmdb_id}'
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
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['movie', 'both'],
        is_active=True
    ).order_by('order', 'id')
    all_players = _strip_videasy_custom_urls(all_players)
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
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['movie', 'both'],
        is_active=True
    ).order_by('order', 'id')
    all_players = _strip_videasy_custom_urls(all_players)
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
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['tv', 'both'],
        is_active=True
    ).order_by('order', 'id')
    all_players = _strip_videasy_custom_urls(all_players)
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
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['tv', 'both'],
        is_active=True
    ).order_by('order', 'id')
    all_players = _strip_videasy_custom_urls(all_players)
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
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def edit_settings(request):
    site_settings = SiteSettings.get_settings()
    api_keys = TMDBApiKey.objects.all().order_by('-is_active', '-created_at')
    
    if request.method == 'POST':
        form = SiteSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = SiteSettingsForm(instance=site_settings)
    
    return render(request, 'core/edit_settings.html', {
        'form': form,
        'api_keys': api_keys
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def branding_settings(request):
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = BrandingSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = BrandingSettingsForm(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'title': 'Branding Settings',
        'back_url': 'admin_dashboard',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def display_settings(request):
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = DisplaySettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = DisplaySettingsForm(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'title': 'Display Settings',
        'back_url': 'admin_dashboard',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def data_source_settings(request):
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = DataSourceSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = DataSourceSettingsForm(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'title': 'Data Source Settings',
        'back_url': 'admin_dashboard',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def tmdb_db_settings(request):
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = TMDBDBSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = TMDBDBSettingsForm(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'title': 'TMDB Database Settings',
        'back_url': 'admin_dashboard',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def player_settings(request):
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = PlayerSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = PlayerSettingsForm(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'title': 'Player Settings',
        'back_url': 'admin_dashboard',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def url_blocking_settings(request):
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = URLBlockingSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = URLBlockingSettingsForm(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'title': 'URL Blocking Settings',
        'back_url': 'admin_dashboard',
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def email_settings(request):
    """Email management page — SMTP addresses, purpose assignment, and templates."""
    from .models import EmailAddress, EmailTemplate, PURPOSE_CHOICES

    # Handle AJAX actions
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
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
                    email_obj.smtp_port = int(request.POST.get('smtp_port', 587))
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
                        smtp_port=int(request.POST.get('smtp_port', 587)),
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
            port = int(request.POST.get('smtp_port', 587))
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
                    EmailSendLog.objects.create(address=addr_obj, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message='SMTP password not set', sent_by=request.user, source='test_mail')
                    return JsonResponse({'success': False, 'message': 'SMTP password is not set for this address. Edit and save the password first.'})
                server = smtplib.SMTP(addr_obj.smtp_host, addr_obj.smtp_port, timeout=15)
                server.ehlo()
                if addr_obj.smtp_use_tls:
                    server.starttls()
                    server.ehlo()
                server.login(smtp_user, smtp_pass)
                server.sendmail(addr_obj.email, [to_email], msg.as_string())
                server.quit()
                EmailSendLog.objects.create(address=addr_obj, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='sent', sent_by=request.user, source='test_mail')
                return JsonResponse({'success': True, 'message': f'Test email sent successfully to {to_email}! Check inbox.'})
            except smtplib.SMTPAuthenticationError as e:
                EmailSendLog.objects.create(address=addr_obj, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message=f'Authentication failed: {str(e)}', sent_by=request.user, source='test_mail')
                return JsonResponse({'success': False, 'message': f'Authentication failed: {str(e)}. Check email and app password.'})
            except smtplib.SMTPConnectError as e:
                EmailSendLog.objects.create(address=addr_obj, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message=f'Connection failed: {str(e)}', sent_by=request.user, source='test_mail')
                return JsonResponse({'success': False, 'message': f'Cannot connect to {addr_obj.smtp_host}:{addr_obj.smtp_port}. Check host and port.'})
            except smtplib.SMTPException as e:
                EmailSendLog.objects.create(address=addr_obj, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message=f'SMTP error: {str(e)}', sent_by=request.user, source='test_mail')
                return JsonResponse({'success': False, 'message': f'SMTP error: {str(e)}'})
            except Exception as e:
                EmailSendLog.objects.create(address=addr_obj, recipient=to_email, subject=subject, purpose=addr_obj.purpose, status='failed', error_message=str(e), sent_by=request.user, source='test_mail')
                return JsonResponse({'success': False, 'message': f'Failed to send: {str(e)}'})

        return JsonResponse({'success': False, 'message': 'Unknown action'}, status=400)

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
    send_logs = EmailSendLog.objects.select_related('address', 'sent_by').all()[:100]
    log_stats = {
        'total': EmailSendLog.objects.count(),
        'sent': EmailSendLog.objects.filter(status='sent').count(),
        'failed': EmailSendLog.objects.filter(status='failed').count(),
        'today': EmailSendLog.objects.filter(created_at__date__gte=timezone.now().date()).count(),
    }

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
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def footer_settings(request):
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = FooterSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = FooterSettingsForm(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'title': 'Footer Settings',
        'description': 'Edit all footer sections including links, genres, countries, subscribe block, logo area, copyright, and disclaimer.',
        'back_url': 'admin_dashboard',
    })


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
                'created_at': api_key.created_at.strftime('%Y-%m-%d %H:%M'),
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
                'created_at': api_key.created_at.strftime('%Y-%m-%d %H:%M'),
                'last_used_at': api_key.last_used_at.strftime('%Y-%m-%d %H:%M') if api_key.last_used_at else None,
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
    return render(request, 'core/content_row_delete.html', {'content_row': content_row})



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

    # Handle POST to update data retention setting
    if request.method == 'POST' and app_id:
        selected_app = get_object_or_404(AndroidApp, id=app_id)
        try:
            retention_days = int(request.POST.get('retention_days', 30))
            retention_days = max(1, min(3650, retention_days))
            selected_app.data_retention_days = retention_days
            selected_app.save(update_fields=['data_retention_days', 'updated_at'])
            deleted = selected_app.clean_old_analytics_data()
            return JsonResponse({
                'success': True,
                'retention_days': selected_app.data_retention_days,
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
    recent_devices = []
    recent_visits = []
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
        
        # Get recent devices and visits
        recent_devices = selected_app.devices.all()[:10]
        recent_visits = selected_app.device_visits.select_related('device').all()[:20]
        
        # Calculate total unique visitors
        total_unique_visitors = selected_app.devices.count()

    summary_rows = []
    for app in apps:
        total_connections = app.access_logs.aggregate(total=models.Sum('connection_count'))['total'] or 0
        summary_rows.append({
            'app': app,
            'total_connections': total_connections,
            'last_accessed_at': app.last_accessed_at,
        })

    # Synced users
    from .models import SyncedUser
    synced_users = SyncedUser.objects.all().order_by('-last_synced_at')[:5]
    synced_total = SyncedUser.objects.count()
    synced_subscribed = SyncedUser.objects.filter(is_subscribed=True).count()
    synced_free = synced_total - synced_subscribed

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
        'recent_devices': recent_devices,
        'recent_visits': recent_visits,
        'synced_users': synced_users,
        'synced_total': synced_total,
        'synced_subscribed': synced_subscribed,
        'synced_free': synced_free,
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
    # Auto-clean old analytics data based on retention setting
    selected_app.clean_old_analytics_data()
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
        attempt['attempted_at'] = attempt['attempted_at'].isoformat()
        attempt['failure_reason_display'] = dict(AndroidAppFailedAttempt.FAILURE_REASON_CHOICES).get(attempt['failure_reason'])

    recent_devices = list(selected_app.devices.all()[:10].values(
        'user_id', 'device_model', 'os_version', 'total_visits', 'last_seen_at', 'first_seen_at'
    ))
    for device in recent_devices:
        device['last_seen_at'] = device['last_seen_at'].isoformat()
        device['first_seen_at'] = device['first_seen_at'].isoformat()

    recent_visits = list(selected_app.device_visits.select_related('device').all()[:20].values(
        'visited_at', 'device__user_id', 'device_model', 'os_version', 'build_identifier', 'ip_address', 'full_request'
    ))
    for visit in recent_visits:
        visit['visited_at'] = visit['visited_at'].isoformat()

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
        'recent_devices': recent_devices,
        'recent_visits': recent_visits,
        'last_accessed_at': selected_app.last_accessed_at.isoformat() if selected_app.last_accessed_at else None
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
            response_payload['apk_url'] = request.build_absolute_uri(android_app.apk_file.url)
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
    
    ip_address = request.META.get('REMOTE_ADDR', None)
    
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
            'timestamp': timezone.now().isoformat(),
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

    # Build movie_servers and series_servers from PlayerConfiguration
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
            return JsonResponse({'success': True, 'message': 'Login successful'})
        # If no password provided, try email-based login for synced users
        if not password and '@' in username:
            try:
                user = User.objects.get(email=username)
                if not user.has_usable_password():
                    # First web login: set a password
                    new_pass = request.POST.get('new_password', '')
                    if new_pass and len(new_pass) >= 6:
                        user.set_password(new_pass)
                        user.save()
                        login(request, user)
                        _record_session(user, 'web', request)
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
        
        # Check if email already exists (web or Android-synced user)
        existing_user = User.objects.filter(email=email).first()
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
                # Create email verification token
                from .models import EmailVerification
                token = secrets.token_hex(32)
                EmailVerification.objects.create(user=existing_user, token=token)
                # Send verification email
                verify_url = f"{request.scheme}://{request.get_host()}/ajax/verify-email/?token={token}"
                try:
                    send_configured_email(
                        subject='Verify your email - NewMovies',
                        message=f'Hi {username},\n\nClick the link below to verify your email (valid for 5 minutes):\n\n{verify_url}\n\nIf you did not register, please ignore this email.',
                        recipient_list=[email],
                        purpose='verification',
                        fail_silently=True,
                    )
                except Exception as e:
                    logger.warning(f'Failed to send verification email: {e}', exc_info=True)
                return JsonResponse({'success': True, 'message': 'Verification email sent. Please check your inbox.', 'requires_verification': True})
        
        if User.objects.filter(username=username).exists():
            return JsonResponse({'success': False, 'message': 'Username already taken'})
        
        # Create new user (inactive until email verified)
        user = User.objects.create_user(username=username, email=email, password=password)
        user.is_active = False
        user.save()
        
        # Create email verification token
        from .models import EmailVerification
        token = secrets.token_hex(32)
        EmailVerification.objects.create(user=user, token=token)
        
        # Send verification email
        verify_url = f"{request.scheme}://{request.get_host()}/ajax/verify-email/?token={token}"
        try:
            send_configured_email(
                subject='Verify your email - NewMovies',
                message=f'Hi {username},\n\nClick the link below to verify your email (valid for 5 minutes):\n\n{verify_url}\n\nIf you did not register, please ignore this email.',
                recipient_list=[email],
                purpose='verification',
                fail_silently=True,
            )
        except Exception as e:
            logger.warning(f'Failed to send verification email: {e}', exc_info=True)
        
        return JsonResponse({'success': True, 'message': 'Verification email sent. Please check your inbox.', 'requires_verification': True})
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


def verify_email(request):
    """GET /ajax/verify-email/?token=... — verify email within 5 minutes."""
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
    ev.user.is_active = True
    ev.user.save(update_fields=['is_active'])
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


def ajax_forgot_password(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        try:
            user = User.objects.get(email=email)
            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            
            # Send email
            reset_url = request.build_absolute_uri(f'/reset-password/{uid}/{token}/')
            subject = f'Password Reset for {SiteSettings.get_settings().brand_name}'
            message = f'Click the link to reset your password: {reset_url}'
            send_configured_email(subject, message, [email], purpose='password_reset')
            
            return JsonResponse({'success': True, 'message': 'Password reset link sent to your email'})
        except User.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Email not found'})
        except Exception as e:
            return JsonResponse({'success': False, 'message': f'Error sending email: {str(e)}'})
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


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

        WatchList.objects.create(
            user=request.user,
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            poster_path=poster_path
        )
        # Add to cloud data too
        from .models import UserCloudData
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
    watchlist_items = WatchList.objects.filter(user=request.user)
    
    movie_items = []
    series_items = []
    existing_tmdb_ids = set()
    
    for item in watchlist_items:
        existing_tmdb_ids.add((item.tmdb_id, item.media_type))
        processed_item = {
            'title': item.title,
            'slug': slugify(item.title),
            'cover_url': f"{settings.TMDB_IMAGE_BASE_URL}{item.poster_path}" if item.poster_path else None,
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
            WatchList.objects.get_or_create(
                user=request.user, tmdb_id=mid, media_type=mtype,
                defaults={
                    'title': cloud_item.get('title', ''),
                    'poster_path': cloud_item.get('posterPath', ''),
                },
            )
            processed_item = {
                'title': cloud_item.get('title', ''),
                'slug': slugify(cloud_item.get('title', str(mid))),
                'cover_url': f"{settings.TMDB_IMAGE_BASE_URL}{cloud_item.get('posterPath', '')}" if cloud_item.get('posterPath') else None,
                'id': mid,
                'poster_path': cloud_item.get('posterPath', ''),
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
        start_date = datetime.datetime.now().strftime('%Y-%m-%d')
    
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

    # Block obviously non-video URLs for safety
    low = target_url.lower().split('?')[0]
    safe_exts = ('.mp4', '.webm', '.m4v', '.mkv', '.mov', '.ts', '.flv',
                 '.m3u8', '.mpd', '.avi', '.wmv', '.ogg')
    if not any(low.endswith(ext) for ext in safe_exts):
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
        remote = requests.get(target_url, headers=headers, stream=True, timeout=30,
                              allow_redirects=True)
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


def _vk_decrypt(payload_b64, seed_str, tmdb_id):
    """Decrypt Vidking encrypted response"""
    # Base64 decode (URL-safe)
    padded = payload_b64.replace('-', '+').replace('_', '/')
    if len(padded) % 4:
        padded += '=' * (4 - len(padded) % 4)
    data = bytearray(base64.b64decode(padded))

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

    try:
        headers = {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/125.0.0.0 Safari/537.36'),
            'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': target,
        }
        resp = requests.get(target, headers=headers, timeout=15, allow_redirects=True)
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
    return render(request, 'core/player_delete.html', {'player': player})


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
    return render(request, 'core/navbar_item_delete.html', {'navbar_item': navbar_item})


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
    bot_visits = WebsiteVisitorVisit.objects.filter(is_bot=True)

    total_visitors = WebsiteVisitor.objects.filter(
        models.Q(visits__isnull=True) | models.Q(visits__is_bot=False)
    ).distinct().count()
    total_pageviews = human_visits.count()
    visitors_today = WebsiteVisitor.objects.filter(
        visits__is_bot=False,
        visits__visited_at__date=today
    ).distinct().count()
    pageviews_today = human_visits.filter(visited_at__date=today).count()

    bot_total_visits = bot_visits.count()
    bot_visits_today = bot_visits.filter(visited_at__date=today).count()
    bot_visits_last_hour = bot_visits.filter(visited_at__gte=one_hour_ago).count()
    bot_unique_ips = list(
        bot_visits.exclude(ip_address__isnull=True)
        .exclude(ip_address='')
        .values_list('ip_address', flat=True)
        .distinct()
        .order_by('ip_address')
    )

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
            'visited_at': activity.visited_at.isoformat(),
            'ip_address': activity.ip_address,
            'is_bot': activity.is_bot,
        })

    bot_top_ips = list(
        bot_visits.exclude(ip_address__isnull=True)
        .exclude(ip_address='')
        .values('ip_address')
        .annotate(request_count=Count('id'))
        .order_by('-request_count', 'ip_address')[:10]
    )

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
    """Simple health-check endpoint for the Videasy player."""
    return JsonResponse({"status": "ok"})


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

    headers = {
        'User-Agent': _VIDEASY_USER_AGENT,
        'Origin': _VIDEASY_ORIGIN,
        'Referer': f"{_VIDEASY_ORIGIN}/",
    }

    try:
        _session = _get_proxy_session()
        resp = _session.get(target_url, headers=headers, timeout=(5, 120), stream=True, verify=True, allow_redirects=True)
        if resp.status_code == 403:
            headers.pop('Origin', None)
            headers.pop('Referer', None)
            resp.close()
            _session = _get_proxy_session()
        resp = _session.get(target_url, headers=headers, timeout=(5, 120), stream=True, verify=True, allow_redirects=True)
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
        resp = _req.get(
            target_url,
            headers=headers,
            timeout=(8, 30),  # connect timeout, read timeout
            stream=True,
            verify=True,
            allow_redirects=True,
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
def player_sources_view(request):
    """
    Server-side source fetch & decrypt for the embedded player.
    Uses parallel fetching for maximum speed.
    """
    import time as _time
    import concurrent.futures

    tmdb_id = request.GET.get('tmdb_id', '')
    media_type = request.GET.get('type', 'movie')
    season = request.GET.get('season', '')
    episode = request.GET.get('episode', '')

    if not tmdb_id:
        return JsonResponse({'success': False, 'error': 'tmdb_id is required', 'results': []})

    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Invalid tmdb_id', 'results': []})

    timestamp = str(int(_time.time() * 1000))

    # Fetch TMDB info + seed in parallel (saves ~1-2s)
    tmdb_info = {'title': '', 'year': '', 'imdbId': ''}
    seed = ''

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

    title = tmdb_info.get('title', '')
    year = tmdb_info.get('year', '')
    imdb_id = tmdb_info.get('imdbId', '')

    if not seed:
        return JsonResponse({'success': False, 'error': 'Could not fetch seed', 'results': []})

    # Fetch from ALL servers in PARALLEL (was sequential — ~8x faster)
    results = []
    seen_urls = set()

    def _fetch_one(server_name, endpoint):
        try:
            return _vk_fetch_sources_for_server(
                server_name, endpoint, tmdb_id, media_type,
                title=title, year=year, season_id=season,
                episode_id=episode, imdb_id=imdb_id,
                seed=seed, timestamp=timestamp,
            )
        except Exception as e:
            logger.debug('player_sources: %s failed: %s', server_name, e)
            return {'sources': []}

    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        futures = {
            pool.submit(_fetch_one, name, ep): name
            for name, ep in _VIDKING_SERVERS.items()
        }
        for future in concurrent.futures.as_completed(futures):
            server_name = futures[future]
            try:
                result = future.result()
            except Exception:
                continue
            sources = result.get('sources', [])
            if isinstance(sources, list) and sources:
                for src in sources:
                    if isinstance(src, dict) and src.get('url') and src['url'] not in seen_urls:
                        seen_urls.add(src['url'])
                        lang = src.get('language', '') or src.get('audioLanguage', '') or src.get('audio', '') or src.get('title', '') or ''
                        quality = src.get('quality', '?')
                        # Pass through full source object for client-side language detection
                        entry = dict(src)
                        entry['url'] = src['url']
                        entry['quality'] = quality
                        entry['language'] = lang
                        entry['server'] = server_name
                        entry['server_name'] = server_name
                        results.append(entry)

    return JsonResponse({
        'success': True,
        'title': title,
        'year': year,
        'tmdbId': tmdb_id,
        'type': media_type,
        'results': results,
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
    """Admin page to view subscribers, send emails."""
    if not request.user.is_staff:
        return HttpResponseForbidden('Staff only')
    subs = Subscriber.objects.all().order_by('-created_at')
    total_active = subs.filter(is_active=True).count()
    total_inactive = subs.filter(is_active=False).count()
    sent_messages = EmailMessage.objects.select_related('sent_by').all()[:20]
    # Build delivery history per subscriber
    all_deliveries = EmailDelivery.objects.select_related('message', 'subscriber').all()[:100]
    return render(request, 'core/admin_subscribers.html', {
        'subscribers': subs,
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

        # Create EmailMessage record first
        email_msg = EmailMessage.objects.create(
            subject=subject,
            body=body,
            sent_by=request.user,
            recipient_count=len(sub_list),
        )

        # Send via BCC — each subscriber gets their own email, can't see others
        sent_count = 0
        for sub in sub_list:
            try:
                send_mail(subject, body, from_email, [sub.email], fail_silently=False)
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
from .models import SyncedUser, UserCloudData
from .auth import send_configured_email
from .auth import (
    create_user_token_pair, decode_jwt_token, reset_rate_limit,
    check_rate_limit, record_rate_limit_attempt, rate_limit_response,
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
    django_user = User.objects.filter(email=email).first()
    user_obj = SyncedUser.objects.filter(email=email).first()
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
    display_name = body.get('displayName', '')

    if not email or not password:
        return JsonResponse({'status': 'error', 'message': 'Email and password are required'}, status=400)

    # Validate email domain
    allowed_domains = ['gmail.com', 'yahoo.com', 'yahoo.co.in']
    email_domain = email.split('@')[-1] if '@' in email else ''
    if email_domain not in allowed_domains:
        return JsonResponse({'status': 'error', 'message': "We don't support this email. Please use Gmail or Yahoo."}, status=400)

    if len(password) < 6:
        return JsonResponse({'status': 'error', 'message': 'Password must be at least 6 characters'}, status=400)

    # Check if already registered
    existing_user = User.objects.filter(email=email).first()
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

    # Send verification email
    from .models import EmailVerification
    token = secrets.token_hex(32)
    EmailVerification.objects.create(user=django_user, token=token)
    verify_url = f"{request.scheme}://{request.get_host()}/ajax/verify-email/?token={token}"
    try:
        send_configured_email(
            subject='Verify your email - NewMovies',
            message=f'Hi {display_name},\n\nClick the link below to verify your email (valid for 5 minutes):\n\n{verify_url}\n\nIf you did not register, please ignore this email.',
            recipient_list=[email],
            purpose='verification',
            fail_silently=True,
        )
    except Exception as e:
        logger.warning(f'Failed to send verification email: {e}', exc_info=True)

    resp = {
        'status': 'success',
        'message': 'Account registered. Please verify your email via the link sent to your inbox.',
        'requiresVerification': True,
        'user': _user_payload(user_obj, django_user),
        'subscription': FREE_SUBSCRIPTION,
    }
    return JsonResponse(resp)


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

    # Rate limit by IP
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', '')
    allowed, remaining, retry_after = check_rate_limit(ip, 'login')
    if not allowed:
        return rate_limit_response(retry_after)

    # Also rate limit by email to prevent targeted brute force
    allowed_email, _, retry_email = check_rate_limit(email, 'login_email')
    if not allowed_email:
        return rate_limit_response(retry_email)

    django_user = User.objects.filter(email=email).first()
    if not django_user:
        record_rate_limit_attempt(ip, 'login')
        record_rate_limit_attempt(email, 'login_email')
        return JsonResponse({'status': 'error', 'message': 'Email is not registered. Please register first.'}, status=404)

    if not django_user.has_usable_password():
        return JsonResponse({
            'status': 'error',
            'message': 'This account was created from Android sync. Please register with a password first.',
        }, status=400)

    if not django_user.is_active:
        return JsonResponse({
            'status': 'error',
            'message': 'Email not verified. Please check your inbox for the verification link.',
            'requiresVerification': True,
        }, status=403)

    auth_user = authenticate(request, username=django_user.username, password=password)
    if not auth_user:
        record_rate_limit_attempt(ip, 'login')
        record_rate_limit_attempt(email, 'login_email')
        return JsonResponse({'status': 'error', 'message': 'Invalid password'}, status=401)

    # Success — reset rate limits
    reset_rate_limit(ip, 'login')
    reset_rate_limit(email, 'login_email')

    # Get or create synced profile
    user_obj, _ = SyncedUser.objects.get_or_create(
        email=email, defaults={'user': auth_user, 'display_name': auth_user.first_name},
    )
    if not user_obj.user:
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

    return JsonResponse(_build_user_response(auth_user, user_obj, {
        'message': 'Login successful',
        'requiresVerification': False,
    }))


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


@csrf_exempt
@require_http_methods(["GET"])
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
    # Always return success to prevent email enumeration
    django_user = User.objects.filter(email=email).first()
    if django_user and django_user.has_usable_password():
        token = default_token_generator.make_token(django_user)
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode
        uid = urlsafe_base64_encode(force_bytes(django_user.pk))
        reset_url = f"{request.scheme}://{request.get_host()}/reset-password/{uid}/{token}/"
        try:
            send_configured_email(
                subject='Password Reset - NewMovies',
                message=f'Click the link below to reset your password:\n\n{reset_url}\n\nIf you did not request this, please ignore this email.',
                recipient_list=[email],
                purpose='password_reset',
                fail_silently=True,
            )
        except Exception as e:
            logger.warning(f'Failed to send reset email: {e}', exc_info=True)
    return JsonResponse({
        'status': 'success',
        'message': 'Password reset instructions sent to your email.',
    })


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


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_users_list(request):
    """Admin page listing all synced Android users."""
    users = SyncedUser.objects.all().order_by('-last_synced_at')
    total = users.count()
    subscribed = users.filter(is_subscribed=True).count()
    free = total - subscribed
    return render(request, 'core/synced_users_list.html', {
        'users': users,
        'total': total,
        'subscribed': subscribed,
        'free': free,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_user_detail(request, user_id):
    """Show full details for a single synced user."""
    user_obj = get_object_or_404(SyncedUser, id=user_id)
    cloud_data = None
    if user_obj.user:
        cloud_data, _ = UserCloudData.objects.get_or_create(user=user_obj.user)
    return render(request, 'core/synced_user_detail.html', {'user_obj': user_obj, 'cloud_data': cloud_data})


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_user_edit(request, user_id):
    """Admin: edit a synced user's subscription."""
    user_obj = get_object_or_404(SyncedUser, id=user_id)
    if request.method == 'POST':
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
        user_obj.save()
        return redirect('synced_users_list')
    return render(request, 'core/synced_user_edit.html', {'synced_user': user_obj})


@login_required
@user_passes_test(is_staff_or_superuser)
def synced_user_delete(request, user_id):
    """Admin: delete a synced user."""
    user_obj = get_object_or_404(SyncedUser, id=user_id)
    if request.method == 'POST':
        user_obj.delete()
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

    history, created = PlayHistory.objects.update_or_create(
        user=request.user,
        tmdb_id=tmdb_id,
        media_type=media_type,
        season_number=season_number,
        episode_number=episode_number,
        defaults={
            'title': title,
            'poster_path': poster_path,
            'episode_title': episode_title,
            'duration_seconds': duration_seconds,
            'total_duration_seconds': total_duration_seconds,
            'completed': completed,
        }
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
def user_play_history(request):
    """Show the user's play history."""
    history = PlayHistory.objects.filter(user=request.user).order_by('-last_played_at')
    return render(request, 'core/user_play_history.html', {'history': history})


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
    from django.db.models import Sum, Count, Avg, F, Q
    from django.utils import timezone

    days = int(request.GET.get('days', 7))
    since = timezone.now() - timezone.timedelta(days=days)

    # Overview stats
    total_views = UserPageView.objects.filter(viewed_at__gte=since).count()
    total_time = UserPageView.objects.filter(viewed_at__gte=since).aggregate(
        total=Sum('time_spent_seconds'))['total'] or 0
    avg_time = UserPageView.objects.filter(viewed_at__gte=since).aggregate(
        avg=Avg('time_spent_seconds'))['avg'] or 0
    unique_visitors = UserPageView.objects.filter(viewed_at__gte=since).values(
        'visitor_id').distinct().count()
    unique_users = UserPageView.objects.filter(
        viewed_at__gte=since, user__isnull=False).values('user').distinct().count()

    # Platform breakdown
    platform_data = list(UserPageView.objects.filter(viewed_at__gte=since)
        .values('platform')
        .annotate(views=Count('id'), total_time=Sum('time_spent_seconds'))
        .order_by('-views'))

    # Top pages
    top_pages = list(UserPageView.objects.filter(viewed_at__gte=since)
        .values('path', 'page_title')
        .annotate(views=Count('id'), total_time=Sum('time_spent_seconds'),
                  avg_time=Avg('time_spent_seconds'))
        .order_by('-views')[:20])

    # Most active users
    active_users = list(UserPageView.objects.filter(
        viewed_at__gte=since, user__isnull=False)
        .values('user__username', 'user__email')
        .annotate(views=Count('id'), total_time=Sum('time_spent_seconds'))
        .order_by('-views')[:15])

    # Active sessions
    active_sessions = UserSession.objects.filter(is_active=True).select_related('user').order_by('-last_seen_at')[:20]

    # Daily views chart data (last 30 days)
    chart_days = min(days * 2, 30)
    daily_views = []
    for i in range(chart_days):
        d = (timezone.now() - timezone.timedelta(days=i)).date()
        count = UserPageView.objects.filter(viewed_at__date=d).count()
        daily_views.append({'date': d.isoformat(), 'views': count})
    daily_views.reverse()

    # Hourly heatmap data (today)
    hourly_views = list(UserPageView.objects.filter(
        viewed_at__date=timezone.now().date())
        .extra(select={'hour': 'strftime("%%H", viewed_at)'})
        .values('hour')
        .annotate(views=Count('id'))
        .order_by('hour'))

    return render(request, 'core/admin_analytics.html', {
        'days': days,
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
        'hourly_views': hourly_views,
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
        d = (timezone.now() - timezone.timedelta(days=i)).date()
        count = user_views.filter(viewed_at__date=d).count()
        daily_views.append({'date': d.isoformat(), 'views': count})
    daily_views.reverse()

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
        'sessions': sessions,
        'recent_views': recent_views,
    })


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
        session_qs = UserSession.objects.filter(user_id__in=user_ids)
        for row in session_qs.filter(is_active=True).values('user_id').annotate(c=Count('id')):
            active_map[row['user_id']] = row['c']
        for row in session_qs.values('user_id').annotate(c=Count('id')):
            logins_map[row['user_id']] = row['c']
        for row in session_qs.order_by('-logged_in_at').values('user_id').distinct()[:500]:
            if row['user_id'] not in source_map:
                # Get last source from annotation
                pass
        for row in session_qs.values('user_id').annotate(last_src=Max('source')):
            source_map[row['user_id']] = row['last_src'] or ''
        # Attach to each user object
        for u in users:
            u.session_active_count = active_map.get(u.id, 0)
            u.session_total_logins = logins_map.get(u.id, 0)
            u.session_last_source = source_map.get(u.id, '')

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
    """Admin: delete a user."""
    user_obj = get_object_or_404(User, id=user_id)
    if request.method == 'POST':
        username = user_obj.username
        user_obj.delete()
        messages.success(request, f'User {username} deleted')
    return redirect('admin_user_list')


@login_required
@user_passes_test(is_staff_or_superuser)
def admin_user_detail(request, user_id):
    """Full user detail page with subscription, activity, cloud data, session history."""
    from .models import UserSession
    detail_user = get_object_or_404(User, id=user_id)
    synced_profile = SyncedUser.objects.filter(user=detail_user).first()
    cloud_data = UserCloudData.objects.filter(user=detail_user).first()
    play_history = PlayHistory.objects.filter(user=detail_user).order_by('-last_played_at')[:50]
    sessions = UserSession.objects.filter(user=detail_user).order_by('-logged_in_at')[:50]
    total_logins = UserSession.objects.filter(user=detail_user).count()
    active_sessions = UserSession.objects.filter(user=detail_user, is_active=True).count()
    last_source = sessions.first().source if sessions else ''
    return render(request, 'core/admin_user_detail.html', {
        'detail_user': detail_user,
        'synced_profile': synced_profile,
        'cloud_data': cloud_data,
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
            return redirect('db_connection_settings')

        elif action == 'delete':
            conn_id = request.POST.get('conn_id', '')
            if conn_id:
                conn = get_object_or_404(DBConnectionConfig, pk=conn_id)
                conn.delete()
                messages.success(request, 'Connection deleted.')
            return redirect('db_connection_settings')

        elif action == 'toggle_active':
            conn_id = request.POST.get('conn_id', '')
            if conn_id:
                conn = get_object_or_404(DBConnectionConfig, pk=conn_id)
                conn.is_active = not conn.is_active
                conn.save()
                status = 'activated' if conn.is_active else 'deactivated'
                messages.success(request, f'Connection "{conn.name}" {status}.')
            return redirect('db_connection_settings')

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
            return redirect('db_routing_settings')

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
            return redirect('db_routing_settings')

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
