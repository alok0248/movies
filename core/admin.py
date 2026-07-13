from django.contrib import admin
from .models import SiteSettings, ContentRow, WatchList, TMDBMovie, TMDBTV, TMDBGenre, NavbarItem, AndroidApp, AndroidAppAccessLog, AndroidAppBuildLog, AndroidAppFailedAttempt, AndroidAppDevice, AndroidAppDailyUniqueVisitor, AndroidAppDeviceVisit


@admin.register(AndroidApp)
class AndroidAppAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_active', 'total_connections', 'last_accessed_at', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name', 'slug', 'access_username')
    readonly_fields = ('total_connections', 'last_accessed_at', 'created_at', 'updated_at')


@admin.register(AndroidAppAccessLog)
class AndroidAppAccessLogAdmin(admin.ModelAdmin):
    list_display = ('android_app', 'access_date', 'connection_count', 'last_accessed_at')
    list_filter = ('access_date', 'android_app')
    search_fields = ('android_app__name',)
    readonly_fields = ('created_at',)


@admin.register(AndroidAppBuildLog)
class AndroidAppBuildLogAdmin(admin.ModelAdmin):
    list_display = ('android_app', 'build_identifier', 'access_date', 'connection_count', 'last_accessed_at')
    list_filter = ('access_date', 'android_app')
    search_fields = ('android_app__name', 'build_identifier')
    readonly_fields = ('created_at',)


@admin.register(AndroidAppFailedAttempt)
class AndroidAppFailedAttemptAdmin(admin.ModelAdmin):
    list_display = ('android_app', 'app_slug', 'failure_reason', 'ip_address', 'attempted_at')
    list_filter = ('failure_reason', 'attempted_at', 'android_app')
    search_fields = ('app_slug', 'ip_address', 'request_identity', 'build_identifier')
    readonly_fields = ('attempted_at',)


@admin.register(AndroidAppDevice)
class AndroidAppDeviceAdmin(admin.ModelAdmin):
    list_display = ('android_app', 'user_id', 'device_model', 'os_version', 'total_visits', 'last_seen_at', 'first_seen_at')
    list_filter = ('android_app', 'last_seen_at', 'os_version')
    search_fields = ('android_app__name', 'user_id', 'device_model')
    readonly_fields = ('first_seen_at', 'last_seen_at')


@admin.register(AndroidAppDailyUniqueVisitor)
class AndroidAppDailyUniqueVisitorAdmin(admin.ModelAdmin):
    list_display = ('android_app', 'access_date', 'unique_visitor_count')
    list_filter = ('android_app', 'access_date')
    search_fields = ('android_app__name',)
    readonly_fields = ('created_at',)


@admin.register(AndroidAppDeviceVisit)
class AndroidAppDeviceVisitAdmin(admin.ModelAdmin):
    list_display = ('device', 'android_app', 'visited_at', 'build_identifier', 'device_model', 'os_version', 'ip_address')
    list_filter = ('android_app', 'visited_at', 'os_version')
    search_fields = ('device__user_id', 'android_app__name', 'build_identifier', 'device_model', 'ip_address')
    readonly_fields = ('visited_at',)


@admin.register(ContentRow)
class ContentRowAdmin(admin.ModelAdmin):
    list_display = ('title', 'media_type', 'row_type', 'is_active', 'auto_scroll', 'order')
    list_filter = ('media_type', 'row_type', 'is_active', 'auto_scroll')
    list_editable = ('order', 'is_active', 'auto_scroll')
    search_fields = ('title',)
    ordering = ('order',)
    fieldsets = (
        (None, {
            'fields': ('title', 'media_type', 'row_type', 'is_active', 'auto_scroll', 'order')
        }),
        ('Filters (TMDB)', {
            'fields': ('genre_tmdb_id', 'region', 'language', 'sort_by', 'filter_params'),
        }),
        ('Advanced', {
            'fields': ('items_per_page',),
        }),
    )


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ('data_source', 'brand_name', 'brand_color', 'enable_url_blocking')
    fieldsets = (
        (None, {
            'fields': ('data_source',)
        }),
        ('Brand Settings', {
            'fields': ('brand_name', 'brand_tagline', 'brand_color'),
        }),
        ('UI Settings', {
            'fields': ('items_per_row', 'card_size', 'title_size', 'text_size', 'theme_style', 'font_family', 'enable_sidebar_ads', 'sidebar_ads_code'),
        }),
        ('URL Blocking', {
            'fields': ('enable_url_blocking', 'blocked_urls', 'redirect_url'),
        }),
        ('Email Configuration', {
            'fields': ('email_host', 'email_port', 'email_host_user', 'email_host_password', 'email_use_tls'),
        }),
        ('TMDB Settings', {
            'fields': ('watch_region',),
        }),
    )


@admin.register(WatchList)
class WatchListAdmin(admin.ModelAdmin):
    list_display = ('user', 'title', 'media_type', 'tmdb_id', 'added_at')
    list_filter = ('user', 'media_type', 'added_at')
    search_fields = ('user__username', 'title', 'tmdb_id')


@admin.register(TMDBGenre)
class TMDBGenreAdmin(admin.ModelAdmin):
    list_display = ('name', 'media_type', 'id')
    list_filter = ('media_type',)
    search_fields = ('name', 'id')


@admin.register(TMDBMovie)
class TMDBMovieAdmin(admin.ModelAdmin):
    list_display = ('title', 'release_date', 'popularity', 'vote_average', 'vote_count')
    list_filter = ('adult', 'status', 'release_date')
    search_fields = ('title', 'original_title', 'id', 'imdb_id')
    readonly_fields = ('last_fetched',)


@admin.register(TMDBTV)
class TMDBTVAdmin(admin.ModelAdmin):
    list_display = ('name', 'first_air_date', 'popularity', 'vote_average', 'vote_count')
    list_filter = ('adult', 'status', 'in_production', 'first_air_date')
    search_fields = ('name', 'original_name', 'id')
    readonly_fields = ('last_fetched',)


@admin.register(NavbarItem)
class NavbarItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'item_type', 'built_in_id', 'is_active', 'order')
    list_filter = ('item_type', 'is_active')
    list_editable = ('order', 'is_active')
    search_fields = ('name', 'built_in_id', 'url')
    ordering = ('order',)
