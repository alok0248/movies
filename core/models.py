from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify
from django.utils import timezone
import secrets


def normalize_poster_path(value):
    """Normalize a poster reference to a clean TMDB-relative path.

    Accepts relative paths ('/abc.jpg'), bare paths ('abc.jpg') and full
    URLs ('https://image.tmdb.org/t/p/w500/abc.jpg') and always returns the
    relative '/abc.jpg' form (or '' when empty) so stored values stay
    consistent regardless of which client sent them.
    """
    if not value:
        return ''
    v = str(value).strip()
    if not v:
        return ''
    if '://' in v:
        # Strip any image-host prefix and size segment:
        # https://image.tmdb.org/t/p/w500/abc.jpg -> /abc.jpg
        path = v.split('://', 1)[1]
        path = path.split('/', 1)[1] if '/' in path else ''
        segs = path.split('/')
        # image hosts use /t/p/{size}/... — drop the leading t/p/{size}
        if len(segs) >= 3 and segs[0] == 't' and segs[1] == 'p':
            segs = segs[3:]
        v = '/' + '/'.join(segs)
    if not v.startswith('/'):
        v = '/' + v
    return v


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
        ('cinevault', 'CineVault (Cinematic Dark)'),
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
    theme_style = models.CharField(max_length=20, choices=THEME_STYLE_CHOICES, default='cinevault')
    font_family = models.CharField(max_length=50, choices=FONT_FAMILY_CHOICES, default='system-ui')
    brand_name = models.CharField(max_length=50, default='newmovies')
    brand_tagline = models.CharField(max_length=200, default='Unlimited movies, TV shows, and more')
    brand_color = models.CharField(max_length=20, default='#00c896')
    footer_enabled = models.BooleanField(default=True)
    footer_title = models.CharField(max_length=100, default='NETFLIX')
    footer_description = models.CharField(max_length=255, default='Stream movies, TV shows, calendar updates, and watchlist content in one place.')
    footer_bottom_text = models.CharField(max_length=255, default='Powered by TMDB data sources and your local media setup.')
    footer_links_title = models.CharField(max_length=100, default='')
    footer_links = models.TextField(blank=True, default='Movies\nTV-Series\nFAQ\'s\nDMCA')
    footer_genres_title = models.CharField(max_length=100, default='Genres')
    footer_genres = models.TextField(blank=True, default='Action\nAnimation\nComedy\nDrama\nHorror')
    footer_countries_title = models.CharField(max_length=100, default='Country')
    footer_countries = models.TextField(blank=True, default='Australia\nCanada\nNetherlands\nUnited Kingdom\nUnited States')
    footer_subscribe_title = models.CharField(max_length=100, default='Subscribe')
    footer_subscribe_text = models.CharField(max_length=255, default='')
    footer_subscribe_placeholder = models.CharField(max_length=100, default='Put your email')
    footer_subscribe_button_text = models.CharField(max_length=50, default='Subscribe')
    footer_logo_text = models.CharField(max_length=100, default='')
    footer_logo_tagline = models.CharField(max_length=255, default='')
    footer_copyright_text = models.CharField(max_length=255, default='')
    footer_disclaimer_text = models.CharField(max_length=255, default='')
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
    
    # Bot Tracking
    bot_ips = models.TextField(blank=True, null=True, help_text="Comma-separated list of IP addresses for our bot (e.g., 192.168.1.1,10.0.0.1)")
    
    # Google AdSense
    adsense_verification_meta = models.CharField(max_length=200, blank=True, null=True, help_text="Google AdSense verification meta tag content (e.g., abcdef123456)")
    adsense_client_id = models.CharField(max_length=100, blank=True, null=True, help_text="Google AdSense client ID (e.g., ca-pub-1234567890123456)")
    bot_user_agents = models.TextField(blank=True, null=True, help_text="Comma-separated list of user-agent strings for our bot")

    # Auto-click ad cadence (count-based)
    auto_click_every_clicks = models.IntegerField(
        default=10,
        help_text="Number of user clicks (counted) between ad auto-open triggers. After this many clicks, the NEXT click triggers the ad tab to open. Set 1 to trigger on every eligible click."
    )

    # Deferred ad loading (click-threshold + consent)
    default_clicks_required = models.IntegerField(
        default=0,
        help_text="Default click threshold required before ANY ad content loads on a page. Used as the effective Clicks Required for ads whose clicks_required is 0 (or not set). Set 0 to rely solely on per-Ad clicks_required."
    )
    require_ad_consent = models.BooleanField(
        default=False,
        help_text="If checked, ad content will NEVER load until the user explicitly gives consent (GDPR/CCPA). A consent banner is shown until the user accepts or declines."
    )
    ad_consent_message = models.TextField(
        default="We use third-party ads to keep this service free. By clicking \"Accept\", you agree to ad loading and cookies as described in our Privacy Policy.",
        help_text="GDPR/CCPA consent banner copy shown to users when require_ad_consent is enabled."
    )
    max_ad_load_retries = models.IntegerField(
        default=3,
        help_text="Maximum number of automatic retries for failed ad network script/asset loads before the slot is marked failed."
    )

    IDM_VISIBILITY_CHOICES = [
        ('hide', 'Hide IDM (Disabled)'),
        ('logged_in', 'Logged-In Users Only'),
        ('admin_only', 'Admin / Staff Users Only'),
        ('all_users', 'All Users (Include Guests)'),
    ]
    idm_visibility = models.CharField(
        max_length=20,
        choices=IDM_VISIBILITY_CHOICES,
        default='all_users',
        help_text="Who can see and use the IDM Auto-Extract button and UI. Use 'Admin / Staff Users Only' to restrict this feature to site administrators, or 'Hide IDM' to disable it entirely."
    )
    tile_ad_every_n = models.IntegerField(
        default=10,
        help_text="Insert an in-grid ad tile every N movie/series cards. Set 0 to disable tile ads entirely. Example: 10 = one ad tile after every 10 cards."
    )
    enable_tile_click_gating = models.BooleanField(
        default=True,
        help_text="If enabled, the first click on a movie/series card opens an ad (new tab), the second click on the same card navigates to the detail page."
    )
    TILE_AD_SOURCE_CHOICES = [
        ('ads_table', 'Ad records with position=Tile (from Ads page)'),
        ('amazon_random', 'Amazon Affiliate Product List (Random)'),
        ('amazon_sequence', 'Amazon Affiliate Product List (In Sequence)'),
    ]
    tile_ad_source = models.CharField(
        max_length=20,
        choices=TILE_AD_SOURCE_CHOICES,
        default='ads_table',
        help_text="Where the in-grid ad tile content comes from — either individual Tile-position Ad records, or the bulk Amazon Affiliate Product list."
    )
    TILE_GATING_SOURCE_CHOICES = [
        ('autoclick_urls', 'All active ad URLs (autoclick pool)'),
        ('amazon_random', 'Amazon Affiliate Product List (Random)'),
        ('amazon_sequence', 'Amazon Affiliate Product List (In Sequence)'),
    ]
    tile_gating_source = models.CharField(
        max_length=20,
        choices=TILE_GATING_SOURCE_CHOICES,
        default='autoclick_urls',
        help_text="Where the first-click-gating ad URL comes from — the global autoclick ad pool, or the Amazon Affiliate Product list."
    )
    dev_mode_protection = models.BooleanField(
        default=False,
        help_text="If enabled, users with browser DevTools/Inspector open will see a 404 page with ads."
    )

    class Meta:
        verbose_name_plural = "Site Settings"

    def __str__(self):
        return f"Data Source: {self.get_data_source_display()}"

    @classmethod
    def get_settings(cls):
        settings, created = cls.objects.get_or_create(pk=1)
        return settings


