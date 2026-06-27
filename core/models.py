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
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.title} ({self.get_media_type_display()})"


class SiteSettings(models.Model):
    DATA_SOURCE_CHOICES = [
        ('tmdb', 'TMDB'),
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
    data_source = models.CharField(max_length=10, choices=DATA_SOURCE_CHOICES, default='tmdb')
    items_per_row = models.IntegerField(choices=ITEMS_PER_ROW_CHOICES, default=3)
    card_size = models.CharField(max_length=10, choices=CARD_SIZE_CHOICES, default='medium')
    title_size = models.CharField(max_length=10, choices=TEXT_SIZE_CHOICES, default='medium')
    text_size = models.CharField(max_length=10, choices=TEXT_SIZE_CHOICES, default='medium')
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
