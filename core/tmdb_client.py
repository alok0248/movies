import requests
from django.conf import settings
from django.core.paginator import Paginator
from core.models import TMDBMovie, TMDBTV, TMDBGenre, SiteSettings

def get_data_client():
    """Get the appropriate data client based on site settings"""
    settings_obj = SiteSettings.get_settings()
    if settings_obj.data_source == 'local':
        return LocalDBClient()
    return TMDBClient()

class TMDBClient:
    def __init__(self):
        self.api_key = settings.TMDB_API_KEY
        self.base_url = settings.TMDB_BASE_URL
        self.image_base_url = settings.TMDB_IMAGE_BASE_URL

    def _make_request(self, endpoint, params=None):
        if params is None:
            params = {}
        params['api_key'] = self.api_key
        url = f"{self.base_url}{endpoint}"
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def get_popular_movies(self, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = '/movie/popular'
        return self._make_request(endpoint, params)

    def get_top_rated_movies(self, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = '/movie/top_rated'
        return self._make_request(endpoint, params)

    def get_upcoming_movies(self, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = '/movie/upcoming'
        return self._make_request(endpoint, params)

    def get_now_playing_movies(self, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = '/movie/now_playing'
        return self._make_request(endpoint, params)

    def get_popular_series(self, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = '/tv/popular'
        return self._make_request(endpoint, params)

    def get_top_rated_series(self, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = '/tv/top_rated'
        return self._make_request(endpoint, params)

    def get_on_the_air_series(self, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = '/tv/on_the_air'
        return self._make_request(endpoint, params)

    def get_airing_today_series(self, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = '/tv/airing_today'
        return self._make_request(endpoint, params)

    def get_similar_movies(self, movie_id, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = f'/movie/{movie_id}/similar'
        return self._make_request(endpoint, params)

    def get_similar_series(self, series_id, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = f'/tv/{series_id}/similar'
        return self._make_request(endpoint, params)

    def get_movie_genres(self):
        endpoint = '/genre/movie/list'
        return self._make_request(endpoint)

    def get_series_genres(self):
        endpoint = '/genre/tv/list'
        return self._make_request(endpoint)

    def discover_movies(self, params=None):
        endpoint = '/discover/movie'
        return self._make_request(endpoint, params)

    def discover_series(self, params=None):
        endpoint = '/discover/tv'
        return self._make_request(endpoint, params)

    def get_movie_details(self, movie_id):
        endpoint = f'/movie/{movie_id}'
        return self._make_request(endpoint)

    def get_series_details(self, series_id):
        endpoint = f'/tv/{series_id}'
        return self._make_request(endpoint)

    def get_season_details(self, series_id, season_number):
        endpoint = f'/tv/{series_id}/season/{season_number}'
        return self._make_request(endpoint)

    def search_movies(self, query, page=1):
        endpoint = '/search/movie'
        return self._make_request(endpoint, {'query': query, 'page': page})

    def search_series(self, query, page=1):
        endpoint = '/search/tv'
        return self._make_request(endpoint, {'query': query, 'page': page})

    def get_movie_watch_providers(self, movie_id):
        endpoint = f'/movie/{movie_id}/watch/providers'
        return self._make_request(endpoint)

    def get_series_watch_providers(self, series_id):
        endpoint = f'/tv/{series_id}/watch/providers'
        return self._make_request(endpoint)


class LocalDBClient:
    def __init__(self):
        self.image_base_url = settings.TMDB_IMAGE_BASE_URL

    def _movie_to_dict(self, movie):
        return {
            'id': movie.id,
            'adult': movie.adult,
            'backdrop_path': movie.backdrop_path,
            'belongs_to_collection': movie.belongs_to_collection,
            'budget': movie.budget,
            'genres': movie.genres or [],
            'homepage': movie.homepage,
            'imdb_id': movie.imdb_id,
            'original_language': movie.original_language,
            'original_title': movie.original_title,
            'overview': movie.overview,
            'popularity': movie.popularity,
            'poster_path': movie.poster_path,
            'production_companies': movie.production_companies or [],
            'production_countries': movie.production_countries or [],
            'release_date': movie.release_date,
            'revenue': movie.revenue,
            'runtime': movie.runtime,
            'spoken_languages': movie.spoken_languages or [],
            'status': movie.status,
            'tagline': movie.tagline,
            'title': movie.title,
            'video': movie.video,
            'vote_average': movie.vote_average,
            'vote_count': movie.vote_count
        }

    def _tv_to_dict(self, tv):
        return {
            'id': tv.id,
            'adult': tv.adult,
            'backdrop_path': tv.backdrop_path,
            'created_by': tv.created_by or [],
            'episode_run_time': tv.episode_run_time or [],
            'first_air_date': tv.first_air_date,
            'genres': tv.genres or [],
            'homepage': tv.homepage,
            'in_production': tv.in_production,
            'languages': tv.languages or [],
            'last_air_date': tv.last_air_date,
            'last_episode_to_air': tv.last_episode_to_air,
            'name': tv.name,
            'next_episode_to_air': tv.next_episode_to_air,
            'networks': tv.networks or [],
            'number_of_episodes': tv.number_of_episodes,
            'number_of_seasons': tv.number_of_seasons,
            'origin_country': tv.origin_country or [],
            'original_language': tv.original_language,
            'original_name': tv.original_name,
            'overview': tv.overview,
            'popularity': tv.popularity,
            'poster_path': tv.poster_path,
            'production_companies': tv.production_companies or [],
            'production_countries': tv.production_countries or [],
            'seasons': tv.seasons or [],
            'spoken_languages': tv.spoken_languages or [],
            'status': tv.status,
            'tagline': tv.tagline,
            'type': tv.type,
            'vote_average': tv.vote_average,
            'vote_count': tv.vote_count
        }

    def get_popular_movies(self, page=1, params=None):
        movies = TMDBMovie.objects.all().order_by('-popularity')
        paginator = Paginator(movies, 20)
        page_obj = paginator.page(page)
        return {
            'page': page,
            'results': [self._movie_to_dict(m) for m in page_obj],
            'total_pages': paginator.num_pages,
            'total_results': paginator.count
        }

    def get_top_rated_movies(self, page=1, params=None):
        movies = TMDBMovie.objects.all().order_by('-vote_average', '-popularity')
        paginator = Paginator(movies, 20)
        page_obj = paginator.page(page)
        return {
            'page': page,
            'results': [self._movie_to_dict(m) for m in page_obj],
            'total_pages': paginator.num_pages,
            'total_results': paginator.count
        }

    def get_upcoming_movies(self, page=1, params=None):
        movies = TMDBMovie.objects.all().order_by('-release_date', '-popularity')
        paginator = Paginator(movies, 20)
        page_obj = paginator.page(page)
        return {
            'page': page,
            'results': [self._movie_to_dict(m) for m in page_obj],
            'total_pages': paginator.num_pages,
            'total_results': paginator.count
        }

    def get_now_playing_movies(self, page=1, params=None):
        return self.get_upcoming_movies(page, params)

    def get_popular_series(self, page=1, params=None):
        series = TMDBTV.objects.all().order_by('-popularity')
        paginator = Paginator(series, 20)
        page_obj = paginator.page(page)
        return {
            'page': page,
            'results': [self._tv_to_dict(s) for s in page_obj],
            'total_pages': paginator.num_pages,
            'total_results': paginator.count
        }

    def get_top_rated_series(self, page=1, params=None):
        series = TMDBTV.objects.all().order_by('-vote_average', '-popularity')
        paginator = Paginator(series, 20)
        page_obj = paginator.page(page)
        return {
            'page': page,
            'results': [self._tv_to_dict(s) for s in page_obj],
            'total_pages': paginator.num_pages,
            'total_results': paginator.count
        }

    def get_on_the_air_series(self, page=1, params=None):
        return self.get_popular_series(page, params)

    def get_airing_today_series(self, page=1, params=None):
        return self.get_popular_series(page, params)

    def get_similar_movies(self, movie_id, page=1, params=None):
        # For local DB, just return some popular movies as similar
        movies = TMDBMovie.objects.exclude(id=movie_id).order_by('-popularity')[:20]
        return {
            'page': page,
            'results': [self._movie_to_dict(m) for m in movies],
            'total_pages': 1,
            'total_results': len(movies)
        }

    def get_similar_series(self, series_id, page=1, params=None):
        # For local DB, just return some popular series as similar
        series = TMDBTV.objects.exclude(id=series_id).order_by('-popularity')[:20]
        return {
            'page': page,
            'results': [self._tv_to_dict(s) for s in series],
            'total_pages': 1,
            'total_results': len(series)
        }

    def get_movie_genres(self):
        genres = TMDBGenre.objects.filter(media_type='movie')
        return {'genres': [{'id': g.id, 'name': g.name} for g in genres]}

    def get_series_genres(self):
        genres = TMDBGenre.objects.filter(media_type='tv')
        return {'genres': [{'id': g.id, 'name': g.name} for g in genres]}

    def discover_movies(self, params=None):
        if params is None:
            params = {}
        query = TMDBMovie.objects.all()
        if 'with_genres' in params:
            genre_ids = [int(g) for g in params['with_genres'].split(',')]
            query = query.filter(genres__id__in=genre_ids)
        query = query.order_by('-popularity')
        page = params.get('page', 1)
        paginator = Paginator(query, 20)
        page_obj = paginator.page(page)
        return {
            'page': page,
            'results': [self._movie_to_dict(m) for m in page_obj],
            'total_pages': paginator.num_pages,
            'total_results': paginator.count
        }

    def discover_series(self, params=None):
        if params is None:
            params = {}
        query = TMDBTV.objects.all()
        if 'with_genres' in params:
            genre_ids = [int(g) for g in params['with_genres'].split(',')]
            query = query.filter(genres__id__in=genre_ids)
        query = query.order_by('-popularity')
        page = params.get('page', 1)
        paginator = Paginator(query, 20)
        page_obj = paginator.page(page)
        return {
            'page': page,
            'results': [self._tv_to_dict(s) for s in page_obj],
            'total_pages': paginator.num_pages,
            'total_results': paginator.count
        }

    def get_movie_details(self, movie_id):
        movie = TMDBMovie.objects.get(id=movie_id)
        return self._movie_to_dict(movie)

    def get_series_details(self, series_id):
        series = TMDBTV.objects.get(id=series_id)
        return self._tv_to_dict(series)

    def get_season_details(self, series_id, season_number):
        series = TMDBTV.objects.get(id=series_id)
        # Return season data from our stored seasons JSON if available
        if series.seasons:
            for season in series.seasons:
                if season.get('season_number') == season_number:
                    return season
        # Fallback to minimal structure if not available
        return {
            'id': series_id * 1000 + season_number,
            'season_number': season_number,
            'name': f'Season {season_number}',
            'overview': '',
            'poster_path': series.poster_path,
            'air_date': None,
            'episodes': []
        }

    def search_movies(self, query, page=1):
        movies = TMDBMovie.objects.filter(title__icontains=query) | TMDBMovie.objects.filter(original_title__icontains=query)
        movies = movies.distinct().order_by('-popularity')
        paginator = Paginator(movies, 20)
        page_obj = paginator.page(page)
        return {
            'page': page,
            'results': [self._movie_to_dict(m) for m in page_obj],
            'total_pages': paginator.num_pages,
            'total_results': paginator.count
        }

    def search_series(self, query, page=1):
        series = TMDBTV.objects.filter(name__icontains=query) | TMDBTV.objects.filter(original_name__icontains=query)
        series = series.distinct().order_by('-popularity')
        paginator = Paginator(series, 20)
        page_obj = paginator.page(page)
        return {
            'page': page,
            'results': [self._tv_to_dict(s) for s in page_obj],
            'total_pages': paginator.num_pages,
            'total_results': paginator.count
        }

    def get_movie_watch_providers(self, movie_id):
        # Fallback to empty providers for local DB
        return {'results': {}}

    def get_series_watch_providers(self, series_id):
        # Fallback to empty providers for local DB
        return {'results': {}}
