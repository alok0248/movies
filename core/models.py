from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify
import secrets


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
        ('tmdb', 'TMDB API'),
        ('tmdb_db', 'TMDB Database (Extracted)'),
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
    footer_enabled = models.BooleanField(default=True)
    footer_title = models.CharField(max_length=100, default='NETFLIX')
    footer_description = models.CharField(max_length=255, default='Stream movies, TV shows, calendar updates, and watchlist content in one place.')
    footer_bottom_text = models.CharField(max_length=255, default='Powered by TMDB data sources and your local media setup.')
    footer_links_title = models.CharField(max_length=100, default='123movies')
    footer_links = models.TextField(blank=True, default='Movies\nTV-Series\nFAQ\'s\nDMCA')
    footer_genres_title = models.CharField(max_length=100, default='Genres')
    footer_genres = models.TextField(blank=True, default='Action\nAnimation\nComedy\nDrama\nHorror')
    footer_countries_title = models.CharField(max_length=100, default='Country')
    footer_countries = models.TextField(blank=True, default='Australia\nCanada\nNetherlands\nUnited Kingdom\nUnited States')
    footer_subscribe_title = models.CharField(max_length=100, default='Subscribe')
    footer_subscribe_text = models.CharField(max_length=255, default='Subscribe to the 123movies mailing list to receive updates on movies, tv-series and news of top movies.')
    footer_subscribe_placeholder = models.CharField(max_length=100, default='Put your email')
    footer_subscribe_button_text = models.CharField(max_length=50, default='Subscribe')
    footer_logo_text = models.CharField(max_length=100, default='123MOVIES')
    footer_logo_tagline = models.CharField(max_length=255, default='Watch Your Favorite Movies Online')
    footer_copyright_text = models.CharField(max_length=255, default='Copyright © 2026 moviefake.com. All Rights Reserved')
    footer_disclaimer_text = models.CharField(max_length=255, default='Disclaimer: This site does not store any files on its server. All contents are provided by non-affiliated third parties.')
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
    URL_FORMAT_CHOICES = [
        ('slug', 'Title (Slug)'),
        ('id', 'TMDB ID'),
    ]
    url_format = models.CharField(max_length=10, choices=URL_FORMAT_CHOICES, default='slug', help_text="URL format for movie/series detail pages")

    # TMDB Database Connection Settings
    tmdb_db_host = models.CharField(max_length=255, blank=True, null=True, default='localhost', help_text="TMDB Database Host")
    tmdb_db_port = models.IntegerField(blank=True, null=True, default=5432, help_text="TMDB Database Port")
    tmdb_db_name = models.CharField(max_length=255, blank=True, null=True, default='tmdb', help_text="TMDB Database Name")
    tmdb_db_user = models.CharField(max_length=255, blank=True, null=True, default='tmdb', help_text="TMDB Database User")
    tmdb_db_password = models.CharField(max_length=255, blank=True, null=True, default='tmdb123!', help_text="TMDB Database Password")
    tmdb_db_enabled = models.BooleanField(default=True, help_text="Enable TMDB Database access")
    tmdb_db_enable_api_fallback = models.BooleanField(default=True, help_text="Allow TMDB API access and fallback when TMDB DB is selected")
    
    # Live TV Option
    hide_live_tv = models.BooleanField(default=True, help_text="Hide Live TV from navigation")

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


