
from .models import SiteSettings
from .tmdb_client import TMDBClient
from django.conf import settings as django_settings

def site_settings(request):
    settings = SiteSettings.get_settings()
    # Calculate text sizes
    title_sizes = {
        'small': '1rem',
        'medium': '1.25rem',
        'large': '1.5rem',
        'xl': '1.75rem'
    }
    text_sizes = {
        'small': '0.875rem',
        'medium': '1rem',
        'large': '1.125rem',
        'xl': '1.25rem'
    }
    
    # Get genres from TMDB
    movie_genres = []
    series_genres = []
    try:
        client = TMDBClient()
        movie_genres = client.get_movie_genres().get('genres', [])
        series_genres = client.get_series_genres().get('genres', [])
    except Exception as e:
        print(f"Error fetching genres: {e}")
    
    return {
        'site_settings': settings,
        'title_size': title_sizes[settings.title_size],
        'text_size': text_sizes[settings.text_size],
        'settings': django_settings,
        'movie_genres': movie_genres,
        'series_genres': series_genres
    }
