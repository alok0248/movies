
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.template.loader import render_to_string
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.text import slugify
from django.utils import timezone
from django.core.mail import send_mail
from django.core.cache import cache
from django.db.models import Sum, Count
from django.db.models.functions import TruncDate
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.db import models
import json
import datetime
import requests
import calendar
import base64
from bs4 import BeautifulSoup
import psutil
import platform
from .models import (SiteSettings, ContentRow, WatchList, PlayerConfiguration, TMDBApiKey, NavbarItem, DataSourceUsageLog, ProviderItem, CalendarMonthCache, AndroidApp, AndroidAppAccessLog, AndroidAppBuildLog, AndroidAppFailedAttempt, AndroidAppDevice, AndroidAppDailyUniqueVisitor, AndroidAppDeviceVisit, WebsiteVisitor, WebsiteVisitorVisit)
from .tmdb_client import get_data_client, get_tmdb_db_connection, TMDBClient
from .forms import (
    SiteSettingsForm, ContentRowForm, PlayerConfigurationForm, TMDBApiKeyForm, TMDBApiKeyEditForm, NavbarItemForm, ProviderItemForm,
    BrandingSettingsForm, DisplaySettingsForm, FooterSettingsForm, DataSourceSettingsForm, TMDBDBSettingsForm,
    PlayerSettingsForm, AdsSettingsForm, URLBlockingSettingsForm, EmailSettingsForm, AndroidAppForm
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
            processed_item = item.copy()
            processed_item['title'] = item.get('title', 'Unknown Title')
            processed_slug = slugify(processed_item['title'])
            if not processed_slug:
                processed_slug = f"movie-{item.get('id', 'unknown')}"
            processed_item['slug'] = processed_slug
            processed_item['year'] = item.get('release_date', '')[:4] if item.get('release_date') else ''
            processed_item['vote_average'] = item.get('vote_average', 0)
            processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
            processed_item['id'] = item.get('id')
            processed_item['poster_path'] = item.get('poster_path')
            processed_item['overview'] = item.get('overview', '')
            processed_item['release_date'] = item.get('release_date')
            processed_item['media_type'] = 'movie'
            movies.append(processed_item)
    except Exception as e:
        print(f"Error fetching calendar movies: {e}")
    
    # Fetch series with episodes airing in this month
    series = []
    try:
        # For series, we can check first_air_date or use discover with air_date
        series_params = {
            'air_date.gte': first_day.strftime('%Y-%m-%d'),
            'air_date.lte': last_day.strftime('%Y-%m-%d'),
            'sort_by': 'first_air_date.desc'
        }
        series_data = client.discover_series(series_params)
        for item in series_data.get('results', []):
            processed_item = item.copy()
            processed_item['title'] = item.get('name', 'Unknown Title')
            processed_slug = slugify(processed_item['title'])
            if not processed_slug:
                processed_slug = f"series-{item.get('id', 'unknown')}"
            processed_item['slug'] = processed_slug
            processed_item['vote_average'] = item.get('vote_average', 0)
            processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
            processed_item['id'] = item.get('id')
            processed_item['poster_path'] = item.get('poster_path')
            processed_item['overview'] = item.get('overview', '')
            processed_item['first_air_date'] = item.get('first_air_date')
            processed_item['media_type'] = 'tv'
            series.append(processed_item)
    except Exception as e:
        print(f"Error fetching calendar series: {e}")
    
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
        processed_item = item.copy()
        if row.media_type == 'movie':
            processed_item['title'] = item.get('title', 'Unknown Title')
            processed_slug = slugify(processed_item['title'])
            if not processed_slug:
                processed_slug = f"movie-{item.get('id', 'unknown')}"
            processed_item['slug'] = processed_slug
            processed_item['year'] = item.get('release_date', '')[:4] if item.get('release_date') else ''
            processed_item['vote_average'] = item.get('vote_average', 0)
            processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
            processed_item['id'] = item.get('id')
            processed_item['poster_path'] = item.get('poster_path')
            processed_item['overview'] = item.get('overview', '')
            processed_item['release_date'] = item.get('release_date')
        else:
            processed_item['title'] = item.get('name', 'Unknown Title')
            processed_slug = slugify(processed_item['title'])
            if not processed_slug:
                processed_slug = f"series-{item.get('id', 'unknown')}"
            processed_item['slug'] = processed_slug
            processed_item['vote_average'] = item.get('vote_average', 0)
            processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
            processed_item['id'] = item.get('id')
            processed_item['poster_path'] = item.get('poster_path')
            processed_item['overview'] = item.get('overview', '')
            processed_item['first_air_date'] = item.get('first_air_date')
        processed_item['media_type'] = row.media_type
        items.append(processed_item)

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
                    for item in batched_movies:
                        processed_item = item.copy()
                        processed_item['title'] = item.get('title', 'Unknown Title')
                        processed_slug = slugify(processed_item['title']) or f"movie-{item.get('id', 'unknown')}"
                        processed_item['slug'] = processed_slug
                        processed_item['year'] = item.get('release_date', '')[:4] if item.get('release_date') else ''
                        processed_item['vote_average'] = item.get('vote_average', 0)
                        processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                        processed_item['id'] = item.get('id')
                        processed_item['poster_path'] = item.get('poster_path')
                        processed_item['overview'] = item.get('overview', '')
                        processed_item['release_date'] = item.get('release_date')
                        processed_item['media_type'] = 'movie'
                        top_movies.append(processed_item)
                else:
                    for movie_id in movie_ids:
                        item = client.get_movie_details(movie_id)
                        if item:
                            processed_item = item.copy()
                            processed_item['title'] = item.get('title', 'Unknown Title')
                            processed_slug = slugify(processed_item['title']) or f"movie-{item.get('id', 'unknown')}"
                            processed_item['slug'] = processed_slug
                            processed_item['year'] = item.get('release_date', '')[:4] if item.get('release_date') else ''
                            processed_item['vote_average'] = item.get('vote_average', 0)
                            processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                            processed_item['id'] = item.get('id')
                            processed_item['poster_path'] = item.get('poster_path')
                            processed_item['overview'] = item.get('overview', '')
                            processed_item['release_date'] = item.get('release_date')
                            processed_item['media_type'] = 'movie'
                            top_movies.append(processed_item)
                cache.set(top_movies_cache_key, top_movies, 3600)
            except Exception as e:
                print(f"Error fetching curated top movies: {e}")

    if not top_movies:
        top_movies_cache_key = 'top_movies'
        cached_top_movies = cache.get(top_movies_cache_key)
        if cached_top_movies:
            top_movies = cached_top_movies
        else:
            try:
                data = client.get_top_rated_movies(page=1)
                for item in data.get('results', []):
                    processed_item = item.copy()
                    processed_item['title'] = item.get('title', 'Unknown Title')
                    processed_slug = slugify(processed_item['title']) or f"movie-{item.get('id', 'unknown')}"
                    processed_item['slug'] = processed_slug
                    processed_item['year'] = item.get('release_date', '')[:4] if item.get('release_date') else ''
                    processed_item['vote_average'] = item.get('vote_average', 0)
                    processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                    processed_item['id'] = item.get('id')
                    processed_item['poster_path'] = item.get('poster_path')
                    processed_item['overview'] = item.get('overview', '')
                    processed_item['release_date'] = item.get('release_date')
                    processed_item['media_type'] = 'movie'
                    top_movies.append(processed_item)
                cache.set(top_movies_cache_key, top_movies, 3600)
            except Exception as e:
                print(f"Error fetching top movies: {e}")

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
                    for item in batched_series:
                        processed_item = item.copy()
                        processed_item['title'] = item.get('name', 'Unknown Title')
                        processed_slug = slugify(processed_item['title']) or f"series-{item.get('id', 'unknown')}"
                        processed_item['slug'] = processed_slug
                        processed_item['vote_average'] = item.get('vote_average', 0)
                        processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                        processed_item['id'] = item.get('id')
                        processed_item['poster_path'] = item.get('poster_path')
                        processed_item['overview'] = item.get('overview', '')
                        processed_item['first_air_date'] = item.get('first_air_date')
                        processed_item['media_type'] = 'tv'
                        top_series.append(processed_item)
                else:
                    for series_id in series_ids:
                        item = client.get_series_details(series_id)
                        if item:
                            processed_item = item.copy()
                            processed_item['title'] = item.get('name', 'Unknown Title')
                            processed_slug = slugify(processed_item['title']) or f"series-{item.get('id', 'unknown')}"
                            processed_item['slug'] = processed_slug
                            processed_item['vote_average'] = item.get('vote_average', 0)
                            processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                            processed_item['id'] = item.get('id')
                            processed_item['poster_path'] = item.get('poster_path')
                            processed_item['overview'] = item.get('overview', '')
                            processed_item['first_air_date'] = item.get('first_air_date')
                            processed_item['media_type'] = 'tv'
                            top_series.append(processed_item)
                cache.set(top_series_cache_key, top_series, 3600)
            except Exception as e:
                print(f"Error fetching curated top series: {e}")

    if not top_series:
        top_series_cache_key = 'top_series'
        cached_top_series = cache.get(top_series_cache_key)
        if cached_top_series:
            top_series = cached_top_series
        else:
            try:
                data = client.get_top_rated_series(page=1)
                for item in data.get('results', []):
                    processed_item = item.copy()
                    processed_item['title'] = item.get('name', 'Unknown Title')
                    processed_slug = slugify(processed_item['title']) or f"series-{item.get('id', 'unknown')}"
                    processed_item['slug'] = processed_slug
                    processed_item['vote_average'] = item.get('vote_average', 0)
                    processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                    processed_item['id'] = item.get('id')
                    processed_item['poster_path'] = item.get('poster_path')
                    processed_item['overview'] = item.get('overview', '')
                    processed_item['first_air_date'] = item.get('first_air_date')
                    processed_item['media_type'] = 'tv'
                    top_series.append(processed_item)
                cache.set(top_series_cache_key, top_series, 3600)
            except Exception as e:
                print(f"Error fetching top series: {e}")

    return {
        'movie_rows': movie_rows_data,
        'series_rows': series_rows_data,
        'top_movies': top_movies,
        'top_series': top_series,
        'current_month_data': current_month_data,
    }

def index(request):
    return render(request, 'core/index.html')


def home_initial_data(request):
    try:
        context = _build_home_data()
        html = render_to_string('core/_home_content.html', context, request=request)
        return JsonResponse({'html': html})
    except Exception as e:
        print(f"Error loading homepage data: {e}")
        return JsonResponse({'html': '', 'error': 'Unable to load homepage data right now.'}, status=500)


def is_staff_or_superuser(user):
    return user.is_staff or user.is_superuser

@login_required
@user_passes_test(is_staff_or_superuser)
def admin_dashboard(request):
    site_settings = SiteSettings.get_settings()
    return render(request, 'core/admin_dashboard.html', {'site_settings': site_settings})


def movie_list(request):
    client = get_data_client()
    search_client = TMDBClient()
    site_settings = SiteSettings.get_settings()

    search_query = request.GET.get('search', '')
    genre_id = request.GET.get('genre', '')
    sort_by = request.GET.get('sort', '')
    order = request.GET.get('order', 'desc')
    filter_type = request.GET.get('filter_type', '')
    provider_slug = request.GET.get('provider', '')
    page = int(request.GET.get('page', 1))

    cache_key = f"movie_list_{search_query}_{genre_id}_{sort_by}_{order}_{filter_type}_{provider_slug}_{page}"
    cached_result = cache.get(cache_key)
    if cached_result:
        items, total_pages, all_genres = cached_result
    else:
        params = {'page': page}
        if genre_id:
            params['with_genres'] = genre_id

        if search_query:
            data = search_client.search_movies(search_query, page=page)
        else:
            if genre_id:
                data = client.discover_movies(params)
            else:
                if filter_type == 'popular':
                    data = client.get_popular_movies(page=page, params=params)
                elif filter_type == 'top_rated':
                    data = client.get_top_rated_movies(page=page, params=params)
                elif filter_type == 'upcoming':
                    data = client.get_upcoming_movies(page=page, params=params)
                else:
                    data = client.get_now_playing_movies(page=page, params=params)

        items = []
        for item in data.get('results', []):
            processed_item = item.copy()
            processed_item['title'] = item.get('title', 'Unknown Title')
            processed_slug = slugify(processed_item['title']) or f"movie-{item.get('id', 'unknown')}"
            processed_item['slug'] = processed_slug
            processed_item['year'] = item.get('release_date', '')[:4] if item.get('release_date') else ''
            processed_item['vote_average'] = item.get('vote_average', 0)
            processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
            processed_item['id'] = item.get('id')
            processed_item['poster_path'] = item.get('poster_path')
            processed_item['overview'] = item.get('overview', '')
            processed_item['release_date'] = item.get('release_date')
            processed_item['media_type'] = 'movie'
            items.append(processed_item)

        if provider_slug:
            items = [item for item in items if _provider_slug_matches(item, provider_slug)]

        total_pages = data.get('total_pages', 1)
        all_genres = cache.get('movie_genres')
        if not all_genres:
            try:
                all_genres = client.get_movie_genres().get('genres', [])
                cache.set('movie_genres', all_genres, 3600 * 24)
            except Exception as e:
                print(f"Error fetching genres: {e}")
                all_genres = []

        cache.set(cache_key, (items, total_pages, all_genres), 1800)

    has_next = page < total_pages
    base_col = 12 // site_settings.items_per_row
    col_class = f"col-{base_col} col-sm-{max(1, base_col-1)} col-md-{base_col} col-lg-{max(1, base_col-2)} col-xl-{max(1, base_col-3)}"
    image_heights = {'small': '200px', 'medium': '300px', 'large': '400px'}
    image_height = image_heights[site_settings.card_size]

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        html = render_to_string('core/_movie_cards.html', {'movies': items, 'col_class': col_class, 'image_height': image_height})
        return JsonResponse({'html': html, 'has_next': has_next})
    return render(request, 'core/movie_list.html', {
        'movies': items,
        'search_query': search_query,
        'sort_by': sort_by,
        'order': order,
        'genre_id': genre_id,
        'filter_type': filter_type,
        'provider_slug': provider_slug,
        'all_genres': all_genres,
        'has_next': has_next,
        'col_class': col_class,
        'image_height': image_height
    })


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
    provider_slug = request.GET.get('provider', '')
    page = int(request.GET.get('page', 1))
    
    # Create cache key based on all filters and page
    cache_key = f"series_list_{search_query}_{genre_id}_{sort_by}_{order}_{filter_type}_{provider_slug}_{page}"
    cached_result = cache.get(cache_key)
    if cached_result:
        items, total_pages, all_genres = cached_result
    else:
        params = {'page': page}

        # Handle genre
        if genre_id:
            params['with_genres'] = genre_id

        # Handle search
        if search_query:
            data = search_client.search_series(search_query, page=page)
        else:
            # If genre is selected, always use discover endpoint
            if genre_id:
                data = client.discover_series(params)
            else:
                if filter_type == 'popular':
                    data = client.get_popular_series(page=page, params=params)
                elif filter_type == 'top_rated':
                    data = client.get_top_rated_series(page=page, params=params)
                elif filter_type == 'airing_today':
                    data = client.get_airing_today_series(page=page, params=params)
                elif filter_type == 'on_the_air':
                    data = client.get_on_the_air_series(page=page, params=params)
                else:  # Latest (on the air)
                    data = client.get_on_the_air_series(page=page, params=params)

        # Process results
        items = []
        for item in data.get('results', []):
            processed_item = item.copy()
            processed_item['title'] = item.get('name', 'Unknown Title')
            processed_slug = slugify(processed_item['title'])
            if not processed_slug:
                processed_slug = f"series-{item.get('id', 'unknown')}"
            processed_item['slug'] = processed_slug
            processed_item['vote_average'] = item.get('vote_average', 0)
            processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
            processed_item['id'] = item.get('id')
            processed_item['poster_path'] = item.get('poster_path')
            processed_item['overview'] = item.get('overview', '')
            processed_item['first_air_date'] = item.get('first_air_date')
            processed_item['media_type'] = 'tv'
            items.append(processed_item)

        if provider_slug:
            items = [item for item in items if _provider_slug_matches(item, provider_slug)]

        total_pages = data.get('total_pages', 1)

        # Get all series genres from API (cached)
        all_genres = cache.get('series_genres')
        if not all_genres:
            try:
                all_genres = client.get_series_genres().get('genres', [])
                cache.set('series_genres', all_genres, 3600 * 24)  # Cache for 24 hours
            except Exception as e:
                print(f"Error fetching genres: {e}")
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
        html = render_to_string('core/_series_cards.html', {
            'series': items,
            'col_class': col_class,
            'image_height': image_height
        })
        return JsonResponse({'html': html, 'has_next': has_next})
    return render(request, 'core/series_list.html', {
        'series': items,
        'search_query': search_query,
        'sort_by': sort_by,
        'order': order,
        'genre_id': genre_id,
        'filter_type': filter_type,
        'all_genres': all_genres,
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
        html = render_to_string('core/_movie_cards.html', {'movies': items})
    else:
        html = render_to_string('core/_series_cards.html', {'series': items})

    return JsonResponse({
        'html': html,
        'has_next': has_next,
        'next_page': page + 1
    })


def movie_detail_by_id(request, movie_id):
    client = get_data_client()
    site_settings = SiteSettings.get_settings()
    
    # Check cache for movie detail first
    cache_key = f"movie_detail_id_{movie_id}"
    cached_result = cache.get(cache_key)
    if cached_result:
        processed_movie, more_movies, watch_providers = cached_result
    else:
        # Get movie by ID directly!
        movie = client.get_movie_details(movie_id)
        
        if not movie:
            # If no movie found, 404
            return render(request, '404.html', status=404)

        # Process movie data for template
        processed_movie = movie.copy()
        processed_movie['title'] = movie.get('title', 'Unknown Title')
        processed_slug = slugify(processed_movie['title'])
        if not processed_slug:
            processed_slug = f"movie-{movie_id}"
        processed_movie['slug'] = processed_slug
        processed_movie['year'] = movie.get('release_date', '')[:4] if movie.get('release_date') else ''
        processed_movie['vote_average'] = movie.get('vote_average', 0)
        processed_movie['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{movie['poster_path']}" if movie.get('poster_path') else None
        processed_movie['backdrop_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{movie['backdrop_path']}" if movie.get('backdrop_path') else None
        processed_movie['id'] = movie.get('id')
        processed_movie['media_type'] = 'movie'
        
        # Add more TMDB details
        processed_movie['tagline'] = movie.get('tagline', '')
        processed_movie['runtime'] = movie.get('runtime', 0)
        processed_movie['status'] = movie.get('status', '')
        processed_movie['release_date'] = movie.get('release_date', '')
        processed_movie['genres'] = movie.get('genres', [])
        
        # Get similar movies
        more_movies = []
        try:
            similar_data = client.get_similar_movies(movie_id, page=1)
            for item in similar_data.get('results', [])[:12]:
                processed_item = item.copy()
                processed_item['title'] = item.get('title', 'Unknown Title')
                processed_slug = slugify(processed_item['title'])
                if not processed_slug:
                    processed_slug = f"movie-{item.get('id', 'unknown')}"
                processed_item['slug'] = processed_slug
                processed_item['vote_average'] = item.get('vote_average', 0)
                processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                processed_item['id'] = item.get('id')
                processed_item['poster_path'] = item.get('poster_path')
                processed_item['overview'] = item.get('overview', '')
                processed_item['release_date'] = item.get('release_date')
                processed_item['media_type'] = 'movie'
                more_movies.append(processed_item)
        except Exception as e:
            print(f"Error fetching similar movies: {e}")

        # Get watch providers
        watch_providers = None
        try:
            providers_data = client.get_movie_watch_providers(movie_id)
            watch_providers = providers_data.get('results', {})
        except Exception as e:
            print(f"Error fetching movie watch providers: {e}")
        
        # Cache for 1 hour
        cache.set(cache_key, (processed_movie, more_movies, watch_providers), 3600)
    
    if site_settings.url_format == 'slug':
        return redirect('movie_detail', movie_slug=processed_movie['slug'])

    # Get provider URLs
    provider_urls = {
        provider.name.lower(): provider.url
        for provider in ProviderItem.objects.exclude(url__isnull=True).exclude(url='')
    }

    # Get player configurations
    active_player = site_settings.active_movie_player
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['movie', 'both'],
        is_active=True
    ).order_by('order', 'id')
    return render(request, 'core/movie_detail.html', {
        'movie': processed_movie,
        'more_movies': more_movies,
        'watch_providers': watch_providers,
        'watch_region': site_settings.watch_region or 'US',
        'active_player': active_player,
        'all_players': all_players,
        'movie_id': movie_id,
        'provider_urls': provider_urls
    })

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
        processed_movie = movie.copy()
        processed_movie['title'] = movie.get('title', 'Unknown Title')
        processed_slug = slugify(processed_movie['title'])
        if not processed_slug:
            processed_slug = f"movie-{movie_id}"
        processed_movie['slug'] = processed_slug
        processed_movie['year'] = movie.get('release_date', '')[:4] if movie.get('release_date') else ''
        processed_movie['vote_average'] = movie.get('vote_average', 0)
        processed_movie['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{movie['poster_path']}" if movie.get('poster_path') else None
        processed_movie['backdrop_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{movie['backdrop_path']}" if movie.get('backdrop_path') else None
        processed_movie['id'] = movie.get('id')
        processed_movie['media_type'] = 'movie'
        
        # Add more TMDB details
        processed_movie['tagline'] = movie.get('tagline', '')
        processed_movie['runtime'] = movie.get('runtime', 0)
        processed_movie['status'] = movie.get('status', '')
        processed_movie['release_date'] = movie.get('release_date', '')
        processed_movie['genres'] = movie.get('genres', [])
        processed_movie['vote_count'] = movie.get('vote_count', 0)
        processed_movie['popularity'] = movie.get('popularity', 0)
        processed_movie['original_language'] = movie.get('original_language', '')

        # Get similar movies
        more_movies = []
        try:
            similar_data = client.get_similar_movies(movie_id, page=1)
            for item in similar_data.get('results', [])[:12]:
                processed_item = item.copy()
                processed_item['title'] = item.get('title', 'Unknown Title')
                processed_slug = slugify(processed_item['title'])
                if not processed_slug:
                    processed_slug = f"movie-{item.get('id', 'unknown')}"
                processed_item['slug'] = processed_slug
                processed_item['vote_average'] = item.get('vote_average', 0)
                processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                processed_item['id'] = item.get('id')
                processed_item['poster_path'] = item.get('poster_path')
                processed_item['overview'] = item.get('overview', '')
                processed_item['release_date'] = item.get('release_date')
                processed_item['media_type'] = 'movie'
                more_movies.append(processed_item)
        except Exception as e:
            print(f"Error fetching similar movies: {e}")

        # Get watch providers
        watch_providers = None
        try:
            providers_data = client.get_movie_watch_providers(movie_id)
            watch_providers = providers_data.get('results', {})
        except Exception as e:
            print(f"Error fetching movie watch providers: {e}")
        
        # Cache for 1 hour
        cache.set(cache_key, (processed_movie, more_movies, movie_id, watch_providers), 3600)

    if site_settings.url_format == 'id' and movie_id is not None:
        return redirect('movie_detail_by_id', movie_id=movie_id)

    # Get provider URLs
    provider_urls = {
        provider.name.lower(): provider.url
        for provider in ProviderItem.objects.exclude(url__isnull=True).exclude(url='')
    }

    # Get player configurations
    active_player = site_settings.active_movie_player
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['movie', 'both'],
        is_active=True
    ).order_by('order', 'id')
    return render(request, 'core/movie_detail.html', {
        'movie': processed_movie,
        'more_movies': more_movies,
        'watch_providers': watch_providers,
        'watch_region': site_settings.watch_region or 'US',
        'active_player': active_player,
        'all_players': all_players,
        'movie_id': movie_id,
        'provider_urls': provider_urls
    })


def series_detail_by_id(request, series_id):
    client = get_data_client()
    site_settings = SiteSettings.get_settings()
    
    # Get season and episode from request, default to 1
    season_number = int(request.GET.get('season', 1))
    episode_number = int(request.GET.get('episode', 1))
    
    # Check cache for series detail with season
    cache_key = f"series_detail_id_{series_id}_{season_number}"
    cached_result = cache.get(cache_key)
    if cached_result:
        processed_series, seasons, episodes, more_series, watch_providers = cached_result
    else:
        # Get series by ID directly!
        series_details = client.get_series_details(series_id)
        
        if not series_details:
            return render(request, '404.html', status=404)

        # Process series data
        processed_series = series_details.copy()
        processed_series['title'] = series_details.get('name', 'Unknown Title')
        processed_slug = slugify(processed_series['title'])
        if not processed_slug:
            processed_slug = f"series-{series_id}"
        processed_series['slug'] = processed_slug
        processed_series['vote_average'] = series_details.get('vote_average', 0)
        processed_series['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{series_details['poster_path']}" if series_details.get('poster_path') else None
        processed_series['backdrop_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{series_details['backdrop_path']}" if series_details.get('backdrop_path') else None
        processed_series['id'] = series_details.get('id')
        processed_series['media_type'] = 'tv'
        
        # Add more TMDB details
        processed_series['tagline'] = series_details.get('tagline', '')
        processed_series['status'] = series_details.get('status', '')
        processed_series['first_air_date'] = series_details.get('first_air_date', '')
        processed_series['last_air_date'] = series_details.get('last_air_date', '')
        processed_series['number_of_seasons'] = series_details.get('number_of_seasons', 0)
        processed_series['number_of_episodes'] = series_details.get('number_of_episodes', 0)
        processed_series['genres'] = series_details.get('genres', [])
        processed_series['vote_count'] = series_details.get('vote_count', 0)
        processed_series['popularity'] = series_details.get('popularity', 0)
        processed_series['original_language'] = series_details.get('original_language', '')

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
            print(f"Error fetching season details: {e}")

        # Get similar series
        more_series = []
        try:
            similar_data = client.get_similar_series(series_id, page=1)
            for item in similar_data.get('results', [])[:12]:
                processed_item = item.copy()
                processed_item['title'] = item.get('name', 'Unknown Title')
                processed_slug = slugify(processed_item['title'])
                if not processed_slug:
                    processed_slug = f"series-{item.get('id', 'unknown')}"
                processed_item['slug'] = processed_slug
                processed_item['vote_average'] = item.get('vote_average', 0)
                processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                processed_item['id'] = item.get('id')
                processed_item['poster_path'] = item.get('poster_path')
                processed_item['overview'] = item.get('overview', '')
                processed_item['first_air_date'] = item.get('first_air_date')
                processed_item['media_type'] = 'tv'
                more_series.append(processed_item)
        except Exception as e:
            print(f"Error fetching similar series: {e}")

        # Get watch providers
        watch_providers = None
        try:
            providers_data = client.get_series_watch_providers(series_id)
            watch_providers = providers_data.get('results', {})
        except Exception as e:
            print(f"Error fetching series watch providers: {e}")
        
        # Cache for 1 hour
        cache.set(cache_key, (processed_series, seasons, episodes, more_series, watch_providers), 3600)
    
    if site_settings.url_format == 'slug':
        return redirect(f"{redirect('series_detail', series_slug=processed_series['slug']).url}?season={season_number}&episode={episode_number}")

    # Get provider URLs
    provider_urls = {
        provider.name.lower(): provider.url
        for provider in ProviderItem.objects.exclude(url__isnull=True).exclude(url='')
    }

    # Get player configurations
    active_player = site_settings.active_tv_player
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['tv', 'both'],
        is_active=True
    ).order_by('order', 'id')
    return render(request, 'core/series_detail.html', {
        'series': processed_series,
        'seasons': seasons,
        'episodes': episodes,
        'current_season': season_number,
        'current_episode': episode_number,
        'more_series': more_series,
        'watch_providers': watch_providers,
        'watch_region': site_settings.watch_region or 'US',
        'active_player': active_player,
        'all_players': all_players,
        'series_id': series_id,
        'provider_urls': provider_urls
    })

def series_season_episodes(request, series_id, season_number):
    client = get_data_client()

    try:
        season_details = client.get_season_details(series_id, season_number)
        return JsonResponse({'episodes': season_details.get('episodes', [])})
    except Exception as e:
        return JsonResponse({'episodes': [], 'error': str(e)}, status=500)


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
        processed_series = series_details.copy()
        processed_series['title'] = series_details.get('name', 'Unknown Title')
        processed_slug = slugify(processed_series['title'])
        if not processed_slug:
            processed_slug = f"series-{series_id}"
        processed_series['slug'] = processed_slug
        processed_series['vote_average'] = series_details.get('vote_average', 0)
        processed_series['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{series_details['poster_path']}" if series_details.get('poster_path') else None
        processed_series['backdrop_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{series_details['backdrop_path']}" if series_details.get('backdrop_path') else None
        processed_series['id'] = series_details.get('id')
        processed_series['media_type'] = 'tv'
        
        # Add more TMDB details
        processed_series['tagline'] = series_details.get('tagline', '')
        processed_series['status'] = series_details.get('status', '')
        processed_series['first_air_date'] = series_details.get('first_air_date', '')
        processed_series['last_air_date'] = series_details.get('last_air_date', '')
        processed_series['number_of_seasons'] = series_details.get('number_of_seasons', 0)
        processed_series['number_of_episodes'] = series_details.get('number_of_episodes', 0)
        processed_series['genres'] = series_details.get('genres', [])
        processed_series['vote_count'] = series_details.get('vote_count', 0)
        processed_series['popularity'] = series_details.get('popularity', 0)
        processed_series['original_language'] = series_details.get('original_language', '')

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
            print(f"Error fetching season details: {e}")

        # Get similar series
        more_series = []
        try:
            similar_data = client.get_similar_series(series_id, page=1)
            for item in similar_data.get('results', [])[:12]:
                processed_item = item.copy()
                processed_item['title'] = item.get('name', 'Unknown Title')
                processed_slug = slugify(processed_item['title'])
                if not processed_slug:
                    processed_slug = f"series-{item.get('id', 'unknown')}"
                processed_item['slug'] = processed_slug
                processed_item['vote_average'] = item.get('vote_average', 0)
                processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                processed_item['id'] = item.get('id')
                processed_item['poster_path'] = item.get('poster_path')
                processed_item['overview'] = item.get('overview', '')
                processed_item['first_air_date'] = item.get('first_air_date')
                processed_item['media_type'] = 'tv'
                more_series.append(processed_item)
        except Exception as e:
            print(f"Error fetching similar series: {e}")

        # Get watch providers
        watch_providers = None
        try:
            providers_data = client.get_series_watch_providers(series_id)
            watch_providers = providers_data.get('results', {})
        except Exception as e:
            print(f"Error fetching series watch providers: {e}")
        
        # Cache for 1 hour
        cache.set(cache_key, (processed_series, seasons, episodes, more_series, series_id, watch_providers), 3600)

    if site_settings.url_format == 'id' and series_id is not None:
        return redirect(f"{redirect('series_detail_by_id', series_id=series_id).url}?season={season_number}&episode={episode_number}")

    # Get provider URLs
    provider_urls = {
        provider.name.lower(): provider.url
        for provider in ProviderItem.objects.exclude(url__isnull=True).exclude(url='')
    }

    # Get player configurations
    active_player = site_settings.active_tv_player
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['tv', 'both'],
        is_active=True
    ).order_by('order', 'id')
    return render(request, 'core/series_detail.html', {
        'series': processed_series,
        'seasons': seasons,
        'episodes': episodes,
        'current_season': season_number,
        'current_episode': episode_number,
        'more_series': more_series,
        'watch_providers': watch_providers,
        'watch_region': site_settings.watch_region or 'US',
        'active_player': active_player,
        'all_players': all_players,
        'series_id': series_id,
        'provider_urls': provider_urls
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
def ads_settings(request):
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = AdsSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = AdsSettingsForm(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'title': 'Ads Settings',
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
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = EmailSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = EmailSettingsForm(instance=site_settings)
    return render(request, 'core/settings_section.html', {
        'form': form,
        'title': 'Email Settings',
        'back_url': 'admin_dashboard',
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
def toggle_ads(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'})
    try:
        settings_obj = SiteSettings.get_settings()
        enabled_value = request.POST.get('enabled')
        if enabled_value is None:
            return JsonResponse({'success': False, 'message': 'Missing enabled state'})
        settings_obj.enable_sidebar_ads = str(enabled_value).lower() in ['true', '1', 'yes', 'on']
        settings_obj.save()
        return JsonResponse({'success': True, 'message': 'Ads setting updated!', 'enable_sidebar_ads': settings_obj.enable_sidebar_ads})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error updating ads setting: {str(e)}'})


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
    providers = ProviderItem.objects.all().order_by('name')
    return render(request, 'core/provider_item_list.html', {'providers': providers})


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
def provider_item_sync(request):
    from .provider_sync import sync_provider_items_once
    sync_provider_items_once()
    return redirect('provider_item_list')


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
def android_app_dashboard(request, app_id=None):
    apps = AndroidApp.objects.all().order_by('name')
    selected_app = None
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
def android_app_integration_guide(request, app_id):
    android_app = get_object_or_404(AndroidApp, id=app_id)
    app_endpoint = request.build_absolute_uri(f"/api/android-apps/{android_app.slug}/")
    sample_success_payload = json.dumps(android_app.json_payload, indent=2)
    sample_update_payload = {
        'status': 'update_required',
        'message': 'Update the app with the latest APK URL.',
        'expected_build_id': android_app.allowed_build_id or 'your-build-id',
    }
    if android_app.apk_file:
        sample_update_payload['apk_url'] = request.build_absolute_uri(android_app.apk_file.url)

    basic_auth_value = base64.b64encode(
        f"{android_app.access_username}:{android_app.access_password}".encode('utf-8')
    ).decode('utf-8')

    return render(request, 'core/android_app_integration_guide.html', {
        'android_app': android_app,
        'app_endpoint': app_endpoint,
        'sample_success_payload': sample_success_payload,
        'sample_update_payload_json': json.dumps(sample_update_payload, indent=2),
        'basic_auth_value': basic_auth_value,
    })


@login_required
@user_passes_test(is_staff_or_superuser)
def ajax_android_app_dashboard(request, app_id):
    selected_app = get_object_or_404(AndroidApp, id=app_id)
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
        'visited_at', 'device__user_id', 'device_model', 'os_version', 'build_identifier', 'ip_address'
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
        
        # Record individual visit
        AndroidAppDeviceVisit.objects.create(
            device=device,
            android_app=android_app,
            build_identifier=build_identifier,
            request_identity=request_identity,
            ip_address=ip_address,
            device_model=device_model,
            os_version=os_version
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
    # Update the payload
    response_payload = android_app.json_payload.copy() if isinstance(android_app.json_payload, dict) else android_app.json_payload
    if isinstance(response_payload, dict):
        response_payload['movie_servers'] = movie_servers
        response_payload['series_servers'] = series_servers
    return JsonResponse(response_payload, safe=isinstance(response_payload, dict))


def ajax_login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return JsonResponse({'success': True, 'message': 'Login successful'})
        return JsonResponse({'success': False, 'message': 'Invalid username or password'})
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


def ajax_register(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        
        if password != confirm_password:
            return JsonResponse({'success': False, 'message': 'Passwords do not match'})
        
        if User.objects.filter(username=username).exists():
            return JsonResponse({'success': False, 'message': 'Username already taken'})
        
        if User.objects.filter(email=email).exists():
            return JsonResponse({'success': False, 'message': 'Email already registered'})
        
        user = User.objects.create_user(username=username, email=email, password=password)
        login(request, user)
        return JsonResponse({'success': True, 'message': 'Registration successful'})
    return JsonResponse({'success': False, 'message': 'Method not allowed'})


def ajax_logout(request):
    logout(request)
    return JsonResponse({'success': True, 'message': 'Logged out successfully'})


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
            send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email])
            
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
        title = request.POST.get('title')
        poster_path = request.POST.get('poster_path')

        watchlist_item, created = WatchList.objects.get_or_create(
            user=request.user,
            tmdb_id=tmdb_id,
            media_type=media_type,
            defaults={
                'title': title,
                'poster_path': poster_path
            }
        )

        if created:
            return JsonResponse({'success': True, 'action': 'added', 'message': 'Added to watchlist'})

        return JsonResponse({'success': True, 'action': 'exists', 'message': 'Already in watchlist'})

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
    return JsonResponse({'in_watchlist': in_watchlist})
4

@login_required
def watchlist(request):
    watchlist_items = WatchList.objects.filter(user=request.user)
    
    movie_items = []
    series_items = []
    
    for item in watchlist_items:
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
        print(f"Error fetching Wikipedia details: {e}")
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
            for item in movie_data.get('results', []):
                processed_item = item.copy()
                processed_item['title'] = item.get('title', 'Unknown Title')
                processed_slug = slugify(processed_item['title'])
                if not processed_slug:
                    processed_slug = f"movie-{item.get('id', 'unknown')}"
                processed_item['slug'] = processed_slug
                processed_item['year'] = item.get('release_date', '')[:4] if item.get('release_date') else ''
                processed_item['vote_average'] = item.get('vote_average', 0)
                processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                processed_item['id'] = item.get('id')
                processed_item['poster_path'] = item.get('poster_path')
                processed_item['overview'] = item.get('overview', '')
                processed_item['release_date'] = item.get('release_date')
                processed_item['media_type'] = 'movie'
                movie_items.append(processed_item)

            # Search for series
            series_data = search_client.search_series(search_query)
            for item in series_data.get('results', []):
                processed_item = item.copy()
                processed_item['title'] = item.get('name', 'Unknown Title')
                processed_slug = slugify(processed_item['title'])
                if not processed_slug:
                    processed_slug = f"series-{item.get('id', 'unknown')}"
                processed_item['slug'] = processed_slug
                processed_item['vote_average'] = item.get('vote_average', 0)
                processed_item['cover_url'] = f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}" if item.get('poster_path') else None
                processed_item['id'] = item.get('id')
                processed_item['poster_path'] = item.get('poster_path')
                processed_item['overview'] = item.get('overview', '')
                processed_item['first_air_date'] = item.get('first_air_date')
                processed_item['media_type'] = 'tv'
                series_items.append(processed_item)
        
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
        print(f"Error fetching calendar movies: {e}")
    
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
                movies.extend(page_results)
                total_pages = data.get('total_pages', 1)
                if current_page >= total_pages:
                    break
                current_page += 1
        except Exception as e:
            print(f"Error fetching movies: {e}")
        
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
                series.extend(page_results)
                total_pages = data.get('total_pages', 1)
                if current_page >= total_pages:
                    break
                current_page += 1
        except Exception as e:
            print(f"Error fetching series: {e}")
        
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
        print(f"Error fetching calendar series: {e}")
    
    cache.set(cache_key, series, 3600)  # Cache for 1 hour
    return JsonResponse(series, safe=False)


def extract_video_url(request):
    """AJAX endpoint to extract direct video URL from embed URL"""
    from urllib.parse import urlparse, parse_qs, urljoin
    import re
    
    embed_url = request.GET.get('url', '')
    
    if not embed_url:
        return JsonResponse({'success': False, 'error': 'No URL provided'})
    
    cache_key = f"extract_url_{hash(embed_url)}"
    cached_result = cache.get(cache_key)
    
    if cached_result:
        return JsonResponse(cached_result)
    
    try:
        extracted_url = None
        extraction_steps = []
        
        # Method 1: Check for direct video extensions in URL
        extraction_steps.append("Checking for direct video extensions...")
        direct_video_patterns = [
            r'\.(mp4|webm|m3u8|ts|mov|avi|mkv)$',
            r'/video/',
            r'/stream/',
            r'/play/',
            r'/watch/',
            r'/vid/'
        ]
        
        for pattern in direct_video_patterns:
            if re.search(pattern, embed_url, re.IGNORECASE):
                extracted_url = embed_url
                extraction_steps.append(f"✓ Direct video pattern found: {pattern}")
                break
        
        # Method 2: Try to fetch and parse the page content
        if not extracted_url:
            extraction_steps.append("Attempting to fetch and parse page...")
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5'
                }
                response = requests.get(embed_url, headers=headers, timeout=10, allow_redirects=True)
                extraction_steps.append(f"Page loaded successfully (status: {response.status_code})")
                
                # First, let's get the API endpoint from the page
                parsed_embed_url = urlparse(embed_url)
                query_params = parse_qs(parsed_embed_url.query)
                api_key = query_params.get('apikey', [''])[0]
                
                parts = parsed_embed_url.path.strip('/').split('/')
                if len(parts) >= 2 and parts[0] == 'embed':
                    type = parts[1]
                    if type == 'movie' and len(parts) >= 3:
                        movie_id = parts[2]
                        # This is it! The API endpoint!
                        api_endpoint = f"{parsed_embed_url.scheme}://{parsed_embed_url.netloc}/api/movie/{movie_id}?apikey={api_key}"
                        extraction_steps.append(f"✓ Found API endpoint: {api_endpoint}")
                        
                        # Call the API to get sources
                        api_response = requests.get(api_endpoint, headers=headers, timeout=10)
                        api_data = api_response.json()
                        
                        if api_data.get('success') and api_data.get('sources'):
                            # Get the first source!
                            first_source = api_data['sources'][0]
                            extracted_url = first_source.get('url')
                            extraction_steps.append(f"✓ Got source from API: {extracted_url[:80]}...")
                
                # Fallback if that doesn't work
                if not extracted_url:
                    # Look for common video patterns in the page
                    page_content = response.text
                    
                    # First priority: Find any and all m3u8 links
                    extraction_steps.append("Searching for m3u8 playlist links in main page...")
                    m3u8_patterns = [
                        r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*',
                        r'["\']([^"\']+\.m3u8[^"\']*)["\']'
                    ]
                    for pattern in m3u8_patterns:
                        matches = re.findall(pattern, page_content)
                        if matches:
                            for match in matches:
                                if match.startswith(('http://', 'https://')):
                                    extracted_url = match
                                else:
                                    extracted_url = urljoin(response.url, match)
                                extraction_steps.append(f"✓ Found m3u8 playlist: {extracted_url[:80]}...")
                                break
                            if extracted_url:
                                break
                
            except Exception as e:
                extraction_steps.append(f"Page fetch failed: {e}")
        
        # Method 3: Parse and analyze CodeSpecters URL
        if not extracted_url:
            extraction_steps.append("Analyzing CodeSpecters URL...")
            parsed = urlparse(embed_url)
            query_params = parse_qs(parsed.query)
            
            if 'codespecters.com' in parsed.netloc:
                # Try CodeSpecters API endpoints
                path_parts = parsed.path.strip('/').split('/')
                
                if len(path_parts) >= 2 and path_parts[0] == 'movie':
                    movie_id = path_parts[1]
                    api_key = query_params.get('apikey', [''])[0]
                    extraction_steps.append(f"Found movie ID: {movie_id}")
                    
                    # Try common streaming endpoints
                    possible_endpoints = [
                        f"{parsed.scheme}://{parsed.netloc}/api/stream/movie/{movie_id}?apikey={api_key}",
                        f"{parsed.scheme}://{parsed.netloc}/api/video/{movie_id}",
                        f"{parsed.scheme}://api.codespecters.com/stream/movie/{movie_id}",
                    ]
                    
                    for i, endpoint in enumerate(possible_endpoints):
                        extraction_steps.append(f"Trying endpoint {i+1}: {endpoint}")
                        try:
                            test_response = requests.head(endpoint, timeout=5, allow_redirects=True)
                            if test_response.status_code in [200, 301, 302]:
                                extracted_url = endpoint
                                extraction_steps.append(f"✓ Endpoint responded successfully")
                                break
                        except Exception as e:
                            extraction_steps.append(f"Endpoint failed: {e}")
                            continue
                
                if len(path_parts) >= 4 and path_parts[0] == 'tv':
                    series_id = path_parts[1]
                    season = path_parts[2]
                    episode = path_parts[3]
                    api_key = query_params.get('apikey', [''])[0]
                    
                    possible_endpoints = [
                        f"{parsed.scheme}://{parsed.netloc}/api/stream/tv/{series_id}/{season}/{episode}?apikey={api_key}",
                    ]
                    
                    for endpoint in possible_endpoints:
                        try:
                            test_response = requests.head(endpoint, timeout=5, allow_redirects=True)
                            if test_response.status_code in [200, 301, 302]:
                                extracted_url = endpoint
                                break
                        except:
                            continue
        
        # Method 4: Fallback to original URL if nothing found
        if not extracted_url:
            extraction_steps.append("No direct link found, using original URL as fallback")
            extracted_url = embed_url
        
        result = {
            'success': True,
            'original_url': embed_url,
            'extracted_url': extracted_url,
            'method': 'fallback' if extracted_url == embed_url else 'parsed',
            'steps': extraction_steps
        }
        
        cache.set(cache_key, result, 3600)
        return JsonResponse(result)
        
    except Exception as e:
        print(f"Extraction error: {e}")
        return JsonResponse({
            'success': False,
            'error': str(e),
            'extracted_url': embed_url,
            'steps': [f"Error: {e}"]
        })


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
