import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.core.management.base import BaseCommand
from django.conf import settings
from core.models import TMDBMovie, TMDBTV, TMDBGenre


class Command(BaseCommand):
    help = 'Import full TMDB catalog into local Django database (multithreaded)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--mode',
            type=str,
            default='popular',
            choices=['popular', 'top_rated', 'upcoming', 'all_movies', 'all_tv', 'genres', 'full'],
            help='Import mode: popular (default), top_rated, upcoming, all_movies, all_tv, genres, full'
        )
        parser.add_argument(
            '--max-workers',
            type=int,
            default=20,
            help='Maximum number of worker threads (default: 20)'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=100,
            help='Number of items to process in each batch (default: 100)'
        )
        parser.add_argument(
            '--api-keys-file',
            type=str,
            default='api_keys.txt',
            help='Path to API keys file (default: api_keys.txt)'
        )
        parser.add_argument(
            '--max-pages',
            type=int,
            default=50,
            help='Maximum pages to fetch for popular/top_rated (default: 50)'
        )

    def handle(self, *args, **options):
        mode = options['mode']
        max_workers = options['max_workers']
        batch_size = options['batch_size']
        api_keys_file = options['api_keys_file']
        max_pages = options['max_pages']

        # Load API keys
        api_keys = self._load_api_keys(api_keys_file)
        if not api_keys:
            self.stderr.write(self.style.ERROR("No API keys found!"))
            return

        self.api_keys = api_keys
        self.current_key_index = 0
        self.key_lock = threading.Lock()
        self.base_url = "https://api.themoviedb.org/3"
        self.requests_per_second = 20 * len(api_keys)
        self.last_request_time = 0
        self.request_lock = threading.Lock()

        self.stdout.write(self.style.SUCCESS(f"Using {len(api_keys)} API keys, {max_workers} workers"))

        if mode in ['genres', 'full']:
            self._import_genres()

        if mode in ['popular', 'full']:
            self._import_popular_movies(max_pages, max_workers, batch_size)
            self._import_popular_tv(max_pages, max_workers, batch_size)

        if mode in ['top_rated', 'full']:
            self._import_top_rated_movies(max_pages, max_workers, batch_size)
            self._import_top_rated_tv(max_pages, max_workers, batch_size)

        if mode in ['upcoming', 'full']:
            self._import_upcoming_movies(max_pages, max_workers, batch_size)

        if mode in ['all_movies', 'full']:
            self._import_all_movies(max_workers, batch_size)

        if mode in ['all_tv', 'full']:
            self._import_all_tv(max_workers, batch_size)

        self.stdout.write(self.style.SUCCESS("Import complete!"))

    def _load_api_keys(self, path):
        """Load API keys from file"""
        if not os.path.exists(path):
            # Try project root
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            path = os.path.join(project_root, path)
        
        if os.path.exists(path):
            with open(path, 'r') as f:
                keys = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            return keys
        return []

    def _get_next_api_key(self):
        """Get next API key in round-robin fashion"""
        with self.key_lock:
            key = self.api_keys[self.current_key_index]
            self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
            return key

    def _rate_limit(self):
        """Apply rate limiting"""
        with self.request_lock:
            current_time = time.time()
            time_since_last = current_time - self.last_request_time
            min_interval = 1.0 / self.requests_per_second
            if time_since_last < min_interval:
                time.sleep(min_interval - time_since_last)
            self.last_request_time = time.time()

    def _make_request(self, endpoint, params=None):
        """Make TMDB API request with rate limiting"""
        import requests
        if params is None:
            params = {}
        
        self._rate_limit()
        params['api_key'] = self._get_next_api_key()
        url = f"{self.base_url}{endpoint}"
        
        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                time.sleep(retry_after)
                return self._make_request(endpoint, params)
            elif response.status_code == 200:
                return response.json()
            else:
                self.stdout.write(f"[!] Request failed: {response.status_code} - {url}")
                return None
        except Exception as e:
            self.stdout.write(f"[!] Request error: {e}")
            return None

    def _import_genres(self):
        """Import movie and TV genres"""
        self.stdout.write("Importing movie genres...")
        movie_genres = self._make_request("/genre/movie/list")
        if movie_genres and 'genres' in movie_genres:
            for genre in movie_genres['genres']:
                TMDBGenre.objects.update_or_create(
                    id=genre['id'],
                    defaults={'name': genre['name'], 'media_type': 'movie'}
                )
            self.stdout.write(self.style.SUCCESS(f"  Imported {len(movie_genres['genres'])} movie genres"))

        self.stdout.write("Importing TV genres...")
        tv_genres = self._make_request("/genre/tv/list")
        if tv_genres and 'genres' in tv_genres:
            for genre in tv_genres['genres']:
                TMDBGenre.objects.update_or_create(
                    id=genre['id'],
                    defaults={'name': genre['name'], 'media_type': 'tv'}
                )
            self.stdout.write(self.style.SUCCESS(f"  Imported {len(tv_genres['genres'])} TV genres"))

    def _fetch_ids_from_endpoint(self, endpoint, max_pages, params=None):
        """Fetch all IDs from a paginated endpoint"""
        if params is None:
            params = {}
        ids = []
        page = 1
        while page <= max_pages:
            params['page'] = page
            data = self._make_request(endpoint, params)
            if not data or 'results' not in data:
                break
            for item in data['results']:
                ids.append(item['id'])
            total_pages = data.get('total_pages', 1)
            if page >= total_pages:
                break
            page += 1
        return ids

    def _bulk_import_movies(self, movie_ids, batch_size, max_workers):
        """Import movies by IDs using multithreading"""
        if not movie_ids:
            return 0

        self.stdout.write(f"Fetching {len(movie_ids)} movies with {max_workers} workers...")
        
        imported = 0
        for i in range(0, len(movie_ids), batch_size):
            batch = movie_ids[i:i + batch_size]
            movies_data = []
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_id = {executor.submit(self._make_request, f"/movie/{mid}"): mid for mid in batch}
                for future in as_completed(future_to_id):
                    movie_id = future_to_id[future]
                    try:
                        movie = future.result()
                        if movie:
                            movies_data.append(movie)
                    except Exception as e:
                        self.stdout.write(f"[!] Error fetching movie {movie_id}: {e}")
            
            # Bulk save
            if movies_data:
                self._save_movies_batch(movies_data)
                imported += len(movies_data)
                self.stdout.write(f"  Saved {len(movies_data)} movies (total: {imported})")
        
        return imported

    def _bulk_import_tv(self, tv_ids, batch_size, max_workers):
        """Import TV shows by IDs using multithreading"""
        if not tv_ids:
            return 0

        self.stdout.write(f"Fetching {len(tv_ids)} TV shows with {max_workers} workers...")
        
        imported = 0
        for i in range(0, len(tv_ids), batch_size):
            batch = tv_ids[i:i + batch_size]
            tv_data = []
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_id = {executor.submit(self._make_request, f"/tv/{tid}"): tid for tid in batch}
                for future in as_completed(future_to_id):
                    tv_id = future_to_id[future]
                    try:
                        tv = future.result()
                        if tv:
                            tv_data.append(tv)
                    except Exception as e:
                        self.stdout.write(f"[!] Error fetching TV {tv_id}: {e}")
            
            # Bulk save
            if tv_data:
                self._save_tv_batch(tv_data)
                imported += len(tv_data)
                self.stdout.write(f"  Saved {len(tv_data)} TV shows (total: {imported})")
        
        return imported

    def _save_movies_batch(self, movies_data):
        """Bulk save movies to database"""
        movie_objects = []
        for movie in movies_data:
            movie_objects.append(TMDBMovie(
                id=movie.get('id'),
                adult=movie.get('adult', False),
                backdrop_path=movie.get('backdrop_path'),
                belongs_to_collection=movie.get('belongs_to_collection'),
                budget=movie.get('budget'),
                genres=movie.get('genres'),
                homepage=movie.get('homepage'),
                imdb_id=movie.get('imdb_id'),
                original_language=movie.get('original_language'),
                original_title=movie.get('original_title'),
                overview=movie.get('overview'),
                popularity=movie.get('popularity'),
                poster_path=movie.get('poster_path'),
                production_companies=movie.get('production_companies'),
                production_countries=movie.get('production_countries'),
                release_date=movie.get('release_date'),
                revenue=movie.get('revenue'),
                runtime=movie.get('runtime'),
                spoken_languages=movie.get('spoken_languages'),
                status=movie.get('status'),
                tagline=movie.get('tagline'),
                title=movie.get('title'),
                video=movie.get('video', False),
                vote_average=movie.get('vote_average'),
                vote_count=movie.get('vote_count')
            ))
        TMDBMovie.objects.bulk_create(movie_objects, ignore_conflicts=True)

    def _save_tv_batch(self, tv_data):
        """Bulk save TV shows to database"""
        tv_objects = []
        for tv in tv_data:
            tv_objects.append(TMDBTV(
                id=tv.get('id'),
                adult=tv.get('adult', False),
                backdrop_path=tv.get('backdrop_path'),
                created_by=tv.get('created_by'),
                episode_run_time=tv.get('episode_run_time'),
                first_air_date=tv.get('first_air_date'),
                genres=tv.get('genres'),
                homepage=tv.get('homepage'),
                in_production=tv.get('in_production', False),
                languages=tv.get('languages'),
                last_air_date=tv.get('last_air_date'),
                last_episode_to_air=tv.get('last_episode_to_air'),
                name=tv.get('name'),
                next_episode_to_air=tv.get('next_episode_to_air'),
                networks=tv.get('networks'),
                number_of_episodes=tv.get('number_of_episodes'),
                number_of_seasons=tv.get('number_of_seasons'),
                origin_country=tv.get('origin_country'),
                original_language=tv.get('original_language'),
                original_name=tv.get('original_name'),
                overview=tv.get('overview'),
                popularity=tv.get('popularity'),
                poster_path=tv.get('poster_path'),
                production_companies=tv.get('production_companies'),
                production_countries=tv.get('production_countries'),
                seasons=tv.get('seasons'),
                spoken_languages=tv.get('spoken_languages'),
                status=tv.get('status'),
                tagline=tv.get('tagline'),
                type=tv.get('type'),
                vote_average=tv.get('vote_average'),
                vote_count=tv.get('vote_count')
            ))
        TMDBTV.objects.bulk_create(tv_objects, ignore_conflicts=True)

    def _import_popular_movies(self, max_pages, max_workers, batch_size):
        """Import popular movies"""
        self.stdout.write("Importing popular movies...")
        ids = self._fetch_ids_from_endpoint("/movie/popular", max_pages)
        self.stdout.write(f"  Found {len(ids)} popular movie IDs")
        count = self._bulk_import_movies(ids, batch_size, max_workers)
        self.stdout.write(self.style.SUCCESS(f"  Imported {count} popular movies"))

    def _import_popular_tv(self, max_pages, max_workers, batch_size):
        """Import popular TV shows"""
        self.stdout.write("Importing popular TV shows...")
        ids = self._fetch_ids_from_endpoint("/tv/popular", max_pages)
        self.stdout.write(f"  Found {len(ids)} popular TV IDs")
        count = self._bulk_import_tv(ids, batch_size, max_workers)
        self.stdout.write(self.style.SUCCESS(f"  Imported {count} popular TV shows"))

    def _import_top_rated_movies(self, max_pages, max_workers, batch_size):
        """Import top rated movies"""
        self.stdout.write("Importing top rated movies...")
        ids = self._fetch_ids_from_endpoint("/movie/top_rated", max_pages)
        self.stdout.write(f"  Found {len(ids)} top rated movie IDs")
        count = self._bulk_import_movies(ids, batch_size, max_workers)
        self.stdout.write(self.style.SUCCESS(f"  Imported {count} top rated movies"))

    def _import_top_rated_tv(self, max_pages, max_workers, batch_size):
        """Import top rated TV shows"""
        self.stdout.write("Importing top rated TV shows...")
        ids = self._fetch_ids_from_endpoint("/tv/top_rated", max_pages)
        self.stdout.write(f"  Found {len(ids)} top rated TV IDs")
        count = self._bulk_import_tv(ids, batch_size, max_workers)
        self.stdout.write(self.style.SUCCESS(f"  Imported {count} top rated TV shows"))

    def _import_upcoming_movies(self, max_pages, max_workers, batch_size):
        """Import upcoming movies"""
        self.stdout.write("Importing upcoming movies...")
        ids = self._fetch_ids_from_endpoint("/movie/upcoming", max_pages)
        self.stdout.write(f"  Found {len(ids)} upcoming movie IDs")
        count = self._bulk_import_movies(ids, batch_size, max_workers)
        self.stdout.write(self.style.SUCCESS(f"  Imported {count} upcoming movies"))

    def _import_all_movies(self, max_workers, batch_size):
        """Import ALL movies from TMDB (by ID iteration)"""
        self.stdout.write("Importing ALL movies (this will take a while)...")
        latest = self._make_request("/movie/latest")
        if not latest:
            self.stdout.write(self.style.ERROR("  Could not fetch latest movie"))
            return
        
        latest_id = latest.get('id', 1000000)
        self.stdout.write(f"  Latest movie ID: {latest_id}")
        
        # Process in chunks
        chunk_size = 10000
        for start in range(1, latest_id + 1, chunk_size):
            end = min(start + chunk_size - 1, latest_id)
            self.stdout.write(f"  Processing IDs {start} to {end}...")
            ids = list(range(start, end + 1))
            count = self._bulk_import_movies(ids, batch_size, max_workers)
            self.stdout.write(self.style.SUCCESS(f"  Imported {count} movies in this chunk"))

    def _import_all_tv(self, max_workers, batch_size):
        """Import ALL TV shows from TMDB (by ID iteration)"""
        self.stdout.write("Importing ALL TV shows (this will take a while)...")
        latest = self._make_request("/tv/latest")
        if not latest:
            self.stdout.write(self.style.ERROR("  Could not fetch latest TV show"))
            return
        
        latest_id = latest.get('id', 300000)
        self.stdout.write(f"  Latest TV ID: {latest_id}")
        
        chunk_size = 10000
        for start in range(1, latest_id + 1, chunk_size):
            end = min(start + chunk_size - 1, latest_id)
            self.stdout.write(f"  Processing IDs {start} to {end}...")
            ids = list(range(start, end + 1))
            count = self._bulk_import_tv(ids, batch_size, max_workers)
            self.stdout.write(self.style.SUCCESS(f"  Imported {count} TV shows in this chunk"))