from .models import SiteSettings, NavbarItem, ProviderItem
from .tmdb_client import get_data_client
from django.conf import settings as django_settings


def site_settings(request):
    settings = SiteSettings.get_settings()
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

    movie_genres = []
    series_genres = []
    try:
        client = get_data_client()
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

    if NavbarItem.objects.count() == 0:
        defaults = [
            {'name': 'Home', 'built_in_id': 'home', 'item_type': 'built_in', 'order': 0, 'is_active': True, 'icon': 'fas fa-home'},
            {'name': 'Movies', 'built_in_id': 'movies', 'item_type': 'built_in', 'order': 1, 'is_active': True, 'icon': 'fas fa-film'},
            {'name': 'TV Shows', 'built_in_id': 'tv_shows', 'item_type': 'built_in', 'order': 2, 'is_active': True, 'icon': 'fas fa-tv'},
            {'name': 'Live TV', 'built_in_id': 'live_tv', 'item_type': 'built_in', 'order': 3, 'is_active': True, 'icon': 'fas fa-broadcast-tower'},
            {'name': 'My Watch List', 'built_in_id': 'watchlist', 'item_type': 'built_in', 'order': 4, 'is_active': True, 'icon': 'fas fa-bookmark'},
            {'name': 'Genres', 'built_in_id': 'genres', 'item_type': 'built_in', 'order': 5, 'is_active': True, 'icon': 'fas fa-tags'},
            {'name': 'Provider', 'built_in_id': 'provider', 'item_type': 'built_in', 'order': 6, 'is_active': True, 'icon': 'fas fa-play-circle'},
            {'name': 'Calendar', 'built_in_id': 'calendar', 'item_type': 'built_in', 'order': 7, 'is_active': True, 'icon': 'fas fa-calendar-alt'},
            {'name': 'Upcoming', 'built_in_id': 'upcoming', 'item_type': 'built_in', 'order': 8, 'is_active': True, 'icon': 'fas fa-rocket'},
        ]
        for item_data in defaults:
            NavbarItem.objects.create(**item_data)

    navbar_items = NavbarItem.objects.filter(is_active=True).order_by('order')
    enabled_providers = ProviderItem.objects.filter(is_enabled=True).order_by('name')

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
        'series_genres': series_genres,
        'navbar_items': navbar_items,
        'enabled_providers': enabled_providers,
    }
