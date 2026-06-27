import requests
from django.conf import settings

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

    def get_popular_series(self, page=1, params=None):
        if params is None:
            params = {}
        params['page'] = page
        endpoint = '/tv/popular'
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