class PlayerConfiguration(models.Model):
    MEDIA_TYPE_CHOICES = [
        ('movie', 'Movies'),
        ('tv', 'TV Shows'),
        ('both', 'Both'),
    ]
    ID_TYPE_CHOICES = [
        ('tmdb', 'TMDB ID'),
        ('imdb', 'IMDb ID'),
    ]
    IFRAME_MODE_CHOICES = [
        ('url', 'URL Mode - Enter URL, system creates iframe'),
        ('full', 'Full HTML Mode - Enter complete iframe HTML'),
    ]
    
    name = models.CharField(max_length=100, help_text="Name to identify this player configuration")
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES, default='both')
    is_active = models.BooleanField(default=True, help_text="Whether this configuration is active")
    order = models.IntegerField(default=0, help_text="Display order for dropdown")
    
    # Vidking Player options
    player_color = models.CharField(max_length=10, blank=True, null=True, help_text="Primary color (hex without #, e.g., e50914)")
    auto_play = models.BooleanField(default=False, help_text="Enable auto-play feature")
    next_episode = models.BooleanField(default=False, help_text="Show next episode button (TV only)")
    episode_selector = models.BooleanField(default=False, help_text="Enable episode selection menu (TV only)")
    
    # Player size options
    player_width = models.CharField(max_length=20, default='100%', help_text="Player width (e.g., 100%, 800px)")
    player_height = models.CharField(max_length=20, default='600px', help_text="Player height (e.g., 600px, 100%)")
    
    # Additional iframe options
    frameborder = models.IntegerField(default=0, help_text="iframe frameborder attribute")
    allowfullscreen = models.BooleanField(default=True, help_text="Enable fullscreen mode")
    
    # Custom iframe mode and fields
    custom_iframe_mode = models.CharField(max_length=10, choices=IFRAME_MODE_CHOICES, default='url', help_text="Choose whether to use a simple URL (system creates iframe) or full HTML iframe code", db_column='custom_type')
    
    # Custom iframe URL (overrides Vidking)
    custom_iframe_id_type = models.CharField(max_length=10, choices=ID_TYPE_CHOICES, default='tmdb', help_text="Choose whether custom iframe placeholders should use the TMDB ID or IMDb ID")
    custom_iframe_url = models.TextField(blank=True, null=True, help_text="Shared custom iframe URL. Use placeholders: {content_id}, {tmdb_id}, {imdb_id}, {season}, {episode}")
    custom_movie_iframe_url = models.TextField(blank=True, null=True, help_text="Movie-specific custom iframe URL. Use placeholders: {content_id}, {tmdb_id}, {imdb_id}")
    custom_tv_iframe_url = models.TextField(blank=True, null=True, help_text="TV-specific custom iframe URL. Use placeholders: {content_id}, {tmdb_id}, {imdb_id}, {season}, {episode}")
    
    # Full iframe HTML fields
    custom_iframe_html = models.TextField(blank=True, null=True, help_text="Shared full custom iframe HTML. Use placeholders: {content_id}, {tmdb_id}, {imdb_id}, {season}, {episode}")
    custom_movie_iframe_html = models.TextField(blank=True, null=True, help_text="Movie-specific full custom iframe HTML. Use placeholders: {content_id}, {tmdb_id}, {imdb_id}")
    custom_tv_iframe_html = models.TextField(blank=True, null=True, help_text="TV-specific full custom iframe HTML. Use placeholders: {content_id}, {tmdb_id}, {imdb_id}, {season}, {episode}")
    
    class Meta:
        ordering = ['order', 'name']
        verbose_name = "Player Configuration"
        verbose_name_plural = "Player Configurations"
    
    def save(self, *args, **kwargs):
        # Clean color - remove # if present
        if self.player_color:
            self.player_color = self.player_color.replace('#', '')
        if not self.custom_iframe_id_type:
            self.custom_iframe_id_type = 'tmdb'
        super(PlayerConfiguration, self).save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.get_media_type_display()})"
    
    def _replace_placeholders(self, text, tmdb_id, season=None, episode=None, imdb_id=None):
        selected_id = imdb_id if getattr(self, 'custom_iframe_id_type', 'tmdb') == 'imdb' and imdb_id else tmdb_id
        result = text
        result = result.replace('{tmdb_id}', str(tmdb_id or ''))
        result = result.replace('{imdb_id}', str(imdb_id or ''))
        result = result.replace('{content_id}', str(selected_id or ''))
        if season is not None:
            result = result.replace('{season}', str(season))
        if episode is not None:
            result = result.replace('{episode}', str(episode))
        return result

    def get_player_url(self, media_type, tmdb_id, season=None, episode=None, imdb_id=None):
        custom_url = self.custom_iframe_url
        if media_type == 'movie' and self.custom_movie_iframe_url:
            custom_url = self.custom_movie_iframe_url
        elif media_type == 'tv' and self.custom_tv_iframe_url:
            custom_url = self.custom_tv_iframe_url

        # If custom iframe URL is set, use that with placeholders
        if custom_url:
            return self._replace_placeholders(custom_url, tmdb_id, season, episode, imdb_id)
        
        # Otherwise use Vidking player
        base_url = "https://www.vidking.net/embed"
        
        if media_type == 'movie':
            url = f"{base_url}/movie/{tmdb_id}"
        elif media_type == 'tv' and season and episode:
            url = f"{base_url}/tv/{tmdb_id}/{season}/{episode}"
        else:
            return None
        
        params = []
        if self.player_color:
            # Remove # from color if present
            clean_color = self.player_color.replace('#', '')
            params.append(f"color={clean_color}")
        if self.auto_play:
            params.append("autoPlay=true")
        if self.next_episode and media_type == 'tv':
            params.append("nextEpisode=true")
        if self.episode_selector and media_type == 'tv':
            params.append("episodeSelector=true")
        
        if params:
            url += f"?{'&'.join(params)}"
        
        return url

    def get_player_html(self, media_type, tmdb_id, season=None, episode=None, imdb_id=None):
        # Check if we're in Full HTML mode first
        if self.custom_iframe_mode == 'full':
            custom_html = self.custom_iframe_html
            if media_type == 'movie' and self.custom_movie_iframe_html:
                custom_html = self.custom_movie_iframe_html
            elif media_type == 'tv' and self.custom_tv_iframe_html:
                custom_html = self.custom_tv_iframe_html
            
            if custom_html:
                return self._replace_placeholders(custom_html, tmdb_id, season, episode, imdb_id)
        
        # Otherwise fall back to URL mode
        player_url = self.get_player_url(media_type, tmdb_id, season, episode, imdb_id)
        if not player_url:
            return None
        
        # Build the standard iframe
        allowfullscreen_attr = 'allowfullscreen' if self.allowfullscreen else ''
        return f'<iframe src="{player_url}" width="{self.player_width}" height="{self.player_height}" frameborder="{self.frameborder}" {allowfullscreen_attr}></iframe>'


