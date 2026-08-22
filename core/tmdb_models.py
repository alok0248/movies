from django.db import models


class TMDBMovie(models.Model):
    id = models.IntegerField(primary_key=True)
    adult = models.BooleanField(null=True, blank=True)
    backdrop_path = models.CharField(max_length=500, null=True, blank=True)
    belongs_to_collection = models.JSONField(null=True, blank=True)
    budget = models.BigIntegerField(null=True, blank=True)
    genres = models.JSONField(null=True, blank=True)
    homepage = models.CharField(max_length=500, null=True, blank=True)
    imdb_id = models.CharField(max_length=20, null=True, blank=True)
    original_language = models.CharField(max_length=10, null=True, blank=True)
    original_title = models.CharField(max_length=500, null=True, blank=True)
    overview = models.TextField(null=True, blank=True)
    popularity = models.FloatField(null=True, blank=True)
    poster_path = models.CharField(max_length=500, null=True, blank=True)
    production_companies = models.JSONField(null=True, blank=True)
    production_countries = models.JSONField(null=True, blank=True)
    release_date = models.DateField(null=True, blank=True)
    revenue = models.BigIntegerField(null=True, blank=True)
    runtime = models.IntegerField(null=True, blank=True)
    spoken_languages = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=50, null=True, blank=True)
    tagline = models.CharField(max_length=500, null=True, blank=True)
    title = models.CharField(max_length=500, null=True, blank=True)
    video = models.BooleanField(null=True, blank=True)
    vote_average = models.FloatField(null=True, blank=True)
    vote_count = models.IntegerField(null=True, blank=True)
    
    # Extra fields for fetch status
    last_fetched = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tmdb_movies'
    
    def __str__(self):
        return f"{self.title or 'Unknown'} ({self.id})"


class TMDBTV(models.Model):
    id = models.IntegerField(primary_key=True)
    adult = models.BooleanField(null=True, blank=True)
    backdrop_path = models.CharField(max_length=500, null=True, blank=True)
    created_by = models.JSONField(null=True, blank=True)
    episode_run_time = models.JSONField(null=True, blank=True)
    first_air_date = models.DateField(null=True, blank=True)
    genres = models.JSONField(null=True, blank=True)
    homepage = models.CharField(max_length=500, null=True, blank=True)
    in_production = models.BooleanField(null=True, blank=True)
    languages = models.JSONField(null=True, blank=True)
    last_air_date = models.DateField(null=True, blank=True)
    last_episode_to_air = models.JSONField(null=True, blank=True)
    name = models.CharField(max_length=500, null=True, blank=True)
    next_episode_to_air = models.JSONField(null=True, blank=True)
    networks = models.JSONField(null=True, blank=True)
    number_of_episodes = models.IntegerField(null=True, blank=True)
    number_of_seasons = models.IntegerField(null=True, blank=True)
    origin_country = models.JSONField(null=True, blank=True)
    original_language = models.CharField(max_length=10, null=True, blank=True)
    original_name = models.CharField(max_length=500, null=True, blank=True)
    overview = models.TextField(null=True, blank=True)
    popularity = models.FloatField(null=True, blank=True)
    poster_path = models.CharField(max_length=500, null=True, blank=True)
    production_companies = models.JSONField(null=True, blank=True)
    production_countries = models.JSONField(null=True, blank=True)
    seasons = models.JSONField(null=True, blank=True)
    spoken_languages = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=50, null=True, blank=True)
    tagline = models.CharField(max_length=500, null=True, blank=True)
    type = models.CharField(max_length=50, null=True, blank=True)
    vote_average = models.FloatField(null=True, blank=True)
    vote_count = models.IntegerField(null=True, blank=True)
    
    # Extra fields for fetch status
    last_fetched = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tmdb_tv'
    
    def __str__(self):
        return f"{self.name or 'Unknown'} ({self.id})"


class TMDBGenre(models.Model):
    id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=100, null=True, blank=True)
    media_type = models.CharField(max_length=10, null=True, blank=True, choices=[('movie', 'Movie'), ('tv', 'TV')])
    
    class Meta:
        db_table = 'tmdb_genres'
    
    def __str__(self):
        return f"{self.name or 'Unknown'} ({self.id})"
