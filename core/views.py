
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.utils.text import slugify
from django.core.mail import send_mail
from django.core.cache import cache
import json
import requests
from bs4 import BeautifulSoup
from .models import SiteSettings, ContentRow, WatchList
from .tmdb_client import TMDBClient
from .forms import SiteSettingsForm, ContentRowForm


def get_content_row_items(row, page=1):
    """Helper function to fetch items for a ContentRow using TMDB API directly with caching"""
    # Create a unique cache key based on row data and page
    cache_key = f"content_row_{row.id}_page_{page}_{row.media_type}_{row.row_type}_{row.genre_tmdb_id or 'no_genre'}"
    
    # Try to get from cache first
    cached_data = cache.get(cache_key)
    if cached_data:
        return cached_data

    client = TMDBClient()
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
            data = client._make_request('/movie/top_rated', params)
        elif row.row_type == 'upcoming':
            data = client._make_request('/movie/upcoming', params)
        elif row.row_type == 'now_playing':
            data = client._make_request('/movie/now_playing', params)
        else:  # genre or custom
            data = client.discover_movies(params)
    else:  # tv
        if row.row_type == 'popular':
            data = client.get_popular_series(page=page, params=params)
        elif row.row_type == 'top_rated':
            data = client._make_request('/tv/top_rated', params)
        elif row.row_type == 'on_the_air':
            data = client._make_request('/tv/on_the_air', params)
        elif row.row_type == 'airing_today':
            data = client._make_request('/tv/airing_today', params)
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


def index(request):
    # Get active ContentRows
    movie_rows = ContentRow.objects.filter(media_type='movie', is_active=True)
    series_rows = ContentRow.objects.filter(media_type='tv', is_active=True)

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

    return render(request, 'core/index.html', {
        'movie_rows': movie_rows_data,
        'series_rows': series_rows_data,
    })


def admin_dashboard(request):
    site_settings = SiteSettings.get_settings()
    return render(request, 'core/admin_dashboard.html', {'site_settings': site_settings})


def movie_list(request):
    client = TMDBClient()
    site_settings = SiteSettings.get_settings()

    # Get filters
    search_query = request.GET.get('search', '')
    genre_id = request.GET.get('genre', '')
    sort_by = request.GET.get('sort', '')
    order = request.GET.get('order', 'desc')
    filter_type = request.GET.get('filter_type', '')
    page = int(request.GET.get('page', 1))
    
    # Create cache key based on all filters and page
    cache_key = f"movie_list_{search_query}_{genre_id}_{sort_by}_{order}_{filter_type}_{page}"
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
            data = client._make_request('/search/movie', {'query': search_query, 'page': page})
        else:
            # If genre is selected, always use discover endpoint
            if genre_id:
                data = client.discover_movies(params)
            else:
                if filter_type == 'popular':
                    data = client.get_popular_movies(page=page, params=params)
                elif filter_type == 'top_rated':
                    data = client._make_request('/movie/top_rated', params)
                elif filter_type == 'upcoming':
                    data = client._make_request('/movie/upcoming', params)
                else:  # Latest (now playing)
                    data = client._make_request('/movie/now_playing', params)

        # Process results
        items = []
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
            items.append(processed_item)

        total_pages = data.get('total_pages', 1)

        # Get all movie genres from API (cached)
        all_genres = cache.get('movie_genres')
        if not all_genres:
            try:
                all_genres = client.get_movie_genres().get('genres', [])
                cache.set('movie_genres', all_genres, 3600 * 24)  # Cache for 24 hours
            except Exception as e:
                print(f"Error fetching genres: {e}")
                all_genres = []

        # Cache the movie list results for 30 minutes
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
        html = render_to_string('core/_movie_cards.html', {
            'movies': items,
            'col_class': col_class,
            'image_height': image_height
        })
        return JsonResponse({'html': html, 'has_next': has_next})
    return render(request, 'core/movie_list.html', {
        'movies': items,
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


def series_list(request):
    client = TMDBClient()
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
            data = client._make_request('/search/tv', {'query': search_query, 'page': page})
        else:
            # If genre is selected, always use discover endpoint
            if genre_id:
                data = client.discover_series(params)
            else:
                if filter_type == 'popular':
                    data = client.get_popular_series(page=page, params=params)
                elif filter_type == 'top_rated':
                    data = client._make_request('/tv/top_rated', params)
                elif filter_type == 'airing_today':
                    data = client._make_request('/tv/airing_today', params)
                elif filter_type == 'on_the_air':
                    data = client._make_request('/tv/on_the_air', params)
                else:  # Latest (on the air)
                    data = client._make_request('/tv/on_the_air', params)

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


def movie_detail(request, movie_slug):
    client = TMDBClient()
    
    # Check cache for movie detail first
    cache_key = f"movie_detail_{movie_slug}"
    cached_result = cache.get(cache_key)
    if cached_result:
        processed_movie, more_movies, movie_id = cached_result
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

        # Get similar movies
        more_movies = []
        try:
            similar_data = client._make_request(f'/movie/{movie_id}/similar', {'page': 1})
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
        
        # Cache for 1 hour
        cache.set(cache_key, (processed_movie, more_movies, movie_id), 3600)

    player_url = f"{settings.CODESPECTERS_BASE_URL}/movie/{movie_id}?apikey={settings.CODESPECTERS_API_KEY}"
    return render(request, 'core/movie_detail.html', {
        'movie': processed_movie,
        'player_url': player_url,
        'more_movies': more_movies
    })


def series_detail(request, series_slug):
    client = TMDBClient()
    
    # Get season and episode from request, default to 1
    season_number = int(request.GET.get('season', 1))
    episode_number = int(request.GET.get('episode', 1))
    
    # Check cache for series detail with season
    cache_key = f"series_detail_{series_slug}_{season_number}"
    cached_result = cache.get(cache_key)
    if cached_result:
        processed_series, seasons, episodes, more_series, series_id = cached_result
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

        seasons = series_details.get('seasons', [])
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
            similar_data = client._make_request(f'/tv/{series_id}/similar', {'page': 1})
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
        
        # Cache for 1 hour
        cache.set(cache_key, (processed_series, seasons, episodes, more_series, series_id), 3600)

    player_url = f"{settings.CODESPECTERS_BASE_URL}/tv/{series_id}/{season_number}/{episode_number}?apikey={settings.CODESPECTERS_API_KEY}"
    return render(request, 'core/series_detail.html', {
        'series': processed_series,
        'player_url': player_url,
        'seasons': seasons,
        'episodes': episodes,
        'current_season': season_number,
        'current_episode': episode_number,
        'more_series': more_series
    })


def edit_settings(request):
    site_settings = SiteSettings.get_settings()
    if request.method == 'POST':
        form = SiteSettingsForm(request.POST, instance=site_settings)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard')
    else:
        form = SiteSettingsForm(instance=site_settings)
    return render(request, 'core/edit_settings.html', {'form': form})


def content_row_list(request):
    content_rows = ContentRow.objects.all().order_by('order')
    return render(request, 'core/content_row_list.html', {'content_rows': content_rows})


def content_row_create(request):
    if request.method == 'POST':
        form = ContentRowForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('content_row_list')
    else:
        form = ContentRowForm()
    return render(request, 'core/content_row_form.html', {'form': form, 'action': 'Create'})


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


def content_row_delete(request, row_id):
    content_row = get_object_or_404(ContentRow, id=row_id)
    if request.method == 'POST':
        content_row.delete()
        return redirect('content_row_list')
    return render(request, 'core/content_row_delete.html', {'content_row': content_row})


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


def search(request):
    client = TMDBClient()
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
