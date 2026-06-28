from django.db import models
from django.contrib.auth.models import User


class ContentRow(models.Model):
    ROW_TYPE_CHOICES = [
        ('popular', 'Popular'),
        ('top_rated', 'Top Rated'),
        ('upcoming', 'Upcoming (Movies)'),
        ('now_playing', 'Now Playing (Movies)'),
        ('on_the_air', 'On The Air (TV)'),
        ('airing_today', 'Airing Today (TV)'),
        ('genre', 'By Genre'),
        ('custom', 'Custom Filter'),
    ]

    title = models.CharField(max_length=100)
    media_type = models.CharField(max_length=10, choices=[('movie', 'Movies'), ('tv', 'TV Shows')])
    row_type = models.CharField(max_length=20, choices=ROW_TYPE_CHOICES, default='popular')
    genre_tmdb_id = models.IntegerField(blank=True, null=True)
    region = models.CharField(max_length=10, blank=True, null=True, help_text="TMDB region (e.g., US, GB, IN, FR)")
    language = models.CharField(max_length=20, blank=True, null=True, help_text="TMDB language (e.g., en-US, es-ES, fr-FR)")
    sort_by = models.CharField(max_length=50, default='popularity.desc', blank=True, help_text="TMDB sort parameter, e.g., popularity.desc, vote_average.desc")
    filter_params = models.TextField(blank=True, help_text="Additional TMDB filter params in JSON format, e.g., {\"vote_average.gte\": 7}")
    items_per_page = models.IntegerField(default=20, help_text="Number of items to load per page for this row")
    is_active = models.BooleanField(default=True)
    auto_scroll = models.BooleanField(default=False, help_text="Enable auto-scrolling for this row on the homepage")
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.title} ({self.get_media_type_display()})"


class SiteSettings(models.Model):
    DATA_SOURCE_CHOICES = [
        ('tmdb', 'TMDB'),
        ('local', 'Local Database'),
        ('xtream', 'Xtream'),
    ]
    ITEMS_PER_ROW_CHOICES = [
        (1, '1'),
        (2, '2'),
        (3, '3'),
        (4, '4'),
        (6, '6'),
    ]
    CARD_SIZE_CHOICES = [
        ('small', 'Small'),
        ('medium', 'Medium'),
        ('large', 'Large'),
    ]
    TEXT_SIZE_CHOICES = [
        ('small', 'Small'),
        ('medium', 'Medium'),
        ('large', 'Large'),
        ('xl', 'Extra Large'),
    ]
    THEME_STYLE_CHOICES = [
        ('netflix', 'Netflix (Dark Red)'),
        ('amazon', 'Amazon Prime (Dark Blue)'),
        ('hbo', 'HBO Max (Dark Purple)'),
        ('disney', 'Disney+ (Dark Blue/Teal)'),
        ('spotify', 'Spotify (Dark Green)'),
    ]
    FONT_FAMILY_CHOICES = [
        ('system-ui', 'System UI'),
        ('Arial, sans-serif', 'Arial'),
        ('Helvetica, sans-serif', 'Helvetica'),
        ('\"Segoe UI\", sans-serif', 'Segoe UI'),
        ('Georgia, serif', 'Georgia'),
        ('\"Times New Roman\", serif', 'Times New Roman'),
        ('\"Courier New\", monospace', 'Courier New'),
    ]
    data_source = models.CharField(max_length=10, choices=DATA_SOURCE_CHOICES, default='tmdb')
    items_per_row = models.IntegerField(choices=ITEMS_PER_ROW_CHOICES, default=3)
    card_size = models.CharField(max_length=10, choices=CARD_SIZE_CHOICES, default='medium')
    title_size = models.CharField(max_length=10, choices=TEXT_SIZE_CHOICES, default='medium')
    text_size = models.CharField(max_length=10, choices=TEXT_SIZE_CHOICES, default='medium')
    theme_style = models.CharField(max_length=20, choices=THEME_STYLE_CHOICES, default='netflix')
    font_family = models.CharField(max_length=50, choices=FONT_FAMILY_CHOICES, default='system-ui')
    enable_sidebar_ads = models.BooleanField(default=False)
    sidebar_ads_code = models.TextField(blank=True, null=True, help_text='HTML/JS code for sidebar ads')
    brand_name = models.CharField(max_length=50, default='NETFLIX')
    brand_tagline = models.CharField(max_length=200, default='Unlimited movies, TV shows, and more')
    brand_color = models.CharField(max_length=20, default='#e50914')
    enable_url_blocking = models.BooleanField(default=False, help_text="Enable URL blocking for non-admin pages")
    blocked_urls = models.TextField(blank=True, null=True, help_text="List of URLs to block (one per line), or 'all' to block all except admin")
    redirect_url = models.CharField(max_length=200, blank=True, null=True, default="/", help_text="URL to redirect blocked requests to")
    email_host = models.CharField(max_length=100, blank=True, null=True, default='smtp.gmail.com', help_text="Email host (e.g., smtp.gmail.com)")
    email_port = models.IntegerField(blank=True, null=True, default=587, help_text="Email port (e.g., 587 for TLS)")
    email_host_user = models.EmailField(blank=True, null=True, help_text="Email address (e.g., your@gmail.com)")
    email_host_password = models.CharField(max_length=200, blank=True, null=True, help_text="Email app password (not regular Gmail password)")
    email_use_tls = models.BooleanField(default=True, help_text="Use TLS for email")
    watch_region = models.CharField(max_length=10, blank=True, null=True, default='US', help_text="TMDB watch region (e.g., US, GB, IN, FR)")
    curated_top_movie_ids = models.TextField(blank=True, null=True, help_text="Comma-separated TMDB IDs of top movies (e.g., 123,456,789)")
    curated_top_series_ids = models.TextField(blank=True, null=True, help_text="Comma-separated TMDB IDs of top series (e.g., 123,456,789)")

    class Meta:
        verbose_name_plural = "Site Settings"

    def __str__(self):
        return f"Data Source: {self.get_data_source_display()}"

    @classmethod
    def get_settings(cls):
        settings, created = cls.objects.get_or_create(pk=1)
        return settings