class ImportLog(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    MEDIA_TYPE_CHOICES = [
        ('movie', 'Movies'),
        ('tv', 'TV Shows'),
        ('both', 'Both'),
    ]
    
    year = models.IntegerField()
    month = models.IntegerField()
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES, default='both')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    movies_imported = models.IntegerField(default=0)
    tv_imported = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, null=True)
    
    class Meta:
        unique_together = ('year', 'month', 'media_type')
        ordering = ('-year', '-month')
        verbose_name = "Import Log"
        verbose_name_plural = "Import Logs"
    
    def __str__(self):
        return f"{self.year}-{self.month:02d} ({self.get_media_type_display()}) - {self.get_status_display()}"


class NavbarItem(models.Model):
    TYPE_CHOICES = [
        ('built_in', 'Built-in Item'),
        ('custom', 'Custom Button'),
    ]
    name = models.CharField(max_length=100, help_text="Display name in navbar")
    item_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='built_in')
    built_in_id = models.CharField(max_length=50, blank=True, null=True, help_text="ID for built-in items like 'home', 'movies', etc.")
    url = models.CharField(max_length=255, blank=True, null=True, help_text="URL for custom items")
    icon = models.CharField(max_length=100, blank=True, null=True, help_text="Font Awesome icon class (e.g., 'fas fa-home')")
    is_active = models.BooleanField(default=True, help_text="Show this item in navbar")
    order = models.IntegerField(default=0, help_text="Display order in navbar")
    dropdown_items = models.JSONField(blank=True, null=True, help_text="JSON array of dropdown items (for dropdown menus)")

    class Meta:
        ordering = ['order', 'name']
        verbose_name_plural = "Navbar Items"

    def __str__(self):
        return f"{self.name} ({self.get_item_type_display()})"


