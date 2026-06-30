from django.contrib import admin
from .models import SiteSettings, ContentRow, WatchList, TMDBMovie, TMDBTV, TMDBGenre, NavbarItem


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
