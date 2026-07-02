import requests
import sys
import json
from contextlib import contextmanager
from threading import local
from django.conf import settings
from django.core.paginator import Paginator
from django.db import connections
from django.utils import timezone
from core.models import TMDBMovie, TMDBTV, TMDBGenre, SiteSettings, TMDBApiKey, DataSourceUsageLog
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


_thread_local = local()


def _get_cached_site_settings():
    settings_obj = getattr(_thread_local, 'site_settings', None)
    if settings_obj is None:
        settings_obj = SiteSettings.get_settings()
        _thread_local.site_settings = settings_obj
    return settings_obj


def _clear_cached_db_connection():
    conn = getattr(_thread_local, 'tmdb_db_conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _thread_local.tmdb_db_conn = None


def get_tmdb_db_connection():
    """Create or reuse a direct connection to the extracted TMDB PostgreSQL database using SiteSettings"""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = getattr(_thread_local, 'tmdb_db_conn', None)
    if conn is not None and getattr(conn, 'closed', 1) == 0:
        return conn

    settings_obj = _get_cached_site_settings()
    conn = psycopg2.connect(
        host=settings_obj.tmdb_db_host or 'localhost',
        port=settings_obj.tmdb_db_port or 5432,
        dbname=settings_obj.tmdb_db_name or 'tmdb',
        user=settings_obj.tmdb_db_user or 'tmdb',
        password=settings_obj.tmdb_db_password or 'tmdb123!',
        connect_timeout=5,
        options='-c statement_timeout=5000',
        cursor_factory=RealDictCursor,
        application_name='movies_tmdb_db_client'
    )
    conn.autocommit = False
    _thread_local.tmdb_db_conn = conn
    return conn


@contextmanager
def tmdb_db_cursor():
    conn = get_tmdb_db_connection()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _json_or_default(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value




def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
        self.movie_genre_table = 'movie_genres'
        self.tv_genre_table = 'tv_genres'
        self.movie_provider_table = 'movie_watch_providers'
        self.tv_provider_table = 'tv_watch_providers'
        self.movie_spoken_language_table = 'movie_spoken_languages'
        self.tv_spoken_language_table = 'tv_spoken_languages'
        self.movie_production_country_table = 'movie_production_countries'
        self.tv_production_country_table = 'tv_production_countries'
        self.tv_network_table = 'tv_networks'
        self.tv_created_by_table = 'tv_created_by'
        self.tv_origin_country_table = 'tv_origin_countries'
        self.tv_language_table = 'tv_languages'
        self.movie_recommendation_table = 'movie_recommendations'
        self.movie_similar_table = 'movie_similar'
        self.tv_recommendation_table = 'tv_recommendations'
        self.tv_similar_table = 'tv_similar'
        self.movie_release_date_table = 'movie_release_dates'
        self.content_rating_table = 'tv_content_ratings'
        self.season_table = 'tv_seasons'
        self.episode_table = 'tv_episodes'

    def _json_db_value(self, value):
        return json.dumps(value) if value is not None else None

    def _fetch_all(self, query, params=None):
        with tmdb_db_cursor() as cur:
            cur.execute(query, params or [])
            return cur.fetchall()

    def _fetch_one(self, query, params=None):
        with tmdb_db_cursor() as cur:
            cur.execute(query, params or [])
            return cur.fetchone()

    def _fetch_many_payloads(self, table, ids):
        ids = [item for item in ids if item is not None]
        if not ids:
            return []
        with tmdb_db_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = ANY(%s)", (ids,))
            rows = cur.fetchall()
        rows_by_id = {row['id']: row for row in rows}
        return [rows_by_id[item_id] for item_id in ids if item_id in rows_by_id]

    def _get_genres(self, media_type, entity_id):
        table = self.movie_genre_table if media_type == 'movie' else self.tv_genre_table
        id_column = 'movie_id' if media_type == 'movie' else 'tv_id'
        rows = self._fetch_all(
            f"""
            SELECT g.id, g.name
            FROM {table} rel
            JOIN {self.genre_table} g ON g.id = rel.genre_id
            WHERE rel.{id_column} = %s
            ORDER BY g.name
            """,
            (entity_id,),
        )
        return [{'id': row['id'], 'name': row['name']} for row in rows]

    def _get_spoken_languages(self, media_type, entity_id):
        table = self.movie_spoken_language_table if media_type == 'movie' else self.tv_spoken_language_table
        id_column = 'movie_id' if media_type == 'movie' else 'tv_id'
        rows = self._fetch_all(
            f"SELECT iso_639_1, english_name, name FROM {table} WHERE {id_column} = %s ORDER BY iso_639_1",
            (entity_id,),
        )
        return [
            {
                'iso_639_1': row['iso_639_1'],
                'english_name': row.get('english_name'),
                'name': row.get('name'),
            }
            for row in rows
        ]

    def _get_production_countries(self, media_type, entity_id):
        table = self.movie_production_country_table if media_type == 'movie' else self.tv_production_country_table
        id_column = 'movie_id' if media_type == 'movie' else 'tv_id'
        rows = self._fetch_all(
            f"SELECT iso_3166_1, name FROM {table} WHERE {id_column} = %s ORDER BY iso_3166_1",
            (entity_id,),
        )
        return [{'iso_3166_1': row['iso_3166_1'], 'name': row.get('name')} for row in rows]

    def _get_watch_providers(self, media_type, entity_id):
        table = self.movie_provider_table if media_type == 'movie' else self.tv_provider_table
        id_column = 'movie_id' if media_type == 'movie' else 'tv_id'
        rows = self._fetch_all(
            f"""
            SELECT country_code, provider_type, provider_id, provider_name, logo_path, display_priority
            FROM {table}
            WHERE {id_column} = %s
            ORDER BY country_code, provider_type, display_priority, provider_name
            """,
            (entity_id,),
        )
        results = {}
        for row in rows:
            region = results.setdefault(row['country_code'], {'link': None})
            region.setdefault(row['provider_type'], []).append(
                {
                    'provider_id': row['provider_id'],
                    'provider_name': row.get('provider_name'),
                    'logo_path': row.get('logo_path'),
                    'display_priority': row.get('display_priority'),
                }
            )
        return {'results': results}

    def _get_related_ids(self, table, source_column, target_column, entity_id):
        rows = self._fetch_all(
            f"SELECT {target_column} FROM {table} WHERE {source_column} = %s",
            (entity_id,),
        )
        return [row[target_column] for row in rows]

    def _get_movie_release_dates(self, movie_id):
        rows = self._fetch_all(
            f"SELECT country_code, release_date, type, note, certification, iso_639_1 FROM {self.movie_release_date_table} WHERE movie_id = %s ORDER BY country_code, release_date",
            (movie_id,),
        )
        grouped = {}
        for row in rows:
            item = grouped.setdefault(row['country_code'], {'iso_3166_1': row['country_code'], 'release_dates': []})
            item['release_dates'].append(
                {
                    'certification': row.get('certification') or '',
                    'descriptors': [],
                    'iso_639_1': row.get('iso_639_1'),
                    'note': row.get('note') or '',
                    'release_date': row.get('release_date'),
                    'type': row.get('type'),
                }
            )
        return {'results': list(grouped.values())}

    def _get_tv_content_ratings(self, tv_id):
        rows = self._fetch_all(
            f"SELECT country_code, rating FROM {self.content_rating_table} WHERE tv_id = %s ORDER BY country_code",
            (tv_id,),
        )
        return {'results': [{'iso_3166_1': row['country_code'], 'rating': row.get('rating') or ''} for row in rows]}

    def _get_tv_networks(self, tv_id):
        rows = self._fetch_all(
            f"SELECT network_id, name, logo_path, origin_country FROM {self.tv_network_table} WHERE tv_id = %s ORDER BY name",
            (tv_id,),
        )
        return [
            {
                'id': row['network_id'],
                'name': row.get('name'),
                'logo_path': row.get('logo_path'),
                'origin_country': row.get('origin_country') or '',
            }
            for row in rows
        ]

    def _get_tv_created_by(self, tv_id):
        rows = self._fetch_all(
            f"SELECT person_id, name, gender, profile_path FROM {self.tv_created_by_table} WHERE tv_id = %s ORDER BY name",
            (tv_id,),
        )
        return [
            {
                'id': row['person_id'],
                'credit_id': None,
                'name': row.get('name'),
                'gender': row.get('gender'),
                'profile_path': row.get('profile_path'),
            }
            for row in rows
        ]

    def _get_tv_origin_country(self, tv_id):
        rows = self._fetch_all(
            f"SELECT iso_3166_1 FROM {self.tv_origin_country_table} WHERE tv_id = %s ORDER BY iso_3166_1",
            (tv_id,),
        )
        return [row['iso_3166_1'] for row in rows]

    def _get_tv_languages(self, tv_id):
        rows = self._fetch_all(
            f"SELECT iso_639_1 FROM {self.tv_language_table} WHERE tv_id = %s ORDER BY iso_639_1",
            (tv_id,),
        )
        return [row['iso_639_1'] for row in rows]

    def _get_tv_episode_runtime(self, tv_id):
        row = self._fetch_one(
            f"SELECT runtime FROM {self.episode_table} WHERE tv_id = %s AND runtime IS NOT NULL ORDER BY runtime DESC LIMIT 1",
            (tv_id,),
        )
        return [row['runtime']] if row and row.get('runtime') is not None else []

    def _get_tv_seasons_summary(self, tv_id):
        rows = self._fetch_all(
            f"SELECT tv_id, season_number, id, air_date, name, overview, poster_path, vote_average, episode_count FROM {self.season_table} WHERE tv_id = %s ORDER BY season_number",
            (tv_id,),
        )
        return [
            {
                'id': row.get('id'),
                'air_date': row.get('air_date'),
                'episode_count': row.get('episode_count') or 0,
                'name': row.get('name') or f"Season {row['season_number']}",
                'overview': row.get('overview') or '',
                'poster_path': row.get('poster_path'),
                'season_number': row['season_number'],
                'vote_average': _safe_float(row.get('vote_average')),
            }
            for row in rows
        ]

    def _get_last_or_next_episode(self, table, tv_id):
        row = self._fetch_one(
            f"SELECT * FROM {table} WHERE tv_id = %s",
            (tv_id,),
        )
        if not row:
            return None
        return {
            'id': row.get('episode_id'),
            'air_date': row.get('air_date'),
            'episode_number': row.get('episode_number'),
            'name': row.get('name'),
            'overview': row.get('overview') or '',
            'production_code': row.get('production_code') or '',
            'runtime': row.get('runtime'),
            'season_number': row.get('season_number'),
            'show_id': tv_id,
            'still_path': row.get('still_path'),
            'vote_average': _safe_float(row.get('vote_average')),
            'vote_count': _safe_int(row.get('vote_count')),
        }

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
                    (tv_id, season_number, id, air_date, name, overview, poster_path, vote_average, episode_count, last_fetched, extraction_status, last_extracted_at, error_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW(), %s)
                    """,
                    (
                        series_id,
                        season_number,
                        season_payload.get('id'),
                        season_payload.get('air_date'),
                        season_payload.get('name'),
                        season_payload.get('overview'),
                        season_payload.get('poster_path'),
                        season_payload.get('vote_average'),
                        len(episodes),
                        'success',
                        None,
                    )
                )

                for episode in episodes:
                    cur.execute(
                        f"""
                        INSERT INTO {self.episode_table}
                        (tv_id, season_number, episode_number, id, air_date, name, overview, production_code, runtime, still_path, vote_average, vote_count, last_fetched, extraction_status, last_extracted_at, error_message)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW(), %s)
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
                            episode.get('still_path'),
                            episode.get('vote_average'),
                            episode.get('vote_count'),
                            'success',
                            None,
                        )
                    )
                conn.commit()
        finally:
            conn.close()

    def _movie_summary(self, movie):
        movie_id = movie['id']
        return {
            'id': movie_id,
            'adult': bool(movie.get('adult')),
            'backdrop_path': movie.get('backdrop_path'),
            'genre_ids': [],
            'original_language': movie.get('original_language') or '',
            'original_title': movie.get('original_title') or movie.get('title') or '',
            'overview': movie.get('overview') or '',
            'popularity': _safe_float(movie.get('popularity')),
            'poster_path': movie.get('poster_path'),
            'release_date': _date_to_iso(movie.get('release_date')),
            'title': movie.get('title') or '',
            'video': bool(movie.get('video')),
            'vote_average': _safe_float(movie.get('vote_average')),
            'vote_count': _safe_int(movie.get('vote_count')),
        }

    def _tv_summary(self, tv):
        tv_id = tv['id']
        return {
            'id': tv_id,
            'adult': bool(tv.get('adult')),
            'backdrop_path': tv.get('backdrop_path'),
            'genre_ids': [],
            'origin_country': [],
            'original_language': tv.get('original_language') or '',
            'original_name': tv.get('original_name') or tv.get('name') or '',
            'overview': tv.get('overview') or '',
            'popularity': _safe_float(tv.get('popularity')),
            'poster_path': tv.get('poster_path'),
            'first_air_date': _date_to_iso(tv.get('first_air_date')),
            'name': tv.get('name') or '',
            'vote_average': _safe_float(tv.get('vote_average')),
            'vote_count': _safe_int(tv.get('vote_count')),
        }

    def _ordered_id_rows(self, table, ids):
        ids = [int(item_id) for item_id in ids if str(item_id).isdigit()]
        if not ids:
            return []

        with tmdb_db_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = ANY(%s)", (ids,))
            rows = cur.fetchall()

        rows_by_id = {row['id']: row for row in rows}
        return [rows_by_id[item_id] for item_id in ids if item_id in rows_by_id]

    def get_movies_by_ids(self, ids):
        return [self._movie_summary(movie) for movie in self._ordered_id_rows(self.movie_table, ids)]

    def get_series_by_ids(self, ids):
        return [self._tv_summary(series) for series in self._ordered_id_rows(self.tv_table, ids)]

    def _movie_to_dict(self, movie):
        movie_id = movie['id']
        return {
            'id': movie_id,
            'adult': bool(movie.get('adult')),
            'backdrop_path': movie.get('backdrop_path'),
            'belongs_to_collection': None,
            'budget': movie.get('budget') or 0,
            'genres': self._get_genres('movie', movie_id),
            'homepage': movie.get('homepage') or '',
            'imdb_id': movie.get('imdb_id'),
            'original_language': movie.get('original_language') or '',
            'original_title': movie.get('original_title') or movie.get('title') or '',
            'overview': movie.get('overview') or '',
            'popularity': _safe_float(movie.get('popularity')),
            'poster_path': movie.get('poster_path'),
            'production_companies': [],
            'production_countries': self._get_production_countries('movie', movie_id),
            'release_date': _date_to_iso(movie.get('release_date')),
            'revenue': movie.get('revenue') or 0,
            'runtime': movie.get('runtime'),
            'spoken_languages': self._get_spoken_languages('movie', movie_id),
            'status': movie.get('status') or '',
            'tagline': movie.get('tagline') or '',
            'title': movie.get('title') or '',
            'video': bool(movie.get('video')),
            'vote_average': _safe_float(movie.get('vote_average')),
            'vote_count': _safe_int(movie.get('vote_count')),
            'credits': {'cast': [], 'crew': []},
            'videos': {'results': []},
            'keywords': {'keywords': [], 'results': []},
            'external_ids': {'imdb_id': movie.get('imdb_id')},
            'recommendations': {'page': 1, 'results': [], 'total_pages': 0, 'total_results': 0},
            'similar': {'page': 1, 'results': [], 'total_pages': 0, 'total_results': 0},

            'reviews': {'results': []},
            'images': {'backdrops': [], 'posters': []},
            'lists': {'results': []},
            'translations': {'translations': []},
            'watch_providers': self._get_watch_providers('movie', movie_id),
            'release_dates': self._get_movie_release_dates(movie_id),
        }

    def _tv_to_dict(self, tv):
        tv_id = tv['id']
        return {
            'id': tv_id,
            'adult': bool(tv.get('adult')),
            'backdrop_path': tv.get('backdrop_path'),
            'created_by': self._get_tv_created_by(tv_id),
            'episode_run_time': self._get_tv_episode_runtime(tv_id),
            'first_air_date': _date_to_iso(tv.get('first_air_date')),
            'genres': self._get_genres('tv', tv_id),
            'homepage': tv.get('homepage') or '',
            'in_production': bool(tv.get('in_production')),
            'languages': self._get_tv_languages(tv_id),
            'last_air_date': _date_to_iso(tv.get('last_air_date')),
            'last_episode_to_air': self._get_last_or_next_episode('tv_last_episode', tv_id),
            'name': tv.get('name') or '',
            'next_episode_to_air': self._get_last_or_next_episode('tv_next_episode', tv_id),
            'networks': self._get_tv_networks(tv_id),
            'number_of_episodes': _safe_int(tv.get('number_of_episodes')),
            'number_of_seasons': _safe_int(tv.get('number_of_seasons')),
            'origin_country': self._get_tv_origin_country(tv_id),
            'original_language': tv.get('original_language') or '',
            'original_name': tv.get('original_name') or tv.get('name') or '',
            'overview': tv.get('overview') or '',
            'popularity': _safe_float(tv.get('popularity')),
            'poster_path': tv.get('poster_path'),
            'production_companies': [],
            'production_countries': self._get_production_countries('tv', tv_id),
            'seasons': self._get_tv_seasons_summary(tv_id),
            'spoken_languages': self._get_spoken_languages('tv', tv_id),
            'status': tv.get('status') or '',
            'tagline': tv.get('tagline') or '',
            'type': tv.get('type') or '',
            'vote_average': _safe_float(tv.get('vote_average')),
            'vote_count': _safe_int(tv.get('vote_count')),
            'credits': {'cast': [], 'crew': []},
            'videos': {'results': []},
            'keywords': {'results': []},
            'external_ids': {},
            'recommendations': {'page': 1, 'results': [], 'total_pages': 0, 'total_results': 0},
            'similar': {'page': 1, 'results': [], 'total_pages': 0, 'total_results': 0},

            'reviews': {'results': []},
            'images': {'backdrops': [], 'posters': []},
            'translations': {'translations': []},
            'watch_providers': self._get_watch_providers('tv', tv_id),
            'content_ratings': self._get_tv_content_ratings(tv_id),
            'aggregate_credits': {'cast': [], 'crew': []},
        }

    def _paged_table_query(self, table, order_by, page=1, conditions=None, query_params=None):
        conditions = conditions or []
        query_params = query_params or []
        page = int(page or 1)
        offset = (page - 1) * 20
        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ''
        with tmdb_db_cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS count FROM {table}{where_clause}", query_params)
            total_results = cur.fetchone()['count']
            total_pages = (total_results + 19) // 20
            cur.execute(f"SELECT * FROM {table}{where_clause} ORDER BY {order_by} LIMIT 20 OFFSET %s", list(query_params) + [offset])
            rows = cur.fetchall()
        return page, rows, total_pages, total_results

    def get_popular_movies(self, page=1, params=None):
        try:
            page, movies, total_pages, total_results = self._paged_table_query(self.movie_table, 'popularity DESC', page=page)
            return {
                'page': page,
                'results': [self._movie_summary(m) for m in movies],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_popular_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_top_rated_movies(self, page=1, params=None):
        try:
            page, movies, total_pages, total_results = self._paged_table_query(self.movie_table, 'vote_average DESC, popularity DESC', page=page)
            return {
                'page': page,
                'results': [self._movie_summary(m) for m in movies],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_top_rated_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_upcoming_movies(self, page=1, params=None):
        try:
            page, movies, total_pages, total_results = self._paged_table_query(self.movie_table, 'release_date DESC, popularity DESC', page=page)
            return {
                'page': page,
                'results': [self._movie_summary(m) for m in movies],
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
            page, series, total_pages, total_results = self._paged_table_query(self.tv_table, 'popularity DESC', page=page)
            return {
                'page': page,
                'results': [self._tv_summary(s) for s in series],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_popular_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_top_rated_series(self, page=1, params=None):
        try:
            page, series, total_pages, total_results = self._paged_table_query(self.tv_table, 'vote_average DESC, popularity DESC', page=page)
            return {
                'page': page,
                'results': [self._tv_summary(s) for s in series],
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
            related_ids = self._get_related_ids(self.movie_similar_table, 'from_movie_id', 'to_movie_id', movie_id)
            movies = self._fetch_many_payloads(self.movie_table, related_ids[:20])
            return {
                'page': page,
                'results': [self._movie_to_dict(m) for m in movies],
                'total_pages': 1,
                'total_results': len(related_ids)
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_similar_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_similar_series(self, series_id, page=1, params=None):
        try:
            related_ids = self._get_related_ids(self.tv_similar_table, 'from_tv_id', 'to_tv_id', series_id)
            series = self._fetch_many_payloads(self.tv_table, related_ids[:20])
            return {
                'page': page,
                'results': [self._tv_to_dict(s) for s in series],
                'total_pages': 1,
                'total_results': len(related_ids)
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.get_similar_series: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_movie_genres(self):
        try:
            genres = self._fetch_all(f"SELECT id, name FROM {self.genre_table} WHERE media_type = 'movie' ORDER BY name")
            return {'genres': [{'id': g['id'], 'name': g['name']} for g in genres]}
        except Exception as e:
            print(f"Error in TMDBDBClient.get_movie_genres: {e}")
            return {'genres': []}

    def get_series_genres(self):
        try:
            genres = self._fetch_all(f"SELECT id, name FROM {self.genre_table} WHERE media_type = 'tv' ORDER BY name")
            return {'genres': [{'id': g['id'], 'name': g['name']} for g in genres]}
        except Exception as e:
            print(f"Error in TMDBDBClient.get_series_genres: {e}")
            return {'genres': []}

    def discover_movies(self, params=None):
        if params is None:
            params = {}
        try:
            conditions = []
            query_params = []
            if 'primary_release_date.gte' in params:
                conditions.append('release_date >= %s')
                query_params.append(params['primary_release_date.gte'])
            if 'primary_release_date.lte' in params:
                conditions.append('release_date <= %s')
                query_params.append(params['primary_release_date.lte'])
            if 'with_genres' in params:
                genre_ids = [int(g) for g in str(params['with_genres']).split(',') if str(g).strip().isdigit()]
                if genre_ids:
                    conditions.append(f"id IN (SELECT movie_id FROM {self.movie_genre_table} WHERE genre_id = ANY(%s))")
                    query_params.append(genre_ids)
            order_by = 'release_date DESC, popularity DESC' if 'sort_by' in params and 'primary_release_date.desc' in params['sort_by'] else 'popularity DESC'
            page, movies, total_pages, total_results = self._paged_table_query(self.movie_table, order_by, page=params.get('page', 1), conditions=conditions, query_params=query_params)
            return {
                'page': page,
                'results': [self._movie_summary(m) for m in movies],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.discover_movies: {e}")
            return {'page': params.get('page', 1) if params else 1, 'results': [], 'total_pages': 0, 'total_results': 0}

    def discover_series(self, params=None):
        if params is None:
            params = {}
        try:
            conditions = []
            query_params = []
            if 'air_date.gte' in params:
                conditions.append('first_air_date >= %s')
                query_params.append(params['air_date.gte'])
            if 'air_date.lte' in params:
                conditions.append('first_air_date <= %s')
                query_params.append(params['air_date.lte'])
            if 'with_genres' in params:
                genre_ids = [int(g) for g in str(params['with_genres']).split(',') if str(g).strip().isdigit()]
                if genre_ids:
                    conditions.append(f"id IN (SELECT tv_id FROM {self.tv_genre_table} WHERE genre_id = ANY(%s))")
                    query_params.append(genre_ids)
            order_by = 'first_air_date DESC, popularity DESC' if 'sort_by' in params and 'first_air_date.desc' in params['sort_by'] else 'popularity DESC'
            page, series, total_pages, total_results = self._paged_table_query(self.tv_table, order_by, page=params.get('page', 1), conditions=conditions, query_params=query_params)
            return {
                'page': page,
                'results': [self._tv_summary(s) for s in series],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.discover_series: {e}")
            return {'page': params.get('page', 1) if params else 1, 'results': [], 'total_pages': 0, 'total_results': 0}

    def get_movie_details(self, movie_id):
        try:
            movie = self._fetch_one(f"SELECT * FROM {self.movie_table} WHERE id = %s", (movie_id,))
            return self._movie_to_dict(movie) if movie else None
        except Exception as e:
            print(f"Error in TMDBDBClient.get_movie_details: {e}")
            return None

    def get_series_details(self, series_id):
        try:
            series = self._fetch_one(f"SELECT * FROM {self.tv_table} WHERE id = %s", (series_id,))
            if not series:
                return None

            series_dict = self._tv_to_dict(series)
            seasons = series_dict.get('seasons') or []
            if isinstance(seasons, list):
                missing_seasons = []
                for season in seasons:
                    if not isinstance(season, dict):
                        continue
                    season_number = season.get('season_number')
                    if season_number in (None, 0):
                        continue
                    season_count_row = self._fetch_one(f"SELECT COUNT(*) AS count FROM {self.season_table} WHERE tv_id = %s AND season_number = %s", (series_id, season_number))
                    episode_count_row = self._fetch_one(f"SELECT COUNT(*) AS count FROM {self.episode_table} WHERE tv_id = %s AND season_number = %s", (series_id, season_number))
                    season_count = season_count_row['count'] if season_count_row else 0
                    episode_count = episode_count_row['count'] if episode_count_row else 0
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
            with tmdb_db_cursor() as cur:
                cur.execute(f"SELECT * FROM {self.season_table} WHERE tv_id = %s AND season_number = %s", (series_id, season_number))
                season = cur.fetchone()
                cur.execute(f"SELECT * FROM {self.episode_table} WHERE tv_id = %s AND season_number = %s ORDER BY episode_number", (series_id, season_number))
                episodes = cur.fetchall()

            if season and episodes:
                self._track_db_usage('season', entity_id=series_id, detail=f'season:{season_number}')
                season_dict = {
                    'id': season.get('id') or (series_id * 1000 + season_number),
                    'air_date': _date_to_iso(season.get('air_date')),
                    'name': season.get('name') or f"Season {season_number}",
                    'overview': season.get('overview') or '',
                    'poster_path': season.get('poster_path'),
                    'season_number': season.get('season_number', season_number),
                    'vote_average': _safe_float(season.get('vote_average')),
                    'external_ids': {},
                    'images': {'posters': [], 'backdrops': []},
                    'videos': {'results': []},
                    'credits': {'cast': [], 'crew': []},
                    'episodes': [],
                }
                for episode in episodes:
                    season_dict['episodes'].append({
                        'id': episode.get('id') or (series_id * 100000 + season_number * 1000 + (episode.get('episode_number') or 0)),
                        'air_date': _date_to_iso(episode.get('air_date')),
                        'episode_number': episode.get('episode_number'),
                        'name': episode.get('name') or f"Episode {episode.get('episode_number', '')}",
                        'overview': episode.get('overview') or '',
                        'production_code': episode.get('production_code') or '',
                        'runtime': episode.get('runtime'),
                        'season_number': episode.get('season_number', season_number),
                        'show_id': series_id,
                        'still_path': episode.get('still_path'),
                        'vote_average': _safe_float(episode.get('vote_average')),
                        'vote_count': _safe_int(episode.get('vote_count')),
                        'crew': [],
                        'guest_stars': [],
                        'external_ids': {},
                        'images': {'stills': []},
                        'videos': {'results': []},
                    })
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
            search_pattern = f"%{query}%"
            page, movies, total_pages, total_results = self._paged_table_query(
                self.movie_table,
                'popularity DESC',
                page=page,
                conditions=['(title ILIKE %s OR original_title ILIKE %s)'],
                query_params=[search_pattern, search_pattern],
            )
            return {
                'page': page,
                'results': [self._movie_summary(m) for m in movies],
                'total_pages': total_pages,
                'total_results': total_results
            }
        except Exception as e:
            print(f"Error in TMDBDBClient.search_movies: {e}")
            return {'page': page, 'results': [], 'total_pages': 0, 'total_results': 0}

    def search_series(self, query, page=1):
        try:
            search_pattern = f"%{query}%"
            page, series, total_pages, total_results = self._paged_table_query(
                self.tv_table,
                'popularity DESC',
                page=page,
                conditions=['(name ILIKE %s OR original_name ILIKE %s)'],
                query_params=[search_pattern, search_pattern],
            )
            return {
                'page': page,
                'results': [self._tv_summary(s) for s in series],
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