class CalendarMonthCache(models.Model):
    year = models.IntegerField()
    month = models.IntegerField()
    month_name = models.CharField(max_length=20)
    first_day = models.CharField(max_length=10)
    last_day = models.CharField(max_length=10)
    movies = models.JSONField(default=list, blank=True)
    series = models.JSONField(default=list, blank=True)
    last_synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('year', 'month')
        ordering = ['-year', '-month']

    def __str__(self):
        return f"{self.month_name} {self.year}"


class ProviderItem(models.Model):
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    url = models.URLField(max_length=500, blank=True, null=True, help_text="Provider's homepage or official website")
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Provider Item'
        verbose_name_plural = 'Provider Items'

    def __str__(self):
        return self.name


class TMDBApiKey(models.Model):
    key = models.CharField(max_length=255, unique=True, help_text="TMDB API Key")
    is_active = models.BooleanField(default=True, help_text="Is this API key active and usable?")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(blank=True, null=True)
    usage_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-is_active', '-created_at']

    def __str__(self):
        return f"TMDB API Key: {self.key[:10]}..."


class AndroidApp(models.Model):
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    access_username = models.CharField(max_length=255)
    access_password = models.CharField(max_length=255)
    allowed_endpoint = models.CharField(
        max_length=500, blank=True, default='',
        help_text="Allowed app/build identity. Supports comma-separated lists and ranges (e.g., #225, #226, #227 or #225-#250)"
    )
    allowed_build_id = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Allowed build identifier. Supports comma-separated lists and ranges (e.g., 1.0.0, 1.0.1 or 1-10)"
    )
    apk_file = models.FileField(upload_to='android_apks/', blank=True, null=True)
    json_payload = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    total_connections = models.PositiveIntegerField(default=0)
    last_accessed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Android App'
        verbose_name_plural = 'Android Apps'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name) or secrets.token_hex(4)
            slug = base_slug
            counter = 2
            while AndroidApp.objects.exclude(pk=self.pk).filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)


class AndroidAppAccessLog(models.Model):
    android_app = models.ForeignKey(AndroidApp, on_delete=models.CASCADE, related_name='access_logs')
    access_date = models.DateField()
    connection_count = models.PositiveIntegerField(default=0)
    last_accessed_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('android_app', 'access_date')
        ordering = ['-access_date']
        verbose_name = 'Android App Access Log'
        verbose_name_plural = 'Android App Access Logs'

    def __str__(self):
        return f"{self.android_app.name} - {self.access_date}"


class AndroidAppBuildLog(models.Model):
    android_app = models.ForeignKey(AndroidApp, on_delete=models.CASCADE, related_name='build_logs')
    build_identifier = models.CharField(max_length=255)
    access_date = models.DateField()
    connection_count = models.PositiveIntegerField(default=0)
    last_accessed_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('android_app', 'build_identifier', 'access_date')
        ordering = ['-access_date', 'build_identifier']
        verbose_name = 'Android App Build Log'
        verbose_name_plural = 'Android App Build Logs'

    def __str__(self):
        return f"{self.android_app.name} - {self.build_identifier} - {self.access_date}"


