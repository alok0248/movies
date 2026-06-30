import requests
import sys
import json
from django.conf import settings
from django.core.paginator import Paginator
from django.db import connections
from django.utils import timezone
from core.models import TMDBMovie, TMDBTV, TMDBGenre, SiteSettings, TMDBApiKey, DataSourceUsageLog
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def get_tmdb_db_connection():
    """Create a direct connection to the extracted TMDB PostgreSQL database using SiteSettings"""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    settings_obj = SiteSettings.get_settings()
    return psycopg2.connect(
        host=settings_obj.tmdb_db_host or 'localhost',
        port=settings_obj.tmdb_db_port or 5432,
        dbname=settings_obj.tmdb_db_name or 'tmdb',
        user=settings_obj.tmdb_db_user or 'tmdb',
        password=settings_obj.tmdb_db_password or 'tmdb123!',
        cursor_factory=RealDictCursor
    )


def _json_or_default(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _date_to_iso(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def _track_data_source_usage(source, entity_type, entity_id=None, detail=None):
    log, _ = DataSourceUsageLog.objects.get_or_create(
        source=source,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
        defaults={'usage_count': 0}
    )
    log.usage_count += 1
    log.save(update_fields=['usage_count', 'last_used_at'])


def get_data_client():
    """Get the appropriate data client based on site settings"""
    settings_obj = SiteSettings.get_settings()
    try:
        if settings_obj.data_source == 'local':
            return LocalDBClient()
        elif settings_obj.data_source == 'tmdb_db':
            if getattr(settings_obj, 'tmdb_db_enabled', True):
                return TMDBDBClient()
            return TMDBClient() if settings_obj.tmdb_db_enable_api_fallback else LocalDBClient()
        return TMDBClient()
    except Exception as e:
        print(f"Error getting data client: {e}")
        return LocalDBClient()


class TMDBDBClient:
    """Client for accessing the extracted TMDB PostgreSQL database"""
    def __init__(self):
        self.image_base_url = settings.TMDB_IMAGE_BASE_URL
        self.site_settings = SiteSettings.get_settings()
        self.api_fallback_enabled = bool(self.site_settings.tmdb_db_enable_api_fallback)
        self.movie_table = 'movies'
        self.tv_table = 'tv_shows'
        self.genre_table = 'genres'
        self.season_table = 'tmdb_tv_seasons'
        self.episode_table = 'tmdb_tv_episodes'

    def _json_db_value(self, value):
        return json.dumps(value) if value is not None else None

    def _store_series_seasons_from_details(self, series_id, series_payload):
        seasons = _json_or_default(series_payload.get('seasons'), [])
        for season in seasons:
            if not isinstance(season, dict):
                continue
            season_number = season.get('season_number')
            if season_number in (None, 0):
                continue
            existing_details = self.get_season_details(series_id, season_number)
            if existing_details.get('episodes'):
                continue
            season_payload = TMDBClient().get_season_details(series_id, season_number)
            if season_payload and isinstance(season_payload, dict) and season_payload.get('id'):
                self._store_season_payload(series_id, season_number, season_payload)

    def _track_db_usage(self, entity_type, entity_id=None, detail=None):
        _track_data_source_usage('db', entity_type, entity_id=entity_id, detail=detail)

    def _track_api_fallback_usage(self, entity_type, entity_id=None, detail=None):
        _track_data_source_usage('api_fallback', entity_type, entity_id=entity_id, detail=detail)

    def _store_season_payload(self, series_id, season_number, season_payload):
        episodes = season_payload.get('episodes', []) or []
        conn = get_tmdb_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self.episode_table} WHERE tv_id = %s AND season_number = %s", (series_id, season_number))
                cur.execute(f"DELETE FROM {self.season_table} WHERE tv_id = %s AND season_number = %s", (series_id, season_number))

                cur.execute(
                    f"""
                    INSERT INTO {self.season_table}
                    (tv_id, season_number, id, air_date, name, overview, poster_path, season_number_actual, vote_average, external_ids, images, videos, credits, last_fetched)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        series_id,
                        season_number,
                        season_payload.get('id'),
                        season_payload.get('air_date'),
                        season_payload.get('name'),
                        season_payload.get('overview'),
                        season_payload.get('poster_path'),
                        season_payload.get('season_number', season_number),
                        season_payload.get('vote_average'),
                        self._json_db_value(season_payload.get('external_ids')),
                        self._json_db_value(season_payload.get('images')),
                        self._json_db_value(season_payload.get('videos')),
                        self._json_db_value(season_payload.get('credits')),
                    )
                )

                for episode in episodes:
                    cur.execute(
                        f"""
                        INSERT INTO {self.episode_table}
                        (tv_id, season_number, episode_number, id, air_date, name, overview, production_code, runtime, season_number_actual, show_id, still_path, vote_average, vote_count, crew, guest_stars, external_ids, images, videos, last_fetched)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        """,
                        (
                            series_id,
                            season_number,
                            episode.get('episode_number'),
                            episode.get('id'),
                            episode.get('air_date'),
                            episode.get('name'),
                            episode.get('overview'),
                            episode.get('production_code'),
                            episode.get('runtime'),
                            episode.get('season_number', season_number),
                            series_id,
                            episode.get('still_path'),
                            episode.get('vote_average'),
                            episode.get('vote_count'),
                            self._json_db_value(episode.get('crew')),
                            self._json_db_value(episode.get('guest_stars')),
                            self._json_db_value(episode.get('external_ids')),
                            self._json_db_value(episode.get('images')),
                            self._json_db_value(episode.get('videos')),
                        )
                    )
                conn.commit()
        finally:
            conn.close()

    def _movie_to_dict(self, movie):
        return {
            'id': movie['id'],
            'adult': movie['adult'],
            'backdrop_path': movie['backdrop_path'],
            'belongs_to_collection': movie['belongs_to_collection'],
            'budget': movie['budget'],
            'genres': _json_or_default(movie['genres'], []),
            'homepage': movie['homepage'],
            'imdb_id': movie['imdb_id'],
            'original_language': movie['original_language'],
            'original_title': movie['original_title'],
            'overview': movie['overview'],
            'popularity': movie['popularity'],
            'poster_path': movie['poster_path'],
            'production_companies': _json_or_default(movie['production_companies'], []),
            'production_countries': _json_or_default(movie['production_countries'], []),
            'release_date': _date_to_iso(movie['release_date']),
            'revenue': movie['revenue'],
            'runtime': movie['runtime'],
            'spoken_languages': _json_or_default(movie['spoken_languages'], []),
            'status': movie['status'],
            'tagline': movie['tagline'],
            'title': movie['title'],
            'video': movie['video'],
            'vote_average': movie['vote_average'],
            'vote_count': movie['vote_count'],
            'credits': _json_or_default(movie.get('credits'), {}),
            'videos': _json_or_default(movie.get('videos'), {'results': []}),
            'keywords': _json_or_default(movie.get('keywords'), {'keywords': [], 'results': []}),
            'external_ids': _json_or_default(movie.get('external_ids'), {}),
            'recommendations': _json_or_default(movie.get('recommendations'), {'results': []}),
            'similar': _json_or_default(movie.get('similar'), {'results': []}),
            'reviews': _json_or_default(movie.get('reviews'), {'results': []}),
            'images': _json_or_default(movie.get('images'), {'backdrops': [], 'posters': []}),
            'lists': _json_or_default(movie.get('lists'), {'results': []}),
            'translations': _json_or_default(movie.get('translations'), {'translations': []}),
            'watch_providers': _json_or_default(movie.get('watch_providers'), {'results': {}}),
            'release_dates': _json_or_default(movie.get('release_dates'), {'results': []}),
        }

    def _tv_to_dict(self, tv):
        return {
            'id': tv['id'],
            'adult': tv['adult'],
            'backdrop_path': tv['backdrop_path'],
            'created_by': _json_or_default(tv['created_by'], []),
            'episode_run_time': _json_or_default(tv['episode_run_time'], []),
            'first_air_date': _date_to_iso(tv['first_air_date']),
            'genres': _json_or_default(tv['genres'], []),
            'homepage': tv['homepage'],
            'in_production': tv['in_production'],
            'languages': _json_or_default(tv['languages'], []),
            'last_air_date': _date_to_iso(tv['last_air_date']),
            'last_episode_to_air': _json_or_default(tv['last_episode_to_air'], {}),
            'name': tv['name'],
            'next_episode_to_air': _json_or_default(tv['next_episode_to_air'], {}),
            'networks': _json_or_default(tv['networks'], []),
            'number_of_episodes': tv['number_of_episodes'],
            'number_of_seasons': tv['number_of_seasons'],
            'origin_country': _json_or_default(tv['origin_country'], []),
            'original_language': tv['original_language'],
            'original_name': tv['original_name'],
            'overview': tv['overview'],
            'popularity': tv['popularity'],
            'poster_path': tv['poster_path'],
            'production_companies': _json_or_default(tv['production_companies'], []),
            'production_countries': _json_or_default(tv['production_countries'], []),
            'seasons': _json_or_default(tv['seasons'], []),
            'spoken_languages': _json_or_default(tv['spoken_languages'], []),
            'status': tv['status'],
            'tagline': tv['tagline'],
            'type': tv['type'],
            'vote_average': tv['vote_average'],
            'vote_count': tv['vote_count'],
            'credits': _json_or_default(tv.get('credits'), {}),
            'videos': _json_or_default(tv.get('videos'), {'results': []}),
            'keywords': _json_or_default(tv.get('keywords'), {'results': []}),
            'external_ids': _json_or_default(tv.get('external_ids'), {}),
            'recommendations': _json_or_default(tv.get('recommendations'), {'results': []}),
            'similar': _json_or_default(tv.get('similar'), {'results': []}),
            'reviews': _json_or_default(tv.get('reviews'), {'results': []}),
            'images': _json_or_default(tv.get('images'), {'backdrops': [], 'posters': []}),
            'translations': _json_or_default(tv.get('translations'), {'translations': []}),
            'watch_providers': _json_or_default(tv.get('watch_providers'), {'results': {}}),
            'content_ratings': _json_or_default(tv.get('content_ratings'), {'results': []}),
            'aggregate_credits': _json_or_default(tv.get('aggregate_credits'), {}),
        }

    def get_popular_movies(self, page=1, params=None):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {self.movie_table}")
                    total_results = cur.fetchone()['count']
                    total_pages = (total_results + 19) // 20
                    offset = (page - 1) * 20
                    cur.execute(f"SELECT * FROM {self.movie_table} ORDER BY popularity DESC LIMIT 20 OFFSET %s", (offset,))
                    movies = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in movies],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_popular_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_top_rated_movies(self, page=1, params=None):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {self.movie_table}")
                    total_results = cur.fetchone()['count']
                    total_pages = (total_results + 19) // 20
                    offset = (page - 1) * 20
                    cur.execute(f"SELECT * FROM {self.movie_table} ORDER BY vote_average DESC, popularity DESC LIMIT 20 OFFSET %s", (offset,))
                    movies = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in movies],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_top_rated_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_upcoming_movies(self, page=1, params=None):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {self.movie_table}")
                    total_results = cur.fetchone()['count']
                    total_pages = (total_results + 19) // 20
                    offset = (page - 1) * 20
                    cur.execute(f"SELECT * FROM {self.movie_table} ORDER BY release_date DESC, popularity DESC LIMIT 20 OFFSET %s", (offset,))
                    movies = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in movies],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_upcoming_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_now_playing_movies(self, page=1, params=None):
        return self.get_upcoming_movies(page, params)

    def get_popular_series(self, page=1, params=None):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {self.tv_table}")
                    total_results = cur.fetchone()['count']
                    total_pages = (total_results + 19) // 20
                    offset = (page - 1) * 20
                    cur.execute(f"SELECT * FROM {self.tv_table} ORDER BY popularity DESC LIMIT 20 OFFSET %s", (offset,))
                    series = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._tv_to_dict(s) for s in series],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_popular_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_top_rated_series(self, page=1, params=None):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {self.tv_table}")
                    total_results = cur.fetchone()['count']
                    total_pages = (total_results + 19) // 20
                    offset = (page - 1) * 20
                    cur.execute(f"SELECT * FROM {self.tv_table} ORDER BY vote_average DESC, popularity DESC LIMIT 20 OFFSET %s", (offset,))
                    series = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._tv_to_dict(s) for s in series],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_top_rated_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_on_the_air_series(self, page=1, params=None):
        return self.get_popular_series(page, params)

    def get_airing_today_series(self, page=1, params=None):
        return self.get_popular_series(page, params)

    def get_similar_movies(self, movie_id, page=1, params=None):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {self.movie_table} WHERE id != %s ORDER BY popularity DESC LIMIT 20", (movie_id,))
                    movies = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in movies],
                'total_pages': 1,
                'total_results': len(movies)
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_similar_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_similar_series(self, series_id, page=1, params=None):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {self.tv_table} WHERE id != %s ORDER BY popularity DESC LIMIT 20", (series_id,))
                    series = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._tv_to_dict(s) for s in series],
                'total_pages': 1,
                'total_results': len(series)
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_similar_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_movie_genres(self):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT id, name FROM {self.genre_table} WHERE media_type = 'movie'")
                    genres = cur.fetchall()
            finally:
                conn.close()
            return {'genres': [{'id': g['id'], 'name': g['name']} for g in genres]}
        except Exception as e:
            print(f"Error in TMDBDBClient.get_movie_genres: {e}")
            return {'genres': []}

    def get_series_genres(self):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT id, name FROM {self.genre_table} WHERE media_type = 'tv'")
                    genres = cur.fetchall()
            finally:
                conn.close()
            return {'genres': [{'id': g['id'], 'name': g['name']} for g in genres]}
        except Exception as e:
            print(f"Error in TMDBDBClient.get_series_genres: {e}")
            return {'genres': []}

    def discover_movies(self, params=None):
        if params is None:
            params = {}
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    base_query = f"SELECT * FROM {self.movie_table}"
                    count_query = f"SELECT COUNT(*) FROM {self.movie_table}"
                    conditions = []
                    query_params = []
                    
                    if 'primary_release_date.gte' in params:
                        conditions.append("release_date >= %s")
                        query_params.append(params['primary_release_date.gte'])
                    if 'primary_release_date.lte' in params:
                        conditions.append("release_date <= %s")
                        query_params.append(params['primary_release_date.lte'])
                    if 'with_genres' in params:
                        genre_ids = [int(g) for g in str(params['with_genres']).split(',') if str(g).strip().isdigit()]
                        if genre_ids:
                            conditions.append("genres::text ~ %s")
                            query_params.append('(' + '|'.join([f'\\"id\\": {gid}' for gid in genre_ids]) + ')')
                    
                    if 'sort_by' in params and 'primary_release_date.desc' in params['sort_by']:
                        order_by = "ORDER BY release_date DESC, popularity DESC"
                    else:
                        order_by = "ORDER BY popularity DESC"
                    
                    page = int(params.get('page', 1))
                    offset = (page - 1) * 20
                    
                    if conditions:
                        where_clause = " WHERE " + " AND ".join(conditions)
                    else:
                        where_clause = ""
                    
                    cur.execute(count_query + where_clause, query_params)
                    total_results = cur.fetchone()['count']
                    total_pages = (total_results + 19) // 20
                    
                    cur.execute(base_query + where_clause + " " + order_by + " LIMIT 20 OFFSET %s", query_params + [offset])
                    movies = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in movies],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.discover_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def discover_series(self, params=None):
        if params is None:
            params = {}
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    base_query = f"SELECT * FROM {self.tv_table}"
                    count_query = f"SELECT COUNT(*) FROM {self.tv_table}"
                    conditions = []
                    query_params = []
                    
                    if 'air_date.gte' in params:
                        conditions.append("first_air_date >= %s")
                        query_params.append(params['air_date.gte'])
                    if 'air_date.lte' in params:
                        conditions.append("first_air_date <= %s")
                        query_params.append(params['air_date.lte'])
                    if 'with_genres' in params:
                        genre_ids = [int(g) for g in str(params['with_genres']).split(',') if str(g).strip().isdigit()]
                        if genre_ids:
                            conditions.append("genres::text ~ %s")
                            query_params.append('(' + '|'.join([f'\\"id\\": {gid}' for gid in genre_ids]) + ')')
                        order_by = "ORDER BY first_air_date DESC, popularity DESC"
                    else:
                        order_by = "ORDER BY popularity DESC"
                    
                    page = int(params.get('page', 1))
                    offset = (page - 1) * 20
                    
                    if conditions:
                        where_clause = " WHERE " + " AND ".join(conditions)
                    else:
                        where_clause = ""
                    
                    cur.execute(count_query + where_clause, query_params)
                    total_results = cur.fetchone()['count']
                    total_pages = (total_results + 19) // 20
                    
                    cur.execute(base_query + where_clause + " " + order_by + " LIMIT 20 OFFSET %s", query_params + [offset])
                    series = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._tv_to_dict(s) for s in series],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.discover_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_movie_details(self, movie_id):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {self.movie_table} WHERE id = %s", (movie_id,))
                    movie = cur.fetchone()
            finally:
                conn.close()
            return self._movie_to_dict(movie)
        except Exception as e:
            print(f"Error in TMDBDBClient.get_movie_details: {e}")
            return None

    def get_series_details(self, series_id):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {self.tv_table} WHERE id = %s", (series_id,))
                    series = cur.fetchone()
            finally:
                conn.close()

            if not series:
                return None

            series_dict = self._tv_to_dict(series)
            seasons = _json_or_default(series_dict.get('seasons'), [])
            series_dict['seasons'] = seasons if isinstance(seasons, list) else []
            if isinstance(series_dict['seasons'], list):
                missing_seasons = []
                for season in series_dict['seasons']:
                    if not isinstance(season, dict):
                        continue
                    season_number = season.get('season_number')
                    if season_number in (None, 0):
                        continue
                    conn = get_tmdb_db_connection()
                    try:
                        with conn.cursor() as cur:
                            cur.execute(f"SELECT COUNT(*) FROM {self.season_table} WHERE tv_id = %s AND season_number = %s", (series_id, season_number))
                            season_count = cur.fetchone()['count']
                            cur.execute(f"SELECT COUNT(*) FROM {self.episode_table} WHERE tv_id = %s AND season_number = %s", (series_id, season_number))
                            episode_count = cur.fetchone()['count']
                    finally:
                        conn.close()
                    if season_count == 0 or episode_count == 0:
                        missing_seasons.append(season_number)

                if missing_seasons:
                    api_client = TMDBClient()
                    api_series = api_client.get_series_details(series_id)
                    if api_series and isinstance(api_series, dict):
                        self._track_api_fallback_usage('series_seasons', entity_id=series_id, detail=','.join(str(item) for item in missing_seasons))
                        self._store_series_seasons_from_details(series_id, api_series)
                        refreshed = self.get_series_details(series_id)
                        if refreshed:
                            return refreshed

            self._track_db_usage('series', entity_id=series_id, detail='details')
            return series_dict
        except Exception as e:
            print(f"Error in TMDBDBClient.get_series_details: {e}")
            return None

    def get_season_details(self, series_id, season_number):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {self.season_table} WHERE tv_id = %s AND season_number = %s", (series_id, season_number))
                    season = cur.fetchone()
                    cur.execute(f"SELECT * FROM {self.episode_table} WHERE tv_id = %s AND season_number = %s ORDER BY episode_number", (series_id, season_number))
                    episodes = cur.fetchall()
            finally:
                conn.close()

            if season and episodes:
                self._track_db_usage('season', entity_id=series_id, detail=f'season:{season_number}')
                season_dict = dict(season)
                season_dict['air_date'] = _date_to_iso(season_dict.get('air_date'))
                season_dict['overview'] = season_dict.get('overview') or ''
                season_dict['poster_path'] = season_dict.get('poster_path')
                season_dict['name'] = season_dict.get('name') or f"Season {season_number}"
                season_dict['season_number'] = season_dict.get('season_number', season_number)
                season_dict['vote_average'] = season_dict.get('vote_average') or 0
                season_dict['external_ids'] = _json_or_default(season_dict.get('external_ids'), {})
                season_dict['images'] = _json_or_default(season_dict.get('images'), {'posters': []})
                season_dict['videos'] = _json_or_default(season_dict.get('videos'), {'results': []})
                season_dict['credits'] = _json_or_default(season_dict.get('credits'), {'cast': [], 'crew': []})
                season_dict['episodes'] = []
                for episode in episodes:
                    episode_dict = dict(episode)
                    episode_dict['air_date'] = _date_to_iso(episode_dict.get('air_date'))
                    episode_dict['overview'] = episode_dict.get('overview') or ''
                    episode_dict['still_path'] = episode_dict.get('still_path')
                    episode_dict['name'] = episode_dict.get('name') or f"Episode {episode_dict.get('episode_number', '')}"
                    episode_dict['vote_average'] = episode_dict.get('vote_average') or 0
                    episode_dict['vote_count'] = episode_dict.get('vote_count') or 0
                    episode_dict['runtime'] = episode_dict.get('runtime')
                    episode_dict['season_number'] = episode_dict.get('season_number', season_number)
                    episode_dict['crew'] = _json_or_default(episode_dict.get('crew'), [])
                    episode_dict['guest_stars'] = _json_or_default(episode_dict.get('guest_stars'), [])
                    episode_dict['external_ids'] = _json_or_default(episode_dict.get('external_ids'), {})
                    episode_dict['images'] = _json_or_default(episode_dict.get('images'), {'stills': []})
                    episode_dict['videos'] = _json_or_default(episode_dict.get('videos'), {'results': []})
                    season_dict['episodes'].append(episode_dict)
                season_dict['episode_count'] = len(season_dict['episodes'])
                return season_dict

            api_client = TMDBClient()
            season_payload = api_client.get_season_details(series_id, season_number)
            if season_payload and isinstance(season_payload, dict) and season_payload.get('id') and season_payload.get('episodes'):
                self._track_api_fallback_usage('season', entity_id=series_id, detail=f'season:{season_number}')
                self._store_season_payload(series_id, season_number, season_payload)
                return self.get_season_details(series_id, season_number)
        except Exception as e:
            print(f"Error in TMDBDBClient.get_season_details: {e}")

        return {
            'id': series_id * 1000 + season_number,
            'season_number': season_number,
            'name': f'Season {season_number}',
            'overview': '',
            'poster_path': None,
            'air_date': None,
            'episodes': []
        }

    def search_movies(self, query, page=1):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    search_pattern = f"%{query}%"
                    cur.execute(f"SELECT COUNT(*) FROM {self.movie_table} WHERE title ILIKE %s OR original_title ILIKE %s", (search_pattern, search_pattern))
                    total_results = cur.fetchone()['count']
                    total_pages = (total_results + 19) // 20
                    offset = (page - 1) * 20
                    cur.execute(f"SELECT * FROM {self.movie_table} WHERE title ILIKE %s OR original_title ILIKE %s ORDER BY popularity DESC LIMIT 20 OFFSET %s", (search_pattern, search_pattern, offset))
                    movies = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in movies],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.search_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def search_series(self, query, page=1):
        try:
            conn = get_tmdb_db_connection()
            try:
                with conn.cursor() as cur:
                    search_pattern = f"%{query}%"
                    cur.execute(f"SELECT COUNT(*) FROM {self.tv_table} WHERE name ILIKE %s OR original_name ILIKE %s", (search_pattern, search_pattern))
                    total_results = cur.fetchone()['count']
                    total_pages = (total_results + 19) // 20
                    offset = (page - 1) * 20
                    cur.execute(f"SELECT * FROM {self.tv_table} WHERE name ILIKE %s OR original_name ILIKE %s ORDER BY popularity DESC LIMIT 20 OFFSET %s", (search_pattern, search_pattern, offset))
                    series = cur.fetchall()
            finally:
                conn.close()
            return {
                'page': page,
                'results': [self._tv_to_dict(s) for s in series],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.search_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_movie_watch_providers(self, movie_id):
        movie = self.get_movie_details(movie_id)
        if movie:
            return movie.get('watch_providers', {'results': {}})
        return {'results': {}}

    def get_series_watch_providers(self, series_id):
        series = self.get_series_details(series_id)
        if series:
            return series.get('watch_providers', {'results': {}})
        return {'results': {}}

def _month_bounds(year, month):
    import datetime
    first_day = datetime.date(year, month, 1)
    if month == 12:
        last_day = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return first_day, last_day


class TMDBClient:
    def __init__(self):
        self.base_url = settings.TMDB_BASE_URL
        self.image_base_url = settings.TMDB_IMAGE_BASE_URL
        
        # Create a session with retries
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Get available API keys from database
        self._load_api_keys()

    def _load_api_keys(self):
        """Load active API keys from the database"""
        self.api_keys = list(TMDBApiKey.objects.filter(is_active=True).order_by('last_used_at'))
        self.current_key_index = 0

    def _get_next_api_key(self):
        """Get the next API key in rotation"""
        if not self.api_keys:
            return None
        
        # Get the current key
        key = self.api_keys[self.current_key_index]
        key.usage_count += 1
        
        # Update last used time
        key.last_used_at = timezone.now()
        key.save(update_fields=['usage_count', 'last_used_at'])
        
        # Move to next key (round robin)
        self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
        
        return key.key

    def _make_request(self, endpoint, params=None):
        if params is None:
            params = {}

        keys_to_try = max(len(self.api_keys), 1)

        for _ in range(keys_to_try):
            api_key = self._get_next_api_key()
            if not api_key:
                break

            request_params = params.copy()
            request_params['api_key'] = api_key
            url = f"{self.base_url}{endpoint}"

            try:
                response = self.session.get(url, params=request_params, timeout=10)
                if response.status_code == 429:
                    print(f"TMDB rate limit hit for key on {endpoint}, rotating key")
                    continue
                response.raise_for_status()
                _track_data_source_usage('api', 'request', detail=endpoint)
                return response.json()
            except requests.exceptions.RequestException as e:
                print(f"Error making TMDB API request to {endpoint} with API key: {e}")
                continue

        print("All TMDB API keys failed")
        return {'page': 1, 'results': [], 'total_pages': 0, 'total_results': 0}

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
        try:
            movies = TMDBMovie.objects.all().order_by('-popularity')
            paginator = Paginator(movies, 20)
            page_obj = paginator.page(page)
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in page_obj],
                'total_pages': paginator.num_pages,
                'total_results': paginator.count
            }
        except Exception as e:
            print(f"Error in LocalDBClient.get_popular_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_top_rated_movies(self, page=1, params=None):
        try:
            movies = TMDBMovie.objects.all().order_by('-vote_average', '-popularity')
            paginator = Paginator(movies, 20)
            page_obj = paginator.page(page)
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in page_obj],
                'total_pages': paginator.num_pages,
                'total_results': paginator.count
            }
        except Exception as e:
            print(f"Error in LocalDBClient.get_top_rated_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_upcoming_movies(self, page=1, params=None):
        try:
            movies = TMDBMovie.objects.all().order_by('-release_date', '-popularity')
            paginator = Paginator(movies, 20)
            page_obj = paginator.page(page)
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in page_obj],
                'total_pages': paginator.num_pages,
                'total_results': paginator.count
            }
        except Exception as e:
            print(f"Error in LocalDBClient.get_upcoming_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_now_playing_movies(self, page=1, params=None):
        return self.get_upcoming_movies(page, params)

    def get_popular_series(self, page=1, params=None):
        try:
            series = TMDBTV.objects.all().order_by('-popularity')
            paginator = Paginator(series, 20)
            page_obj = paginator.page(page)
            return {
                'page': page,
                'results': [self._tv_to_dict(s) for s in page_obj],
                'total_pages': paginator.num_pages,
                'total_results': paginator.count
            }
        except Exception as e:
            print(f"Error in LocalDBClient.get_popular_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_top_rated_series(self, page=1, params=None):
        try:
            series = TMDBTV.objects.all().order_by('-vote_average', '-popularity')
            paginator = Paginator(series, 20)
            page_obj = paginator.page(page)
            return {
                'page': page,
                'results': [self._tv_to_dict(s) for s in page_obj],
                'total_pages': paginator.num_pages,
                'total_results': paginator.count
            }
        except Exception as e:
            print(f"Error in LocalDBClient.get_top_rated_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_on_the_air_series(self, page=1, params=None):
        return self.get_popular_series(page, params)

    def get_airing_today_series(self, page=1, params=None):
        return self.get_popular_series(page, params)

    def get_similar_movies(self, movie_id, page=1, params=None):
        try:
            # For local DB, just return some popular movies as similar
            movies = TMDBMovie.objects.exclude(id=movie_id).order_by('-popularity')[:20]
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in movies],
                'total_pages': 1,
                'total_results': len(movies)
            }
        except Exception as e:
            print(f"Error in LocalDBClient.get_similar_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_similar_series(self, series_id, page=1, params=None):
        try:
            # For local DB, just return some popular series as similar
            series = TMDBTV.objects.exclude(id=series_id).order_by('-popularity')[:20]
            return {
                'page': page,
                'results': [self._tv_to_dict(s) for s in series],
                'total_pages': 1,
                'total_results': len(series)
            }
        except Exception as e:
            print(f"Error in LocalDBClient.get_similar_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_movie_genres(self):
        try:
            genres = TMDBGenre.objects.filter(media_type='movie')
            return {'genres': [{'id': g.id, 'name': g.name} for g in genres]}
        except Exception as e:
            print(f"Error in LocalDBClient.get_movie_genres: {e}")
            return {'genres': []}

    def get_series_genres(self):
        try:
            genres = TMDBGenre.objects.filter(media_type='tv')
            return {'genres': [{'id': g.id, 'name': g.name} for g in genres]}
        except Exception as e:
            print(f"Error in LocalDBClient.get_series_genres: {e}")
            return {'genres': []}

    def discover_movies(self, params=None):
        try:
            if params is None:
                params = {}
            query = TMDBMovie.objects.all()
            if 'with_genres' in params:
                genre_ids = [int(g) for g in params['with_genres'].split(',')]
                query = query.filter(genres__id__in=genre_ids)
            if 'primary_release_date.gte' in params:
                query = query.filter(release_date__gte=params['primary_release_date.gte'])
            if 'primary_release_date.lte' in params:
                query = query.filter(release_date__lte=params['primary_release_date.lte'])
            if 'sort_by' in params and 'primary_release_date.desc' in params['sort_by']:
                query = query.order_by('-release_date', '-popularity')
            else:
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
        except Exception as e:
            print(f"Error in LocalDBClient.discover_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def discover_series(self, params=None):
        try:
            if params is None:
                params = {}
            query = TMDBTV.objects.all()
            if 'with_genres' in params:
                genre_ids = [int(g) for g in params['with_genres'].split(',')]
                query = query.filter(genres__id__in=genre_ids)
            if 'air_date.gte' in params:
                query = query.filter(first_air_date__gte=params['air_date.gte'])
            if 'air_date.lte' in params:
                query = query.filter(first_air_date__lte=params['air_date.lte'])
            if 'sort_by' in params and 'first_air_date.desc' in params['sort_by']:
                query = query.order_by('-first_air_date', '-popularity')
            else:
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
        except Exception as e:
            print(f"Error in LocalDBClient.discover_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_movie_details(self, movie_id):
        try:
            movie = TMDBMovie.objects.get(id=movie_id)
            return self._movie_to_dict(movie)
        except Exception as e:
            print(f"Error in LocalDBClient.get_movie_details: {e}")
            return None

    def get_series_details(self, series_id):
        try:
            series = TMDBTV.objects.get(id=series_id)
            return self._tv_to_dict(series)
        except Exception as e:
            print(f"Error in LocalDBClient.get_series_details: {e}")
            return None

    def get_season_details(self, series_id, season_number):
        try:
            series = TMDBTV.objects.get(id=series_id)
            # Return season data from our stored seasons JSON if available
            if series.seasons:
                for season in series.seasons:
                    if season.get('season_number') == season_number:
                        return season
        except Exception as e:
            print(f"Error in LocalDBClient.get_season_details: {e}")
        # Fallback to minimal structure if not available
        return {
            'id': series_id * 1000 + season_number,
            'season_number': season_number,
            'name': f'Season {season_number}',
            'overview': '',
            'poster_path': None,
            'air_date': None,
            'episodes': []
        }

    def search_movies(self, query, page=1):
        try:
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
        except Exception as e:
            print(f"Error in LocalDBClient.search_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def search_series(self, query, page=1):
        try:
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
        except Exception as e:
            print(f"Error in LocalDBClient.search_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_calendar_month_data(self, year, month):
        first_day, last_day = _month_bounds(year, month)
        movie_data = self.discover_movies({
            'primary_release_date.gte': first_day.strftime('%Y-%m-%d'),
            'primary_release_date.lte': last_day.strftime('%Y-%m-%d'),
            'sort_by': 'primary_release_date.desc'
        })
        series_data = self.discover_series({
            'air_date.gte': first_day.strftime('%Y-%m-%d'),
            'air_date.lte': last_day.strftime('%Y-%m-%d'),
            'sort_by': 'first_air_date.desc'
        })
        return {
            'year': year,
            'month': month,
            'month_name': __import__('calendar').month_name[month],
            'first_day': first_day.strftime('%Y-%m-%d'),
            'last_day': last_day.strftime('%Y-%m-%d'),
            'movies': movie_data.get('results', []),
            'series': series_data.get('results', []),
        }

    def get_movie_watch_providers(self, movie_id):
        # Fallback to empty providers for local DB
        return {'results': {}}

    def get_series_watch_providers(self, series_id):
        # Fallback to empty providers for local DB
        return {'results': {}}