class Ad(models.Model):
    PROVIDER_CHOICES = [
        ('google_adsense', 'Google AdSense'),
        ('amazon_affiliate', 'Amazon Affiliate'),
        ('custom_script', 'Custom Script'),
        ('custom_image', 'Custom Image/Banner'),
    ]

    POSITION_CHOICES = [
        ('head', 'Head (All Pages)'),
        ('sidebar', 'Sidebar'),
        ('above_player', 'Above Video Player'),
        ('below_player', 'Below Video Player'),
        ('above_content', 'Above Main Content'),
        ('below_content', 'Below Main Content'),
        ('footer', 'Footer'),
        ('popup', 'Popup / Modal'),
        ('tile', 'Tile / Card (In-Grid)'),
    ]

    name = models.CharField(max_length=255, help_text="Name of the ad for internal use")
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES, default='custom_script')
    position = models.CharField(max_length=50, choices=POSITION_CHOICES, default='sidebar', help_text="Where this ad will appear on the site")
    script = models.TextField(blank=True, null=True, help_text="Ad script code (for Custom Script and Google AdSense)")
    affiliate_url = models.URLField(blank=True, null=True, help_text="Amazon Associates tracking/product URL")
    affiliate_image_url = models.URLField(blank=True, null=True, help_text="Amazon product image URL")
    affiliate_title = models.CharField(max_length=255, blank=True, null=True, help_text="Product title shown in the affiliate card")
    affiliate_price = models.CharField(max_length=100, blank=True, null=True, help_text="Optional product price text")
    image_url = models.URLField(blank=True, null=True, help_text="Image URL for custom banner ad")
    link_url = models.URLField(blank=True, null=True, help_text="Destination URL for custom banner ad")
    alt_text = models.CharField(max_length=255, blank=True, null=True, help_text="Alt text for custom banner ad image")
    clicks_required = models.IntegerField(default=0, help_text="Number of user clicks required before showing this ad")
    is_active = models.BooleanField(default=True, help_text="Whether this ad is active and can be shown")
    use_for_android = models.BooleanField(default=False, help_text="Include this ad in Android app responses")
    order = models.IntegerField(default=0, help_text="Display order (lower numbers show first)")

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return self.name


class AdImpression(models.Model):
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name='impressions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='ad_impressions')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    impression_date = models.DateField(auto_now_add=True)
    views = models.IntegerField(default=0)
    clicks = models.IntegerField(default=0)
    
    class Meta:
        unique_together = (('ad', 'user', 'impression_date'), ('ad', 'ip_address', 'impression_date'))
        indexes = [
            models.Index(fields=['ad', 'impression_date']),
            models.Index(fields=['user', 'impression_date']),
            models.Index(fields=['ip_address', 'impression_date']),
        ]
    
    def __str__(self):
        return f"Impression for {self.ad.name} on {self.impression_date}"


class AmazonAffiliateProduct(models.Model):
    affiliate_url = models.URLField(max_length=1000, help_text="Full Amazon affiliate link including your tracking tag (e.g. https://www.amazon.com/dp/B08XYZ/?tag=yourname-20)")
    title = models.CharField(max_length=500, blank=True, null=True, help_text="Product name shown on the ad tile.")
    image_url = models.URLField(max_length=1000, blank=True, null=True, help_text="Product cover image URL (Amazon CDN or your own).")
    price = models.CharField(max_length=100, blank=True, null=True, help_text="Optional price text, e.g. $29.99 or 'From $9.99'.")
    is_active = models.BooleanField(default=True, help_text="Uncheck to exclude this product from rotation without deleting it.")
    order = models.IntegerField(default=0, help_text="Display order / sequence position (lower numbers come first when using Sequence mode).")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'title']
        verbose_name = "Amazon Affiliate Product"
        verbose_name_plural = "Amazon Affiliate Products"

    def __str__(self):
        return self.title or self.affiliate_url[:80]


class UserActivity(models.Model):
    # Cross-DB link (external users DB) — no DB-level constraint.
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='activity', null=True, blank=True, db_constraint=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    activity_date = models.DateField(auto_now_add=True)
    clicks_today = models.IntegerField(default=0)
    pages_viewed_today = models.IntegerField(default=0)
    
    class Meta:
        unique_together = (('user', 'activity_date'), ('ip_address', 'activity_date'))
        indexes = [
            models.Index(fields=['user', 'activity_date']),
            models.Index(fields=['ip_address', 'activity_date']),
        ]
    
    def __str__(self):
        if self.user:
            return f"Activity for {self.user.username} on {self.activity_date}"
        else:
            return f"Activity for IP {self.ip_address} on {self.activity_date}"


class WatchList(models.Model):
    MEDIA_TYPE_CHOICES = [
        ('movie', 'Movie'),
        ('tv', 'TV Show'),
    ]
    # Cross-DB link (external users DB) — no DB-level constraint.
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='watchlist', db_constraint=False)
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
    use_for_android = models.BooleanField(default=False, help_text="Use this player configuration for Android app endpoints")
    
    # Vidking Player options
    player_color = models.CharField(max_length=10, blank=True, null=True, help_text="Primary color (hex without #, e.g., e50914)")
    
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
        fullscreen_attrs = ''
        if self.allowfullscreen:
            fullscreen_attrs = 'allowfullscreen webkitallowfullscreen mozallowfullscreen'
        return f'<iframe src="{player_url}" width="{self.player_width}" height="{self.player_height}" frameborder="{self.frameborder}" {fullscreen_attrs} allow="autoplay; fullscreen; picture-in-picture"></iframe>'


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