class AndroidAppFailedAttempt(models.Model):
    FAILURE_REASON_CHOICES = [
        ('auth_missing', 'Missing Authorization Header'),
        ('auth_invalid_format', 'Invalid Authorization Format'),
        ('auth_invalid_creds', 'Invalid Credentials'),
        ('identity_invalid', 'Invalid Endpoint Identity'),
        ('app_inactive', 'App Not Active'),
        ('app_not_found', 'App Not Found'),
    ]
    
    android_app = models.ForeignKey(AndroidApp, on_delete=models.CASCADE, related_name='failed_attempts', blank=True, null=True)
    app_slug = models.CharField(max_length=255, blank=True, null=True)
    failure_reason = models.CharField(max_length=50, choices=FAILURE_REASON_CHOICES)
    request_identity = models.CharField(max_length=500, blank=True, default='')
    build_identifier = models.CharField(max_length=255, blank=True, default='')
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.TextField(blank=True, default='')
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-attempted_at']
        verbose_name = 'Android App Failed Attempt'
        verbose_name_plural = 'Android App Failed Attempts'

    def __str__(self):
        app_name = self.android_app.name if self.android_app else self.app_slug or 'Unknown'
        return f"{app_name} - {self.get_failure_reason_display()} - {self.attempted_at}"


class AndroidAppDevice(models.Model):
    android_app = models.ForeignKey(AndroidApp, on_delete=models.CASCADE, related_name='devices')
    user_id = models.CharField(max_length=255, db_index=True)  # Unique per device/app (Android ID or UUID)
    device_model = models.CharField(max_length=255, blank=True, default='')  # Device model (e.g. Pixel 7 Pro)
    os_version = models.CharField(max_length=50, blank=True, default='')  # Android OS version (e.g. 13, 14)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    total_visits = models.PositiveIntegerField(default=0)
    
    class Meta:
        unique_together = ('android_app', 'user_id')
        ordering = ['-last_seen_at']
        verbose_name = 'Android App Device'
        verbose_name_plural = 'Android App Devices'
    
    def __str__(self):
        return f"{self.android_app.name} - {self.user_id}"


class AndroidAppDailyUniqueVisitor(models.Model):
    android_app = models.ForeignKey(AndroidApp, on_delete=models.CASCADE, related_name='daily_unique_visitors')
    access_date = models.DateField()
    unique_visitor_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ('android_app', 'access_date')
        ordering = ['-access_date']
        verbose_name = 'Android App Daily Unique Visitor'
        verbose_name_plural = 'Android App Daily Unique Visitors'
    
    def __str__(self):
        return f"{self.android_app.name} - {self.access_date}: {self.unique_visitor_count} unique visitors"


class AndroidAppDeviceVisit(models.Model):
    device = models.ForeignKey(AndroidAppDevice, on_delete=models.CASCADE, related_name='visits')
    android_app = models.ForeignKey(AndroidApp, on_delete=models.CASCADE, related_name='device_visits')
    visited_at = models.DateTimeField(auto_now_add=True)
    build_identifier = models.CharField(max_length=255, blank=True, default='')
    request_identity = models.CharField(max_length=500, blank=True, default='')
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    device_model = models.CharField(max_length=255, blank=True, default='')
    os_version = models.CharField(max_length=50, blank=True, default='')
    
    class Meta:
        ordering = ['-visited_at']
        verbose_name = 'Android App Device Visit'
        verbose_name_plural = 'Android App Device Visits'
    
    def __str__(self):
        return f"{self.device.user_id} - {self.visited_at}"


class DataSourceUsageLog(models.Model):
    SOURCE_CHOICES = [
        ('db', 'Database'),
        ('api', 'TMDB API'),
        ('api_fallback', 'TMDB API Fallback'),
    ]

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    entity_type = models.CharField(max_length=50)
    entity_id = models.IntegerField(blank=True, null=True)
    detail = models.CharField(max_length=255, blank=True, null=True)
    usage_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('source', 'entity_type', 'entity_id', 'detail')
        ordering = ['-last_used_at']

    def __str__(self):
        return f"{self.source} - {self.entity_type} - {self.entity_id or 'n/a'}"


# Add active player references to SiteSettings
SiteSettings.add_to_class('active_movie_player', models.ForeignKey(PlayerConfiguration, on_delete=models.SET_NULL, null=True, blank=True, related_name='movie_settings'))
SiteSettings.add_to_class('active_tv_player', models.ForeignKey(PlayerConfiguration, on_delete=models.SET_NULL, null=True, blank=True, related_name='tv_settings'))
