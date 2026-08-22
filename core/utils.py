"""
Shared utility functions for normalizing TMDB API items into template-ready dicts.
"""
from django.conf import settings
from django.utils.text import slugify


def normalize_movie_item(item):
    """Normalize a raw TMDB movie result dict into a template-ready dict.

    Sets: title, slug, year, vote_average, cover_url, id, poster_path,
          overview, release_date, media_type='movie'
    """
    title = item.get('title') or item.get('name') or item.get('original_title') or 'Unknown Title'
    slug = slugify(title) or f"movie-{item.get('id', 'unknown')}"
    release_date = item.get('release_date') or ''
    return {
        **item,
        'title': title,
        'slug': slug,
        'year': release_date[:4] if release_date else '',
        'vote_average': item.get('vote_average', 0),
        'cover_url': (
            f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}"
            if item.get('poster_path') else None
        ),
        'id': item.get('id'),
        'poster_path': item.get('poster_path'),
        'overview': item.get('overview', ''),
        'release_date': item.get('release_date'),
        'media_type': 'movie',
    }


def normalize_series_item(item):
    """Normalize a raw TMDB series result dict into a template-ready dict.

    Sets: title, slug, vote_average, cover_url, id, poster_path,
          overview, first_air_date, media_type='tv'
    """
    title = item.get('name') or item.get('title') or item.get('original_name') or 'Unknown Title'
    slug = slugify(title) or f"series-{item.get('id', 'unknown')}"
    return {
        **item,
        'title': title,
        'slug': slug,
        'vote_average': item.get('vote_average', 0),
        'cover_url': (
            f"{settings.TMDB_IMAGE_BASE_URL}{item['poster_path']}"
            if item.get('poster_path') else None
        ),
        'id': item.get('id'),
        'poster_path': item.get('poster_path'),
        'overview': item.get('overview', ''),
        'first_air_date': item.get('first_air_date'),
        'media_type': 'tv',
    }


def _extract_names(field):
    """Extract 'name' from a list of dicts, or return as-is if already strings."""
    if not field:
        return ''
    if isinstance(field, str):
        return field
    if isinstance(field, list):
        names = []
        for item in field:
            if isinstance(item, dict):
                n = item.get('name', '')
                if n:
                    names.append(n)
            elif isinstance(item, str):
                names.append(item)
        return ', '.join(names)
    return str(field)


def normalize_movie_detail(item):
    """Normalize a raw TMDB movie detail dict for the detail page."""
    base = normalize_movie_item(item)
    base['backdrop_url'] = (
        f"{settings.TMDB_IMAGE_BASE_URL}{item['backdrop_path']}"
        if item.get('backdrop_path') else None
    )
    base['tagline'] = item.get('tagline', '')
    base['runtime'] = item.get('runtime', 0)
    base['status'] = item.get('status', '')
    base['genres'] = item.get('genres', [])
    base['vote_count'] = item.get('vote_count', 0)
    base['popularity'] = item.get('popularity', 0)
    base['original_language'] = item.get('original_language', '')
    # Extract names from list-of-dict fields
    base['production_countries'] = _extract_names(item.get('production_countries', []))
    base['production_companies_names'] = _extract_names(item.get('production_companies', []))
    base['spoken_languages_names'] = _extract_names(item.get('spoken_languages', []))
    base['origin_country'] = _extract_names(item.get('origin_country', []))
    return base


def normalize_series_detail(item):
    """Normalize a raw TMDB series detail dict for the detail page."""
    base = normalize_series_item(item)
    base['backdrop_url'] = (
        f"{settings.TMDB_IMAGE_BASE_URL}{item['backdrop_path']}"
        if item.get('backdrop_path') else None
    )
    base['tagline'] = item.get('tagline', '')
    base['status'] = item.get('status', '')
    base['first_air_date'] = item.get('first_air_date', '')
    base['last_air_date'] = item.get('last_air_date', '')
    base['number_of_seasons'] = item.get('number_of_seasons', 0)
    base['number_of_episodes'] = item.get('number_of_episodes', 0)
    base['genres'] = item.get('genres', [])
    base['vote_count'] = item.get('vote_count', 0)
    base['popularity'] = item.get('popularity', 0)
    base['original_language'] = item.get('original_language', '')
    # Extract names from list-of-dict fields
    base['production_countries'] = _extract_names(item.get('production_countries', []))
    base['production_companies_names'] = _extract_names(item.get('production_companies', []))
    base['spoken_languages_names'] = _extract_names(item.get('spoken_languages', []))
    base['origin_country'] = _extract_names(item.get('origin_country', []))
    base['networks'] = item.get('networks', [])
    return base