class WatchRegion(models.Model):
    code = models.CharField(max_length=2, unique=True, help_text="ISO 3166-1 country code, e.g. US, IN, GB")
    name = models.CharField(max_length=255)
    is_enabled = models.BooleanField(default=True)
    display_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'name', 'code']
        verbose_name = 'Watch Region'
        verbose_name_plural = 'Watch Regions'

    def save(self, *args, **kwargs):
        self.code = (self.code or '').strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.code})"


class ProviderItem(models.Model):
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    tmdb_provider_id = models.IntegerField(blank=True, null=True, unique=True)
    logo_path = models.CharField(max_length=500, blank=True, null=True)
    display_priority = models.IntegerField(default=0)
    supports_movies = models.BooleanField(default=False)
    supports_tv = models.BooleanField(default=False)
    url = models.URLField(max_length=500, blank=True, null=True, help_text="Provider's homepage or official website")
    is_enabled = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_priority', 'name']
        verbose_name = 'Provider Item'
        verbose_name_plural = 'Provider Items'

    def __str__(self):
        return self.name


class ProviderRegionAvailability(models.Model):
    MEDIA_TYPE_CHOICES = [('movie', 'Movies'), ('tv', 'TV Series')]

    provider = models.ForeignKey(ProviderItem, on_delete=models.CASCADE, related_name='region_availability')
    region = models.ForeignKey(WatchRegion, on_delete=models.CASCADE, related_name='provider_availability')
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES)
    display_priority = models.IntegerField(default=0)

    class Meta:
        ordering = ['region__code', 'media_type', 'display_priority', 'provider__name']
        constraints = [
            models.UniqueConstraint(fields=['provider', 'region', 'media_type'], name='unique_provider_region_media'),
        ]
        verbose_name = 'Provider Region Availability'
        verbose_name_plural = 'Provider Region Availability'

    def __str__(self):
        return f"{self.provider.name} - {self.region.code} - {self.media_type}"


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
    data_retention_days = models.PositiveIntegerField(
        default=30,
        help_text="Number of days to retain analytics logs. Older data is automatically deleted."
    )
    log_collection_enabled = models.BooleanField(
        default=True,
        help_text="Enable or disable log collection from this app. When disabled, the app's /log/ endpoint will reject incoming logs."
    )
    log_retention_days = models.PositiveIntegerField(
        default=30,
        help_text="Number of days to retain AndroidAppLog entries. Older logs are automatically deleted by the clean_analytics command."
    )
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

    def clean_old_analytics_data(self):
        """Delete analytics logs older than their respective retention settings."""
        from datetime import date, timedelta
        analytics_cutoff = date.today() - timedelta(days=self.data_retention_days)
        log_cutoff = date.today() - timedelta(days=self.log_retention_days)
        deleted_counts = {
            'access_logs': AndroidAppAccessLog.objects.filter(
                android_app=self, access_date__lt=analytics_cutoff
            ).delete()[0],
            'build_logs': AndroidAppBuildLog.objects.filter(
                android_app=self, access_date__lt=analytics_cutoff
            ).delete()[0],
            'daily_unique_visitors': AndroidAppDailyUniqueVisitor.objects.filter(
                android_app=self, access_date__lt=analytics_cutoff
            ).delete()[0],
            'device_visits': AndroidAppDeviceVisit.objects.filter(
                android_app=self, visited_at__date__lt=analytics_cutoff
            ).delete()[0],
            'failed_attempts': AndroidAppFailedAttempt.objects.filter(
                android_app=self, attempted_at__date__lt=analytics_cutoff
            ).delete()[0],
            'app_logs': AndroidAppLog.objects.filter(
                android_app=self, timestamp__date__lt=log_cutoff
            ).delete()[0],
        }
        return deleted_counts


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
    full_request = models.TextField(blank=True, default='', help_text='Full request details (headers, params, etc.) for debugging')
    
    class Meta:
        ordering = ['-visited_at']
        verbose_name = 'Android App Device Visit'
        verbose_name_plural = 'Android App Device Visits'

    def __str__(self):
        return f"{self.device.user_id} - {self.visited_at}"


class AndroidAppLog(models.Model):
    LOG_LEVEL_CHOICES = [
        ('debug', 'Debug'),
        ('info', 'Info'),
        ('warn', 'Warning'),
        ('error', 'Error'),
        ('fatal', 'Fatal'),
    ]
    android_app = models.ForeignKey(AndroidApp, on_delete=models.CASCADE, related_name='app_logs')
    device = models.ForeignKey(AndroidAppDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name='app_logs')
    user_id = models.CharField(max_length=255, blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=20, choices=LOG_LEVEL_CHOICES, default='info')
    tag = models.CharField(max_length=255, blank=True, default='', help_text='Log tag / category')
    message = models.TextField(blank=True, default='')
    data = models.TextField(blank=True, default='', help_text='JSON payload for extra context')
    build_identifier = models.CharField(max_length=255, blank=True, default='')
    device_model = models.CharField(max_length=255, blank=True, default='')
    os_version = models.CharField(max_length=50, blank=True, default='')
    ip_address = models.GenericIPAddressField(blank=True, null=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Android App Log'
        verbose_name_plural = 'Android App Logs'
        indexes = [
            models.Index(fields=['android_app', '-timestamp']),
            models.Index(fields=['level']),
        ]

    def __str__(self):
        return f"[{self.level.upper()}] {self.android_app.name} - {self.tag} - {self.timestamp}"


class WebsiteVisitor(models.Model):
    visitor_id = models.UUIDField(unique=True, db_index=True)
    user = models.ForeignKey(
        'auth.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='website_visitors',
        db_constraint=False
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    last_path = models.CharField(max_length=500, blank=True, default='')
    total_visits = models.PositiveIntegerField(default=0)
    last_ip_address = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.TextField(blank=True, default='')
    
    class Meta:
        ordering = ['-last_seen_at']
        verbose_name = 'Website Visitor'
        verbose_name_plural = 'Website Visitors'
    
    def __str__(self):
        return str(self.visitor_id)


class WebsiteVisitorVisit(models.Model):
    visitor = models.ForeignKey(
        WebsiteVisitor,
        related_name='visits',
        on_delete=models.CASCADE
    )
    visited_at = models.DateTimeField(auto_now_add=True)
    path = models.CharField(max_length=500)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    is_bot = models.BooleanField(default=False, help_text="Whether this visit is from our bot")
    
    class Meta:
        ordering = ['-visited_at']
        verbose_name = 'Website Visitor Visit'
        verbose_name_plural = 'Website Visitor Visits'
        indexes = [
            models.Index(fields=['visited_at']),
            models.Index(fields=['path']),
        ]
    
    def __str__(self):
        return f"{self.visitor.visitor_id} - {self.path} - {self.visited_at}"


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
SiteSettings.add_to_class('player_ui_style', models.CharField(max_length=20, default='default', choices=[('default', 'Classic Player'), ('netflix', 'Netflix-Style Player')], help_text='Player UI style for video playback'))


class Subscriber(models.Model):
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.email

    @property
    def messages_received(self):
        return self.deliveries.count()


class EmailMessage(models.Model):
    subject = models.CharField(max_length=255)
    body = models.TextField()
    sent_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    recipient_count = models.PositiveIntegerField(default=0)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return f"{self.subject} ({self.recipient_count} recipients)"


class EmailDelivery(models.Model):
    STATUS_CHOICES = [
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]
    message = models.ForeignKey(EmailMessage, on_delete=models.CASCADE, related_name='deliveries')
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name='deliveries')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='sent')
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-sent_at']
        unique_together = ('message', 'subscriber')

    def __str__(self):
        return f"{self.subscriber.email} <- {self.message.subject} [{self.status}]"