class WatchList(models.Model):
    MEDIA_TYPE_CHOICES = [
        ('movie', 'Movie'),
        ('tv', 'TV Show'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='watchlist')
    tmdb_id = models.IntegerField()
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES)
    title = models.CharField(max_length=255)
    poster_path = models.CharField(max_length=255, blank=True, null=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'tmdb_id', 'media_type')
        ordering = ('-added_at',)

    def __str__(self):
        return f"{self.user.username} - {self.title} ({self.get_media_type_display()})"


class TMDBGenre(models.Model):
    id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=100)
    media_type = models.CharField(max_length=10, choices=[('movie', 'Movie'), ('tv', 'TV')])

    class Meta:
        unique_together = ('id', 'media_type')
        verbose_name = "TMDB Genre"
        verbose_name_plural = "TMDB Genres"

    def __str__(self):
        return f"{self.name} ({self.media_type})"


class TMDBMovie(models.Model):
    id = models.IntegerField(primary_key=True)
    adult = models.BooleanField(default=False)
    backdrop_path = models.CharField(max_length=255, blank=True, null=True)
    belongs_to_collection = models.JSONField(blank=True, null=True)
    budget = models.BigIntegerField(blank=True, null=True)
    genres = models.JSONField(blank=True, null=True)
    homepage = models.CharField(max_length=255, blank=True, null=True)
    imdb_id = models.CharField(max_length=20, blank=True, null=True)
    original_language = models.CharField(max_length=10, blank=True, null=True)
    original_title = models.CharField(max_length=255, blank=True, null=True)
    overview = models.TextField(blank=True, null=True)
    popularity = models.FloatField(blank=True, null=True)
    poster_path = models.CharField(max_length=255, blank=True, null=True)
    production_companies = models.JSONField(blank=True, null=True)
    production_countries = models.JSONField(blank=True, null=True)
    release_date = models.CharField(max_length=20, blank=True, null=True)
    revenue = models.BigIntegerField(blank=True, null=True)
    runtime = models.IntegerField(blank=True, null=True)
    spoken_languages = models.JSONField(blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    tagline = models.CharField(max_length=255, blank=True, null=True)
    title = models.CharField(max_length=255)
    video = models.BooleanField(default=False)
    vote_average = models.FloatField(blank=True, null=True)
    vote_count = models.IntegerField(blank=True, null=True)
    last_fetched = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TMDB Movie"
        verbose_name_plural = "TMDB Movies"
        ordering = ('-popularity',)

    def __str__(self):
        return self.title or str(self.id)


class TMDBTV(models.Model):
    id = models.IntegerField(primary_key=True)
    adult = models.BooleanField(default=False)
    backdrop_path = models.CharField(max_length=255, blank=True, null=True)
    created_by = models.JSONField(blank=True, null=True)
    episode_run_time = models.JSONField(blank=True, null=True)
    first_air_date = models.CharField(max_length=20, blank=True, null=True)
    genres = models.JSONField(blank=True, null=True)
    homepage = models.CharField(max_length=255, blank=True, null=True)
    in_production = models.BooleanField(default=False)
    languages = models.JSONField(blank=True, null=True)
    last_air_date = models.CharField(max_length=20, blank=True, null=True)
    last_episode_to_air = models.JSONField(blank=True, null=True)
    name = models.CharField(max_length=255)
    next_episode_to_air = models.JSONField(blank=True, null=True)
    networks = models.JSONField(blank=True, null=True)
    number_of_episodes = models.IntegerField(blank=True, null=True)
    number_of_seasons = models.IntegerField(blank=True, null=True)
    origin_country = models.JSONField(blank=True, null=True)
    original_language = models.CharField(max_length=10, blank=True, null=True)
    original_name = models.CharField(max_length=255, blank=True, null=True)
    overview = models.TextField(blank=True, null=True)
    popularity = models.FloatField(blank=True, null=True)
    poster_path = models.CharField(max_length=255, blank=True, null=True)
    production_companies = models.JSONField(blank=True, null=True)
    production_countries = models.JSONField(blank=True, null=True)
    seasons = models.JSONField(blank=True, null=True)
    spoken_languages = models.JSONField(blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    tagline = models.CharField(max_length=255, blank=True, null=True)
    type = models.CharField(max_length=50, blank=True, null=True)
    vote_average = models.FloatField(blank=True, null=True)
    vote_count = models.IntegerField(blank=True, null=True)
    last_fetched = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TMDB TV Show"
        verbose_name_plural = "TMDB TV Shows"
        ordering = ('-popularity',)

    def __str__(self):
        return self.name or str(self.id)
