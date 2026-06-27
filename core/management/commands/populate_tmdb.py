from django.core.management.base import BaseCommand
from django.conf import settings
from core.models import Movie, Series
from core.tmdb_client import TMDBClient
import random

class Command(BaseCommand):
    help = 'Populate movies and series from TMDB'

    def handle(self, *args, **options):
        client = TMDBClient()
        
        self.stdout.write(self.style.SUCCESS('Fetching popular movies from TMDB...'))
        movies_data = client.get_popular_movies(page=1)
        
        for movie in movies_data.get('results', []):
            Movie.objects.update_or_create(
                stream_id=movie['id'],
                defaults={
                    'title': movie['title'],
                    'rating': str(movie.get('vote_average', '')),
                    'year': movie.get('release_date', '')[:4] if movie.get('release_date') else None,
                    'plot': movie.get('overview', ''),
                    'genre': 'N/A',
                    'cover_url': f"{settings.TMDB_IMAGE_BASE_URL}{movie['poster_path']}" if movie.get('poster_path') else None,
                    'container_extension': 'mp4'
                }
            )
            self.stdout.write(f"Added/Updated movie: {movie['title']}")

        self.stdout.write(self.style.SUCCESS('Fetching popular series from TMDB...'))
        series_data = client.get_popular_series(page=1)
        
        for series in series_data.get('results', []):
            Series.objects.update_or_create(
                series_id=series['id'],
                defaults={
                    'title': series['name'],
                    'rating': str(series.get('vote_average', '')),
                    'plot': series.get('overview', ''),
                    'genre': 'N/A',
                    'cover_url': f"{settings.TMDB_IMAGE_BASE_URL}{series['poster_path']}" if series.get('poster_path') else None,
                }
            )
            self.stdout.write(f"Added/Updated series: {series['name']}")

        self.stdout.write(self.style.SUCCESS('Successfully populated movies and series from TMDB!'))