class SyncedUser(models.Model):
    """Android app user synced via POST /api/user/sync."""
    # Cross-DB link (external users DB vs default auth) — no DB-level constraint.
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='synced_profiles', db_constraint=False)
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=255, blank=True, default='')
    photo_url = models.URLField(blank=True, default='')
    google_id = models.CharField(max_length=100, blank=True, default='')
    app_version = models.CharField(max_length=50, blank=True, default='')
    build_number = models.IntegerField(default=0)
    device_id = models.CharField(max_length=100, blank=True, default='')
    device_model = models.CharField(max_length=100, blank=True, default='')
    os_version = models.CharField(max_length=50, blank=True, default='')

    # Subscription
    is_subscribed = models.BooleanField(default=False)
    plan = models.CharField(max_length=100, blank=True, default='')
    valid_until = models.DateField(blank=True, null=True)
    features = models.JSONField(default=list, blank=True)

    last_synced_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_synced_at']

    def __str__(self):
        return f"{self.display_name or self.email}"

    @property
    def days_remaining(self):
        if not self.valid_until:
            return 0
        from datetime import date
        delta = self.valid_until - date.today()
        return max(0, delta.days)

    def subscription_payload(self):
        return {
            'isSubscribed': self.is_subscribed,
            'plan': self.plan,
            'validUntil': self.valid_until.isoformat() if self.valid_until else None,
            'daysRemaining': self.days_remaining,
            'features': self.features or [],
        }


class EmailVerification(models.Model):
    """Email verification token + 6-digit OTP for registration.

    The link-based token lets browser users verify by clicking the emailed
    link; the OTP lets the Android app verify without opening the browser
    (POST /api/user/verify-email/ with {email, otp}).
    """
    # Cross-DB link (external users DB) — no DB-level constraint.
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='email_verifications', db_constraint=False)
    token = models.CharField(max_length=64, unique=True)
    otp = models.CharField(max_length=6, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    verified = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.email} - {'verified' if self.verified else 'pending'}"

    @property
    def is_expired(self):
        from django.utils import timezone
        from datetime import timedelta
        return timezone.now() > self.created_at + timedelta(minutes=5)

    @staticmethod
    def generate_otp():
        import secrets
        return f"{secrets.randbelow(1000000):06d}"


