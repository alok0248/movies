
from .models import SiteSettings
from .tmdb_client import TMDBClient
from django.conf import settings as django_settings

def site_settings(request):
    settings = SiteSettings.get_settings()
    # Calculate text sizes
    title_sizes = {
        'small': '0.9rem',
        'medium': '1.1rem',
        'large': '1.3rem',
        'xl': '1.5rem'
    }
    text_sizes = {
        'small': '0.8rem',
        'medium': '0.9rem',
        'large': '1rem',
        'xl': '1.1rem'
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
    
    theme_colors = {
        'netflix': {'primary': '#e50914', 'bg': '#141414', 'bg_secondary': '#1f1f1f'},
        'amazon': {'primary': '#00a8e1', 'bg': '#0f171e', 'bg_secondary': '#1a242e'},
        'hbo': {'primary': '#9333ea', 'bg': '#080808', 'bg_secondary': '#1a1a1a'},
        'disney': {'primary': '#0063e3', 'bg': '#040714', 'bg_secondary': '#0b0d17'},
        'spotify': {'primary': '#1db954', 'bg': '#121212', 'bg_secondary': '#181818'},
    }
    
    theme = theme_colors.get(settings.theme_style, theme_colors['netflix'])
    
    return {
        'site_settings': settings,
        'title_size': title_sizes[settings.title_size],
        'text_size': text_sizes[settings.text_size],
        'font_family': settings.font_family,
        'theme_primary': theme['primary'],
        'theme_bg': theme['bg'],
        'theme_bg_secondary': theme['bg_secondary'],
        'settings': django_settings,
        'movie_genres': movie_genres,
        'series_genres': series_genres
    }
