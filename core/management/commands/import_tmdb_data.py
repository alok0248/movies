import os
import sqlite3
from django.core.management.base import BaseCommand
from core.models import TMDBMovie, TMDBTV, TMDBGenre


class Command(BaseCommand):
    help = 'Import TMDB data from test/db_import/tmdb_data.db'

    def add_arguments(self, parser):
        parser.add_argument(
            '--db-path',
            type=str,
            default=os.path.join('test', 'db_import', 'tmdb_data.db'),
            help='Path to the TMDB import database file'
        )

    def handle(self, *args, **options):
        db_path = options['db_path']

        if not os.path.exists(db_path):
            self.stderr.write(self.style.ERROR(f"Database file not found: {db_path}"))
            return

        self.stdout.write(self.style.SUCCESS(f"Importing data from: {db_path}"))

        # Connect to import database
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Import genres
        self.stdout.write("Importing genres...")
        cursor.execute("SELECT * FROM tmdb_genres")
        genre_rows = cursor.fetchall()
        
        for row in genre_rows:
            TMDBGenre.objects.update_or_create(
                id=row['id'],
                defaults={
                    'name': row['name'],
                    'media_type': row['media_type']
                }
            )
        self.stdout.write(self.style.SUCCESS(f"  Imported {len(genre_rows)} genres"))

        # Import movies
        self.stdout.write("Importing movies...")
        cursor.execute("SELECT * FROM tmdb_movies")
        movie_rows = cursor.fetchall()
        
        for idx, row in enumerate(movie_rows, 1):
            TMDBMovie.objects.update_or_create(
                id=row['id'],
                defaults={
                    'adult': bool(row['adult']),
                    'backdrop_path': row['backdrop_path'],
                    'belongs_to_collection': self._load_json(row['belongs_to_collection']),
                    'budget': row['budget'],
                    'genres': self._load_json(row['genres']),
                    'homepage': row['homepage'],
                    'imdb_id': row['imdb_id'],
                    'original_language': row['original_language'],
                    'original_title': row['original_title'],
                    'overview': row['overview'],
                    'popularity': row['popularity'],
                    'poster_path': row['poster_path'],
                    'production_companies': self._load_json(row['production_companies']),
                    'production_countries': self._load_json(row['production_countries']),
                    'release_date': row['release_date'],
                    'revenue': row['revenue'],
                    'runtime': row['runtime'],
                    'spoken_languages': self._load_json(row['spoken_languages']),
                    'status': row['status'],
                    'tagline': row['tagline'],
                    'title': row['title'],
                    'video': bool(row['video']),
                    'vote_average': row['vote_average'],
                    'vote_count': row['vote_count']
                }
            )
            if idx % 100 == 0:
                self.stdout.write(f"  Processed {idx}/{len(movie_rows)} movies")
        self.stdout.write(self.style.SUCCESS(f"  Imported {len(movie_rows)} movies"))

        # Import TV shows
        self.stdout.write("Importing TV shows...")
        cursor.execute("SELECT * FROM tmdb_tv")
        tv_rows = cursor.fetchall()
        
        for idx, row in enumerate(tv_rows, 1):
            TMDBTV.objects.update_or_create(
                id=row['id'],
                defaults={
                    'adult': bool(row['adult']),
                    'backdrop_path': row['backdrop_path'],
                    'created_by': self._load_json(row['created_by']),
                    'episode_run_time': self._load_json(row['episode_run_time']),
                    'first_air_date': row['first_air_date'],
                    'genres': self._load_json(row['genres']),
                    'homepage': row['homepage'],
                    'in_production': bool(row['in_production']),
                    'languages': self._load_json(row['languages']),
                    'last_air_date': row['last_air_date'],
                    'last_episode_to_air': self._load_json(row['last_episode_to_air']),
                    'name': row['name'],
                    'next_episode_to_air': self._load_json(row['next_episode_to_air']),
                    'networks': self._load_json(row['networks']),
                    'number_of_episodes': row['number_of_episodes'],
                    'number_of_seasons': row['number_of_seasons'],
                    'origin_country': self._load_json(row['origin_country']),
                    'original_language': row['original_language'],
                    'original_name': row['original_name'],
                    'overview': row['overview'],
                    'popularity': row['popularity'],
                    'poster_path': row['poster_path'],
                    'production_companies': self._load_json(row['production_companies']),
                    'production_countries': self._load_json(row['production_countries']),
                    'seasons': self._load_json(row['seasons']),
                    'spoken_languages': self._load_json(row['spoken_languages']),
                    'status': row['status'],
                    'tagline': row['tagline'],
                    'type': row['type'],
                    'vote_average': row['vote_average'],
                    'vote_count': row['vote_count']
                }
            )
            if idx % 100 == 0:
                self.stdout.write(f"  Processed {idx}/{len(tv_rows)} TV shows")
        self.stdout.write(self.style.SUCCESS(f"  Imported {len(tv_rows)} TV shows"))

        conn.close()
        self.stdout.write(self.style.SUCCESS("Import complete!"))

    def _load_json(self, value):
        if not value:
            return None
        import json
        try:
            return json.loads(value)
        except Exception:
            return None