class PasswordResetOTP(models.Model):
    """6-digit OTP for password reset via the Android app."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='password_reset_otps', db_constraint=False)
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.email} - {'used' if self.used else self.otp}"

    @property
    def is_expired(self):
        from datetime import timedelta
        return timezone.now() > self.created_at + timedelta(minutes=10)

    @property
    def is_valid(self):
        return not self.used and not self.is_expired

    @staticmethod
    def generate():
        import secrets
        return f"{secrets.randbelow(1000000):06d}"


class PlayHistory(models.Model):
    """Track play sessions for logged-in users."""
    MEDIA_TYPE_CHOICES = [
        ('movie', 'Movie'),
        ('tv', 'TV Show'),
    ]
    # Cross-DB link (external users DB) — no DB-level constraint.
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='play_history', db_constraint=False)
    tmdb_id = models.IntegerField()
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES)
    title = models.CharField(max_length=255)
    poster_path = models.CharField(max_length=255, blank=True, null=True)
    season_number = models.IntegerField(null=True, blank=True)
    episode_number = models.IntegerField(null=True, blank=True)
    episode_title = models.CharField(max_length=255, blank=True, default='')
    duration_seconds = models.IntegerField(default=0)
    total_duration_seconds = models.IntegerField(default=0)
    completed = models.BooleanField(default=False)
    last_played_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'tmdb_id', 'media_type', 'season_number', 'episode_number')
        ordering = ['-last_played_at']
        indexes = [
            models.Index(fields=['user', 'last_played_at']),
        ]

    def __str__(self):
        label = self.title
        if self.season_number and self.episode_number:
            label += f" S{self.season_number}E{self.episode_number}"
        return f"{self.user.username} - {label}"

    @property
    def progress_percent(self):
        if not self.total_duration_seconds:
            return 0
        return min(100, round((self.duration_seconds / self.total_duration_seconds) * 100))

    @property
    def formatted_duration(self):
        m, s = divmod(self.duration_seconds, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class UserCloudData(models.Model):
    """Cloud storage for user data synced between Android app and web.
    Stores playback progress, watch history, favorites, watchlist, and ratings."""
    # Cross-DB link (external users DB) — no DB-level constraint.
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='cloud_data', db_constraint=False)
    playback_progress = models.JSONField(default=dict, blank=True)
    seen_keys = models.JSONField(default=list, blank=True)
    watch_history = models.JSONField(default=list, blank=True)
    favorites = models.JSONField(default=list, blank=True)
    watchlist_ids = models.JSONField(default=list, blank=True)
    user_ratings = models.JSONField(default=dict, blank=True)
    app_settings = models.JSONField(default=dict, blank=True)
    downloads = models.JSONField(default=list, blank=True)
    playlists = models.JSONField(default=list, blank=True)
    data_version = models.IntegerField(default=0)
    last_synced_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'User Cloud Data'
        verbose_name_plural = 'User Cloud Data'

    def __str__(self):
        return f"CloudData: {self.user.username}"

    def get_cloud_payload(self):
        return {
            'version': 1,
            'timestamp': int(self.last_synced_at.timestamp() * 1000) if self.last_synced_at else 0,
            'settings': self.app_settings or {},
            'playbackProgress': self.playback_progress or {},
            'seenKeys': self.seen_keys or [],
            'watchHistory': self.watch_history or [],
            'favorites': self.favorites or [],
            'watchlist': self.watchlist_ids or [],
            'userRatings': self.user_ratings or {},
            'downloads': self.downloads or [],
            'playlists': self.playlists or [],
        }

    def merge_incoming(self, incoming):
        """Merge incoming data from Android app. App data wins on conflicts.
        Handles both old format (positionMs/mediaId) and new format (position/id)."""
        if not incoming:
            return

        def _get_id(item):
            """Get mediaId from an item. Supports dicts ('mediaId'/'id' keys) and
            plain string/number ids the app may send — never crashes on either."""
            if isinstance(item, dict):
                return item.get('mediaId') or item.get('id')
            return item

        # Playback progress — app data wins, normalize fields
        app_progress = incoming.get('playbackProgress', {})
        if app_progress:
            for key, entry in app_progress.items():
                if not isinstance(entry, dict):
                    continue
                # Normalize: position -> positionMs, duration -> durationMs
                if 'position' in entry and 'positionMs' not in entry:
                    entry['positionMs'] = entry.pop('position')
                if 'duration' in entry and 'durationMs' not in entry:
                    entry['durationMs'] = entry.pop('duration')
                if 'updatedAt' in entry and 'lastUpdated' not in entry:
                    entry['lastUpdated'] = entry.pop('updatedAt')
                self.playback_progress[key] = entry

        # Settings — app data wins
        app_settings = incoming.get('settings', {})
        if app_settings:
            self.app_settings = app_settings

        # Seen keys — union
        app_seen = incoming.get('seenKeys', [])
        if app_seen:
            existing = set(self.seen_keys or [])
            existing.update(app_seen)
            self.seen_keys = list(existing)

        # Watch history — prepend, deduplicate by id
        app_history = incoming.get('watchHistory', [])
        if app_history:
            existing_ids = {_get_id(h) for h in (self.watch_history or [])}
            new_items = [h for h in app_history if _get_id(h) not in existing_ids]
            self.watch_history = new_items + (self.watch_history or [])

        # Favorites — union by id
        app_favs = incoming.get('favorites', [])
        if app_favs:
            existing_ids = {_get_id(f) for f in (self.favorites or [])}
            new_favs = [f for f in app_favs if _get_id(f) not in existing_ids]
            self.favorites = (self.favorites or []) + new_favs

        # Watchlist — union by id
        app_watchlist = incoming.get('watchlist', [])
        if app_watchlist:
            existing_ids = {_get_id(w) for w in (self.watchlist_ids or [])}
            new_wl = [w for w in app_watchlist if _get_id(w) not in existing_ids]
            self.watchlist_ids = (self.watchlist_ids or []) + new_wl

        # Ratings — app data wins
        app_ratings = incoming.get('userRatings', {})
        if app_ratings:
            self.user_ratings.update(app_ratings)

        # Downloads — app data wins
        app_downloads = incoming.get('downloads', [])
        if app_downloads:
            self.downloads = app_downloads

        # Playlists — app data wins
        app_playlists = incoming.get('playlists', [])
        if app_playlists:
            self.playlists = app_playlists

        self.data_version += 1
        self.save()
        # After merging, push cloud data into web models
        self.sync_to_web_models()

    def sync_to_web_models(self):
        """Push cloud data into web-facing WatchList and PlayHistory models."""
        from .models import WatchList, PlayHistory
        if not self.user:
            return

        # --- Watchlist: cloud -> WatchList ---
        cloud_wl = self.watchlist_ids or []
        if cloud_wl:
            existing_wl = {
                (w.tmdb_id, w.media_type): w
                for w in WatchList.objects.filter(user=self.user)
            }
            for item in cloud_wl:
                if not isinstance(item, dict):
                    continue
                mid = item.get('mediaId') or item.get('id')
                mtype = 'tv' if item.get('isTv') else 'movie'
                if not mid:
                    continue
                title = item.get('title', '')
                # The app sends posterUrl in some payloads and posterPath in
                # others — accept both; normalize full URLs to relative paths.
                poster = normalize_poster_path(item.get('posterUrl') or item.get('posterPath') or '')
                row = existing_wl.get((mid, mtype))
                if row is None:
                    row = WatchList.objects.create(
                        user=self.user, tmdb_id=mid, media_type=mtype,
                        title=title, poster_path=poster,
                    )
                    existing_wl[(mid, mtype)] = row
                elif (not row.title or not row.poster_path) and (title or poster):
                    # Backfill metadata on rows that were created before the
                    # app sent it (e.g. web-toggle adds with empty poster).
                    if title:
                        row.title = title
                    if poster:
                        row.poster_path = poster
                    row.save()

        # --- Playback progress -> PlayHistory ---
        cloud_progress = self.playback_progress or {}
        for key, entry in cloud_progress.items():
            if not isinstance(entry, dict):
                continue
            mid = entry.get('mediaId')
            if not mid:
                # Try parsing from key: movie_550, movie_550_-1_-1, tv_123_2_3
                parts = key.split('_')
                if len(parts) >= 2:
                    try:
                        mid = int(parts[1])
                    except (ValueError, TypeError):
                        continue
                else:
                    continue
            is_tv = entry.get('isTv', False)
            if not is_tv and key.startswith('tv_'):
                is_tv = True
            season = entry.get('season', -1)
            episode = entry.get('episode', -1)
            # Parse season/episode from key if not in entry
            if season < 0 or episode < 0:
                parts = key.split('_')
                if len(parts) >= 4:
                    try:
                        season = int(parts[2])
                        episode = int(parts[3])
                    except (ValueError, TypeError):
                        pass
            media_type = 'tv' if is_tv else 'movie'
            pos_ms = entry.get('positionMs', 0)
            dur_ms = entry.get('durationMs', 0)
            defaults = {
                'duration_seconds': pos_ms // 1000,
                'total_duration_seconds': dur_ms // 1000,
                'completed': dur_ms > 0 and (pos_ms / dur_ms) > 0.95,
            }
            # Only set title/poster when the entry actually carries them —
            # otherwise leave the existing metadata alone so a TMDB/watch-history
            # backfill isn't clobbered by an empty value on the next sync.
            title_val = entry.get('title', '') or ''
            poster_val = normalize_poster_path(entry.get('posterPath') or entry.get('posterUrl') or '')
            if title_val:
                defaults['title'] = title_val
            if poster_val:
                defaults['poster_path'] = poster_val
            PlayHistory.objects.update_or_create(
                user=self.user, tmdb_id=mid, media_type=media_type,
                season_number=season if season >= 0 else None,
                episode_number=episode if episode >= 0 else None,
                defaults=defaults,
            )

        # --- Watch history -> PlayHistory metadata (title/poster/season/episode) ---
        # The app's watchHistory entries carry the display metadata (title,
        # posterUrl, last season/episode) that playbackProgress entries lack,
        # so fold them into the matching PlayHistory rows (which otherwise end
        # up with empty titles and no posters on the web play-history page).
        for h in (self.watch_history or []):
            if not isinstance(h, dict):
                continue
            h_mid = h.get('tmdbId') or h.get('mediaId') or h.get('id')
            if not h_mid:
                continue
            h_is_tv = bool(h.get('isTv'))
            h_season = h.get('lastSeasonNumber', -1)
            h_episode = h.get('lastEpisodeNumber', -1)
            try:
                h_season = int(h_season)
            except (TypeError, ValueError):
                h_season = -1
            try:
                h_episode = int(h_episode)
            except (TypeError, ValueError):
                h_episode = -1
            h_season_final = h_season if h_season >= 0 else None
            h_episode_final = h_episode if h_episode >= 0 else None
            defaults = {
                'title': h.get('title', ''),
                'poster_path': normalize_poster_path(h.get('posterUrl') or h.get('posterPath') or ''),
                'episode_title': h.get('lastEpisodeName', ''),
            }
            if h.get('lastWatchedEpoch'):
                try:
                    defaults['last_played_at'] = timezone.datetime.fromtimestamp(
                        int(h['lastWatchedEpoch']) / 1000.0, tz=timezone.UTC)
                except (TypeError, ValueError, OSError, OverflowError):
                    pass
            # Match an existing row first (exact season/episode, then the base
            # movie/show row) so this never duplicates PlayHistory entries.
            h_type = 'tv' if h_is_tv else 'movie'
            row = PlayHistory.objects.filter(
                user=self.user, tmdb_id=h_mid, media_type=h_type,
                season_number=h_season_final, episode_number=h_episode_final,
            ).first()
            if row is None and h_season_final is not None:
                row = PlayHistory.objects.filter(
                    user=self.user, tmdb_id=h_mid, media_type=h_type,
                    season_number=None, episode_number=None,
                ).first()
            if row is None:
                PlayHistory.objects.create(
                    user=self.user, tmdb_id=h_mid, media_type=h_type,
                    season_number=h_season_final, episode_number=h_episode_final,
                    **defaults,
                )
            else:
                for f, v in defaults.items():
                    setattr(row, f, v)
                row.save()

    def sync_from_web_models(self):
        """Pull web WatchList + PlayHistory into cloud data."""
        from .models import WatchList, PlayHistory
        if not self.user:
            return

        # --- WatchList -> cloud watchlist_ids ---
        web_wl = WatchList.objects.filter(user=self.user)
        cloud_existing = {w.get('mediaId'): w for w in (self.watchlist_ids or []) if isinstance(w, dict)}
        for item in web_wl:
            if item.tmdb_id not in cloud_existing:
                cloud_item = {
                    'mediaId': item.tmdb_id,
                    'isTv': item.media_type == 'tv',
                    'title': item.title,
                    'posterPath': item.poster_path or '',
                }
                self.watchlist_ids = (self.watchlist_ids or []) + [cloud_item]
                cloud_existing[item.tmdb_id] = cloud_item

        # --- PlayHistory -> cloud playback_progress ---
        web_history = PlayHistory.objects.filter(user=self.user)
        for h in web_history:
            season = h.season_number if h.season_number is not None else -1
            episode = h.episode_number if h.episode_number is not None else -1
            key = f"{'tv' if h.media_type == 'tv' else 'movie'}_{h.tmdb_id}_{season}_{episode}"
            if key not in self.playback_progress:
                self.playback_progress[key] = {
                    'mediaId': h.tmdb_id,
                    'isTv': h.media_type == 'tv',
                    'season': season,
                    'episode': episode,
                    'positionMs': h.duration_seconds * 1000,
                    'durationMs': h.total_duration_seconds * 1000,
                    'title': h.title,
                    'posterPath': h.poster_path or '',
                    'lastUpdated': int(h.last_played_at.timestamp() * 1000) if h.last_played_at else 0,
                }
            else:
                # Update if web has newer data
                existing = self.playback_progress[key]
                existing_pos = existing.get('positionMs', 0)
                new_pos = h.duration_seconds * 1000
                if new_pos > existing_pos:
                    existing['positionMs'] = new_pos
                    existing['durationMs'] = h.total_duration_seconds * 1000
                    existing['title'] = h.title
                    existing['lastUpdated'] = int(h.last_played_at.timestamp() * 1000) if h.last_played_at else 0

        self.data_version += 1
        self.save()


PURPOSE_CHOICES = [
    ('verification', 'Email Verification'),
    ('password_reset', 'Password Reset'),
    ('notification', 'Notifications'),
    ('newsletter', 'Newsletter'),
    ('marketing', 'Marketing'),
    ('transactional', 'Transactional'),
]


class EmailAddress(models.Model):
    """Multiple SMTP email addresses that admin can manage."""
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=100, blank=True, default='')
    smtp_host = models.CharField(max_length=100, default='smtp.gmail.com')
    smtp_port = models.IntegerField(default=587)
    smtp_username = models.EmailField(blank=True, default='')
    smtp_password = models.CharField(max_length=200)
    smtp_use_tls = models.BooleanField(default=True)
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES, default='notification')
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False, help_text='Default email for its purpose')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', 'purpose', 'email']
        verbose_name = 'Email Address'
        verbose_name_plural = 'Email Addresses'

    def __str__(self):
        return f"{self.display_name or self.email} ({self.get_purpose_display()})"

    def save(self, *args, **kwargs):
        if self.is_default:
            EmailAddress.objects.filter(purpose=self.purpose, is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    def get_backend(self):
        from django.core.mail.backends.smtp import EmailBackend
        return EmailBackend(
            host=self.smtp_host,
            port=self.smtp_port,
            username=self.smtp_username or self.email,
            password=self.smtp_password,
            use_tls=self.smtp_use_tls,
            fail_silently=False,
        )


class EmailTemplate(models.Model):
    """Email templates for different email types."""
    PURPOSE_CHOICES = PURPOSE_CHOICES
    name = models.CharField(max_length=100)
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES)
    subject = models.CharField(max_length=255)
    body = models.TextField(help_text='Use {variable} placeholders: {name}, {email}, {link}, {code}')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['purpose', 'name']
        verbose_name = 'Email Template'
        verbose_name_plural = 'Email Templates'

    def __str__(self):
        return f"{self.name} ({self.get_purpose_display()})"

    def render(self, **kwargs):
        subject = self.subject
        body = self.body
        for key, value in kwargs.items():
            placeholder = '{' + key + '}'
            subject = subject.replace(placeholder, str(value))
            body = body.replace(placeholder, str(value))
        return subject, body


# ---------------------------------------------------------------------------
# User Session / Login Tracker
# ---------------------------------------------------------------------------


class UserSession(models.Model):
    """Track every login with source, IP, device info, and active status."""
    SOURCE_CHOICES = [
        ('web', 'Web'),
        ('android', 'Android'),
        ('ios', 'iOS'),
    ]

    # Cross-DB link (external users DB) — no DB-level constraint.
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sessions', db_constraint=False)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='web')
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.TextField(blank=True, default='')
    device_model = models.CharField(max_length=200, blank=True, default='')
    os_version = models.CharField(max_length=100, blank=True, default='')
    app_version = models.CharField(max_length=50, blank=True, default='')
    is_active = models.BooleanField(default=True)
    logged_in_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    logged_out_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-logged_in_at']
        indexes = [
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['user', 'source']),
        ]

    def __str__(self):
        status = 'Active' if self.is_active else 'Offline'
        return f"{self.user.username} — {self.source} ({status})"

    def mark_logout(self):
        from django.utils import timezone
        self.is_active = False
        self.logged_out_at = timezone.now()
        self.save(update_fields=['is_active', 'logged_out_at'])

    @classmethod
    def active_count(cls, user):
        return cls.objects.filter(user=user, is_active=True).count()

    @classmethod
    def total_logins(cls, user):
        return cls.objects.filter(user=user).count()

    @classmethod
    def last_source(cls, user):
        last = cls.objects.filter(user=user).order_by('-logged_in_at').first()
        return last.source if last else ''


class UserPageView(models.Model):
    """Track individual page views with time spent per user."""
    PLATFORM_CHOICES = [
        ('web', 'Web Browser'),
        ('android', 'Android App'),
        ('ios', 'iOS App'),
        ('unknown', 'Unknown'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='page_views', null=True, blank=True)
    visitor_id = models.CharField(max_length=100, blank=True, default='', db_index=True)
    path = models.CharField(max_length=500, db_index=True)
    page_title = models.CharField(max_length=255, blank=True, default='')
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, default='web')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    referrer = models.CharField(max_length=500, blank=True, default='')
    time_spent_seconds = models.IntegerField(default=0, help_text='Seconds spent on this page')
    scroll_depth = models.FloatField(default=0, help_text='Max scroll depth percentage (0-100)')
    is_active = models.BooleanField(default=True, help_text='Currently viewing')
    viewed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-viewed_at']
        indexes = [
            models.Index(fields=['user', 'viewed_at']),
            models.Index(fields=['path', 'viewed_at']),
            models.Index(fields=['platform', 'viewed_at']),
        ]
        verbose_name = 'Page View'
        verbose_name_plural = 'Page Views'

    def __str__(self):
        user_str = self.user.username if self.user else self.visitor_id[:8]
        return f"{user_str} → {self.path} ({self.time_spent_seconds}s)"

    @classmethod
    def today_views(cls, user=None, platform=None):
        from django.utils import timezone
        today = timezone.now().date()
        qs = cls.objects.filter(viewed_at__date=today)
        if user:
            qs = qs.filter(user=user)
        if platform:
            qs = qs.filter(platform=platform)
        return qs

    @classmethod
    def total_time_today(cls, user=None):
        from django.utils import timezone
        today = timezone.now().date()
        from django.db.models import Sum
        qs = cls.objects.filter(viewed_at__date=today)
        if user:
            qs = qs.filter(user=user)
        result = qs.aggregate(total=Sum('time_spent_seconds'))
        return result['total'] or 0

    @classmethod
    def top_pages(cls, days=7, limit=20):
        from django.utils import timezone
        from django.db.models import Sum, Count
        since = timezone.now() - timezone.timedelta(days=days)
        return (cls.objects.filter(viewed_at__gte=since)
                .values('path', 'page_title')
                .annotate(total_views=Count('id'), total_time=Sum('time_spent_seconds'))
                .order_by('-total_views')[:limit])

    @classmethod
    def platform_stats(cls, days=7):
        from django.utils import timezone
        from django.db.models import Sum, Count
        since = timezone.now() - timezone.timedelta(days=days)
        return (cls.objects.filter(viewed_at__gte=since)
                .values('platform')
                .annotate(total_views=Count('id'), total_time=Sum('time_spent_seconds'))
                .order_by('-total_views'))


class DBRoutingConfig(models.Model):
    """Controls whether user data goes to local or external database."""
    use_external_db = models.BooleanField(
        default=False,
        help_text='Route user data (users, watchlist, play history, etc.) to the external database configured in DB Connections'
    )
    auto_migrate_on_switch = models.BooleanField(
        default=True,
        help_text='Automatically migrate existing user data when switching databases'
    )
    last_migrated_at = models.DateTimeField(null=True, blank=True)
    last_migration_status = models.CharField(max_length=20, choices=[
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('pending', 'Pending'),
    ], default='pending')
    last_migration_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'DB Routing Config'
        verbose_name_plural = 'DB Routing Config'

    def __str__(self):
        db = 'External' if self.use_external_db else 'Local'
        return f"User data storage: {db}"

    @property
    def external_db_ready(self):
        """Check if there's an active+default DBConnectionConfig ready."""
        return DBConnectionConfig.objects.filter(is_active=True, is_default=True).exists()

    @classmethod
    def get_config(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class DBConnectionConfig(models.Model):
    """External database connection configuration managed by admin.
    The actual connection file is written to server_config.py (VM-local only)."""
    DB_TYPE_CHOICES = [
        ('mysql', 'MySQL / MariaDB'),
        ('oracle', 'Oracle'),
        ('postgresql', 'PostgreSQL'),
        ('mssql', 'MS SQL Server'),
        ('sqlite', 'SQLite'),
    ]

    name = models.CharField(max_length=100, help_text='Friendly name for this connection')
    db_type = models.CharField(max_length=20, choices=DB_TYPE_CHOICES, default='mysql')
    host = models.CharField(max_length=255, default='127.0.0.1', help_text='IP address or hostname (use 127.0.0.1 for VM-local)')
    port = models.IntegerField(default=3306, help_text='Database port')
    database_name = models.CharField(max_length=255, default='', help_text='Database name')
    username = models.CharField(max_length=255, default='', help_text='Database username')
    password = models.CharField(max_length=255, default='', help_text='Database password')
    extra_params = models.JSONField(default=dict, blank=True, help_text='Extra connection params as JSON, e.g. {"charset": "utf8mb4"}')
    is_active = models.BooleanField(default=True, help_text='Only one connection can be active at a time')
    is_default = models.BooleanField(default=False, help_text='Use as the default external DB connection')
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_status = models.CharField(max_length=20, choices=[
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('untested', 'Untested'),
    ], default='untested')
    last_test_message = models.TextField(blank=True, default='')
    notes = models.TextField(blank=True, default='', help_text='Admin notes')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', '-is_active', 'name']
        verbose_name = 'DB Connection Config'
        verbose_name_plural = 'DB Connection Configs'

    def __str__(self):
        return f"{self.name} ({self.get_db_type_display()} @ {self.host}:{self.port})"

    def get_engine_string(self):
        engines = {
            'mysql': 'django.db.backends.mysql',
            'oracle': 'django.db.backends.oracle',
            'postgresql': 'django.db.backends.postgresql',
            'mssql': 'django.db.backends.mssql',
            'sqlite': 'django.db.backends.sqlite3',
        }
        return engines.get(self.db_type, 'django.db.backends.mysql')

    def test_connection(self):
        """Test the database connection and return (success, message)."""
        from django.utils import timezone
        try:
            if self.db_type == 'mysql':
                import MySQLdb
                conn = MySQLdb.connect(
                    host=self.host,
                    port=self.port,
                    user=self.username,
                    passwd=self.password,
                    db=self.database_name or None,
                    connect_timeout=10,
                    **self.extra_params,
                )
                cursor = conn.cursor()
                cursor.execute('SELECT 1')
                result = cursor.fetchone()
                cursor.close()
                conn.close()
                self.last_tested_at = timezone.now()
                self.last_test_status = 'success'
                self.last_test_message = f'Connected successfully. Query returned: {result[0]}'
                if self.pk:
                    self.save(update_fields=['last_tested_at', 'last_test_status', 'last_test_message'])
                return True, self.last_test_message

            elif self.db_type == 'oracle':
                import oracledb
                dsn = oracledb.makedsn(self.host, self.port, service_name=self.database_name)
                conn = oracledb.connect(user=self.username, password=self.password, dsn=dsn)
                cursor = conn.cursor()
                cursor.execute('SELECT 1 FROM DUAL')
                result = cursor.fetchone()
                cursor.close()
                conn.close()
                self.last_tested_at = timezone.now()
                self.last_test_status = 'success'
                self.last_test_message = f'Connected successfully. Query returned: {result[0]}'
                if self.pk:
                    self.save(update_fields=['last_tested_at', 'last_test_status', 'last_test_message'])
                return True, self.last_test_message

            elif self.db_type == 'postgresql':
                import psycopg2
                conn = psycopg2.connect(
                    host=self.host,
                    port=self.port,
                    user=self.username,
                    password=self.password,
                    dbname=self.database_name or 'postgres',
                    connect_timeout=10,
                )
                conn.close()
                self.last_tested_at = timezone.now()
                self.last_test_status = 'success'
                self.last_test_message = 'Connected successfully.'
                if self.pk:
                    self.save(update_fields=['last_tested_at', 'last_test_status', 'last_test_message'])
                return True, self.last_test_message

            elif self.db_type == 'sqlite':
                import sqlite3
                conn = sqlite3.connect(self.database_name or ':memory:', timeout=10)
                cursor = conn.cursor()
                cursor.execute('SELECT 1')
                result = cursor.fetchone()
                cursor.close()
                conn.close()
                self.last_tested_at = timezone.now()
                self.last_test_status = 'success'
                self.last_test_message = f'Connected successfully. Query returned: {result[0]}'
                if self.pk:
                    self.save(update_fields=['last_tested_at', 'last_test_status', 'last_test_message'])
                return True, self.last_test_message

            else:
                msg = f'Unsupported database type: {self.db_type}'
                self.last_tested_at = timezone.now()
                self.last_test_status = 'failed'
                self.last_test_message = msg
                if self.pk:
                    self.save(update_fields=['last_tested_at', 'last_test_status', 'last_test_message'])
                return False, msg

        except ImportError as e:
            msg = f'Missing driver package: {e}'
            self.last_tested_at = timezone.now()
            self.last_test_status = 'failed'
            self.last_test_message = msg
            if self.pk:
                self.save(update_fields=['last_tested_at', 'last_test_status', 'last_test_message'])
            return False, msg
        except Exception as e:
            msg = f'Connection failed: {e}'
            self.last_tested_at = timezone.now()
            self.last_test_status = 'failed'
            self.last_test_message = msg
            if self.pk:
                self.save(update_fields=['last_tested_at', 'last_test_status', 'last_test_message'])
            return False, msg

    def write_server_config(self):
        """Write connection info to a server_config.py file (VM-local only).
        This file is only accessible from the server itself."""
        import os, json
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'server_config.py')
        config_data = {
            'ENGINE': self.get_engine_string(),
            'NAME': self.database_name,
            'USER': self.username,
            'PASSWORD': self.password,
            'HOST': self.host,
            'PORT': str(self.port),
        }
        if self.extra_params:
            config_data.update(self.extra_params)
        content = (
            '# Auto-generated by NewMovies Admin. DO NOT EDIT MANUALLY.\n'
            '# This file is only accessible from the VM itself.\n'
            '# Last updated: ' + str(self.updated_at) + '\n\n'
            'DB_CONFIG = ' + json.dumps(config_data, indent=4) + '\n'
        )
        with open(config_path, 'w') as f:
            f.write(content)
        os.chmod(config_path, 0o600)  # Owner read/write only
        return config_path

    def save(self, *args, **kwargs):
        if self.is_default:
            DBConnectionConfig.objects.filter(is_default=True).exclude(pk=self.pk).update(is_default=False)
        if self.is_active:
            DBConnectionConfig.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)
        # Write server_config.py whenever saved
        if self.is_active and self.is_default:
            try:
                self.write_server_config()
            except Exception:
                pass


class EmailSendLog(models.Model):
    """Track every email sent from the platform."""
    # Cross-DB link (external users DB vs default config) — no DB-level constraint.
    address = models.ForeignKey('EmailAddress', on_delete=models.SET_NULL, null=True, blank=True, help_text='SMTP address used', db_constraint=False)
    recipient = models.EmailField(help_text='Recipient email address')
    subject = models.CharField(max_length=500)
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES, default='notification')
    status = models.CharField(max_length=20, choices=[
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ])
    error_message = models.TextField(blank=True, default='')
    # Cross-DB link (external users DB vs default auth) — no DB-level constraint.
    sent_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, help_text='Admin who triggered it', db_constraint=False)
    source = models.CharField(max_length=20, choices=[
        ('test_mail', 'Test Mail'),
        ('verification', 'Email Verification'),
        ('password_reset', 'Password Reset'),
        ('notification', 'Notification'),
        ('api', 'API'),
    ], default='test_mail')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Email Send Log'
        verbose_name_plural = 'Email Send Logs'

    def __str__(self):
        return f"{self.status}: {self.subject} → {self.recipient} ({self.created_at})"
