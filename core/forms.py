from django import forms
from .models import SiteSettings, ContentRow


class SiteSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = [
            'data_source',
            'items_per_row',
            'card_size',
            'title_size',
            'text_size',
            'theme_style',
            'font_family',
            'enable_sidebar_ads',
            'sidebar_ads_code',
            'brand_name',
            'brand_tagline',
            'brand_color',
            'enable_url_blocking',
            'blocked_urls',
            'redirect_url',
            'email_host',
            'email_port',
            'email_host_user',
            'email_host_password',
            'email_use_tls',
            'curated_top_movie_ids',
            'curated_top_series_ids'
        ]
        widgets = {
            'sidebar_ads_code': forms.Textarea(attrs={'rows': 10, 'cols': 80}),
            'blocked_urls': forms.Textarea(attrs={'rows': 6, 'cols': 80}),
            'email_host_password': forms.PasswordInput(render_value=True),
            'curated_top_movie_ids': forms.Textarea(attrs={'rows': 3, 'cols': 80, 'placeholder': 'Comma-separated TMDB IDs, e.g., 123,456,789,987,654'}),
            'curated_top_series_ids': forms.Textarea(attrs={'rows': 3, 'cols': 80, 'placeholder': 'Comma-separated TMDB IDs, e.g., 123,456,789,987,654'}),
        }


class ContentRowForm(forms.ModelForm):
    class Meta:
        model = ContentRow
        fields = [
            'title',
            'media_type',
            'row_type',
            'genre_tmdb_id',
            'region',
            'language',
            'sort_by',
            'filter_params',
            'items_per_page',
            'is_active',
            'auto_scroll',
            'order'
        ]
        widgets = {
            'filter_params': forms.Textarea(attrs={'rows': 4, 'cols': 80})
        }
