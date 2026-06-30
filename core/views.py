
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.text import slugify
from django.utils import timezone
from django.core.mail import send_mail
from django.core.cache import cache
from django.db.models import Sum
import json
import datetime
import requests
import calendar
from bs4 import BeautifulSoup
from .models import SiteSettings, ContentRow, WatchList, PlayerConfiguration, TMDBApiKey, NavbarItem, DataSourceUsageLog, ProviderItem, CalendarMonthCache
from .tmdb_client import get_data_client, get_tmdb_db_connection
from .forms import (
    SiteSettingsForm, ContentRowForm, PlayerConfigurationForm, TMDBApiKeyForm, TMDBApiKeyEditForm, NavbarItemForm, ProviderItemForm,
    BrandingSettingsForm, DisplaySettingsForm, DataSourceSettingsForm, TMDBDBSettingsForm,
    PlayerSettingsForm, AdsSettingsForm, URLBlockingSettingsForm, EmailSettingsForm
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
    for region_data in results.values():
        if not isinstance(region_data, dict):
            continue
        for section in ['flatrate', 'rent', 'buy', 'ads', 'free']:
            for provider in region_data.get(section, []) or []:
                name = provider.get('provider_name', '')
                slug = slugify(name)
                if slug == provider_slug:
                    return True
    return False


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


def index(request):
    # Get active ContentRows
    movie_rows = ContentRow.objects.filter(media_type='movie', is_active=True)
    series_rows = ContentRow.objects.filter(media_type='tv', is_active=True)
    site_settings = SiteSettings.get_settings()
    
    # Preload current month and nearby calendar data
    _seed_calendar_month_window()
    today = datetime.date.today()
    current_month_data = get_calendar_month_data(today.year, today.month)

    # Fetch initial items for each row
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
    
    # Fetch top movies and series
    client = get_data_client()
    top_movies = []
    top_series = []
    
    # Check if curated movie IDs are set
    if site_settings.curated_top_movie_ids:
        top_movies_cache_key = f"curated_top_movies_{site_settings.curated_top_movie_ids[:100]}"
        cached_top_movies = cache.get(top_movies_cache_key)
        if cached_top_movies:
            top_movies = cached_top_movies
        else:
            try:
                movie_ids = [int(x.strip()) for x in site_settings.curated_top_movie_ids.split(',') if x.strip().isdigit()]
                for movie_id in movie_ids:
                    item = client.get_movie_details(movie_id)
                    if item:
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
                        top_movies.append(processed_item)
                cache.set(top_movies_cache_key, top_movies, 3600)
            except Exception as e:
                print(f"Error fetching curated top movies: {e}")
    
    # If no curated movies, use top rated
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
                    top_movies.append(processed_item)
                cache.set(top_movies_cache_key, top_movies, 3600)
            except Exception as e:
                print(f"Error fetching top movies: {e}")
    
    # Check if curated series IDs are set
    if site_settings.curated_top_series_ids:
        top_series_cache_key = f"curated_top_series_{site_settings.curated_top_series_ids[:100]}"
        cached_top_series = cache.get(top_series_cache_key)
        if cached_top_series:
            top_series = cached_top_series
        else:
            try:
                series_ids = [int(x.strip()) for x in site_settings.curated_top_series_ids.split(',') if x.strip().isdigit()]
                for series_id in series_ids:
                    item = client.get_series_details(series_id)
                    if item:
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
                        top_series.append(processed_item)
                cache.set(top_series_cache_key, top_series, 3600)
            except Exception as e:
                print(f"Error fetching curated top series: {e}")
    
    # If no curated series, use top rated
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
                    top_series.append(processed_item)
                cache.set(top_series_cache_key, top_series, 3600)
            except Exception as e:
                print(f"Error fetching top series: {e}")

    return render(request, 'core/index.html', {
        'movie_rows': movie_rows_data,
        'series_rows': series_rows_data,
        'top_movies': top_movies,
        'top_series': top_series,
        'current_month_data': current_month_data,
    })


def is_staff_or_superuser(user):
    return user.is_staff or user.is_superuser

@login_required
@user_passes_test(is_staff_or_superuser)
def admin_dashboard(request):
    site_settings = SiteSettings.get_settings()
    return render(request, 'core/admin_dashboard.html', {'site_settings': site_settings})


def movie_list(request):
    client = get_data_client()
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
            data = client.search_movies(search_query, page=page)
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
    site_settings = SiteSettings.get_settings()

    # Get filters
    search_query = request.GET.get('search', '')
    genre_id = request.GET.get('genre', '')
    sort_by = request.GET.get('sort', '')
    order = request.GET.get('order', 'desc')
    filter_type = request.GET.get('filter_type', '')
    page = int(request.GET.get('page', 1))
    
    # Create cache key based on all filters and page
    cache_key = f"series_list_{search_query}_{genre_id}_{sort_by}_{order}_{filter_type}_{page}"
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
            data = client.search_series(search_query, page=page)
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
    
    # Get player configurations
    active_player = site_settings.active_movie_player
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['movie', 'both'],
        is_active=True
    )
    return render(request, 'core/movie_detail.html', {
        'movie': processed_movie,
        'more_movies': more_movies,
        'watch_providers': watch_providers,
        'watch_region': site_settings.watch_region or 'US',
        'active_player': active_player,
        'all_players': all_players,
        'movie_id': movie_id
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
        search_results = client.search_movies(search_query)
        
        # Find the first matching movie (we'll just take the first result for now)
        movie_id = None
        movie = None
        if search_results.get('results'):
            movie_id = search_results['results'][0]['id']
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

    # Get player configurations
    active_player = site_settings.active_movie_player
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['movie', 'both'],
        is_active=True
    )
    return render(request, 'core/movie_detail.html', {
        'movie': processed_movie,
        'more_movies': more_movies,
        'watch_providers': watch_providers,
        'watch_region': site_settings.watch_region or 'US',
        'active_player': active_player,
        'all_players': all_players,
        'movie_id': movie_id
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
    
    # Get player configurations
    active_player = site_settings.active_tv_player
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['tv', 'both'],
        is_active=True
    )
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
        'series_id': series_id
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
        search_results = client.search_series(search_query)
        
        # Find the first matching series
        series_id = None
        series_details = None
        if search_results.get('results'):
            series_id = search_results['results'][0]['id']
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

    # Get player configurations
    active_player = site_settings.active_tv_player
    all_players = PlayerConfiguration.objects.filter(
        media_type__in=['tv', 'both'],
        is_active=True
    )
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
        'series_id': series_id
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
        else:
            watchlist_item.delete()
            return JsonResponse({'success': True, 'action': 'removed', 'message': 'Removed from watchlist'})
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
            movie_data = client.search_movies(search_query)
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
            series_data = client.search_series(search_query)
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
    players = PlayerConfiguration.objects.all()
    return render(request, 'core/player_list.html', {'players': players})


@login_required
@user_passes_test(is_staff_or_superuser)
def player_create(request):
    if request.method == 'POST':
        form = PlayerConfigurationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('player_list')
    else:
        form = PlayerConfigurationForm()
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
    return JsonResponse({'success': False, 'message': 'Method not allowed'})
