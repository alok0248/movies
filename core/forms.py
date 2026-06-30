from django import forms
from .models import SiteSettings, ContentRow, PlayerConfiguration


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
            'curated_top_series_ids',
            'active_movie_player',
            'active_tv_player',
            'watch_region'
        ]
        widgets = {
            'sidebar_ads_code': forms.Textarea(attrs={'rows': 10, 'cols': 80, 'class': 'form-control', 'title': 'Enter HTML/JS code for sidebar ads to display on the site'}),
            'blocked_urls': forms.Textarea(attrs={'rows': 6, 'cols': 80, 'class': 'form-control', 'title': 'List of URLs to block (one per line), or "all" to block everything except admin pages'}),
            'email_host_password': forms.PasswordInput(render_value=True, attrs={'class': 'form-control', 'title': 'Enter your email provider\'s SMTP password or app-specific password'}),
            'curated_top_movie_ids': forms.Textarea(attrs={'rows': 3, 'cols': 80, 'placeholder': 'Comma-separated TMDB IDs, e.g., 123,456,789,987,654', 'class': 'form-control', 'title': 'Manually select which movies appear at the top of the homepage (comma-separated TMDB IDs)'}),
            'curated_top_series_ids': forms.Textarea(attrs={'rows': 3, 'cols': 80, 'placeholder': 'Comma-separated TMDB IDs, e.g., 123,456,789,987,654', 'class': 'form-control', 'title': 'Manually select which TV shows appear at the top of the homepage (comma-separated TMDB IDs)'}),
            'brand_color': forms.TextInput(attrs={'type': 'color', 'class': 'form-control form-control-color', 'title': 'Choose the primary brand color for your site (buttons, links, etc.)'}),
            'brand_name': forms.TextInput(attrs={'class': 'form-control', 'title': 'Enter your site\'s brand name (appears in the navbar, etc.)'}),
            'brand_tagline': forms.TextInput(attrs={'class': 'form-control', 'title': 'Enter a short tagline for your site'}),
            'data_source': forms.Select(attrs={'class': 'form-select', 'title': 'Select where your movie/TV data comes from (TMDB API, local database, etc.)'}),
            'items_per_row': forms.Select(attrs={'class': 'form-select', 'title': 'Select how many movie/TV cards to display in each row on the homepage'}),
            'card_size': forms.Select(attrs={'class': 'form-select', 'title': 'Select the size of movie/TV cards on the homepage'}),
            'title_size': forms.Select(attrs={'class': 'form-select', 'title': 'Select the size of section titles on the homepage'}),
            'text_size': forms.Select(attrs={'class': 'form-select', 'title': 'Select the size of general text on the site'}),
            'theme_style': forms.Select(attrs={'class': 'form-select', 'title': 'Select a pre-defined theme style for your site'}),
            'font_family': forms.Select(attrs={'class': 'form-select', 'title': 'Select the font family to use across the site'}),
            'enable_sidebar_ads': forms.CheckboxInput(attrs={'class': 'form-check-input', 'title': 'Check this box to enable sidebar ads on the site'}),
            'enable_url_blocking': forms.CheckboxInput(attrs={'class': 'form-check-input', 'title': 'Check this box to enable URL blocking for non-admin pages'}),
            'redirect_url': forms.TextInput(attrs={'class': 'form-control', 'title': 'Enter the URL to redirect blocked requests to'}),
            'email_host': forms.TextInput(attrs={'class': 'form-control', 'title': 'Enter your email provider\'s SMTP server (e.g., smtp.gmail.com)'}),
            'email_port': forms.NumberInput(attrs={'class': 'form-control', 'title': 'Enter your email provider\'s SMTP port (e.g., 587 for Gmail TLS)'}),
            'email_host_user': forms.EmailInput(attrs={'class': 'form-control', 'title': 'Enter the email address to send from (e.g., yourname@gmail.com)'}),
            'email_use_tls': forms.CheckboxInput(attrs={'class': 'form-check-input', 'title': 'Check this box to use TLS encryption for SMTP (recommended)'}),
            'active_movie_player': forms.Select(attrs={'class': 'form-select', 'title': 'Select the default player to use for movies'}),
            'active_tv_player': forms.Select(attrs={'class': 'form-select', 'title': 'Select the default player to use for TV shows'}),
            'watch_region': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., US, GB, IN', 'title': 'Enter your region for watch provider information'}),
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


class PlayerConfigurationForm(forms.ModelForm):
    class Meta:
        model = PlayerConfiguration
        fields = [
            'name',
            'media_type',
            'is_active',
            'order',
            'player_color',
            'auto_play',
            'next_episode',
            'episode_selector',
            'player_width',
            'player_height',
            'frameborder',
            'allowfullscreen',
            'custom_iframe_url'
        ]
        widgets = {
            'player_color': forms.TextInput(attrs={'placeholder': 'e.g., e50914 (no #)'}),
            'player_width': forms.TextInput(attrs={'placeholder': 'e.g., 100%, 800px'}),
            'player_height': forms.TextInput(attrs={'placeholder': 'e.g., 600px, 100%'}),
            'custom_iframe_url': forms.Textarea(attrs={
                'placeholder': 'Custom iframe URL (e.g., https://example.com/embed/{tmdb_id}?s={season}&e={episode})',
                'rows': 3,
                'style': 'width: 100%;'
            }),
        }
