from django.contrib import admin
from .models import SiteSettings, ContentRow, WatchList


@admin.register(ContentRow)
class ContentRowAdmin(admin.ModelAdmin):
    list_display = ('title', 'media_type', 'row_type', 'is_active', 'order')
    list_filter = ('media_type', 'row_type', 'is_active')
    list_editable = ('order', 'is_active')
    search_fields = ('title',)
    ordering = ('order',)
    fieldsets = (
        (None, {
            'fields': ('title', 'media_type', 'row_type', 'is_active', 'order')
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
            'fields': ('items_per_row', 'card_size', 'title_size', 'text_size', 'enable_sidebar_ads', 'sidebar_ads_code'),
        }),
        ('URL Blocking', {
            'fields': ('enable_url_blocking', 'blocked_urls', 'redirect_url'),
        }),
        ('Email Configuration', {
            'fields': ('email_host', 'email_port', 'email_host_user', 'email_host_password', 'email_use_tls'),
        }),
    )


@admin.register(WatchList)
class WatchListAdmin(admin.ModelAdmin):
    list_display = ('user', 'title', 'media_type', 'tmdb_id', 'added_at')
    list_filter = ('user', 'media_type', 'added_at')
    search_fields = ('user__username', 'title', 'tmdb_id')
