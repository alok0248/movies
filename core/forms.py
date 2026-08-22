from django import forms
from .models import SiteSettings, ContentRow, PlayerConfiguration, TMDBApiKey, NavbarItem, ProviderItem, WatchRegion, AndroidApp, Ad
import json


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
            'watch_region',
            'url_format',
            'tmdb_db_host',
            'tmdb_db_port',
            'tmdb_db_name',
            'tmdb_db_user',
            'tmdb_db_password',
            'hide_live_tv',
            'bot_ips',
            'bot_user_agents',
            'adsense_verification_meta',
            'adsense_client_id',
            'auto_click_every_clicks',
            'default_clicks_required',
            'require_ad_consent',
            'ad_consent_message',
            'max_ad_load_retries',
            'idm_visibility',
            'tile_ad_every_n',
            'enable_tile_click_gating',
            'tile_ad_source',
            'tile_gating_source',
        ]
        widgets = {
            'blocked_urls': forms.Textarea(attrs={'rows': 6, 'cols': 80, 'class': 'form-control', 'title': 'List of URLs to block (one per line), or "all" to block everything except admin pages'}),
            'bot_ips': forms.Textarea(attrs={'rows': 4, 'cols': 80, 'class': 'form-control', 'placeholder': '192.168.1.10, 10.0.0.5', 'title': 'Comma-separated or line-separated bot IP addresses to classify as bot traffic'}),
            'bot_user_agents': forms.Textarea(attrs={'rows': 4, 'cols': 80, 'class': 'form-control', 'placeholder': 'Googlebot, MyCustomCrawler', 'title': 'Comma-separated or line-separated user-agent fragments to classify as bot traffic'}),
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
            'enable_url_blocking': forms.CheckboxInput(attrs={'class': 'form-check-input', 'title': 'Check this box to enable URL blocking for non-admin pages'}),
            'redirect_url': forms.TextInput(attrs={'class': 'form-control', 'title': 'Enter the URL to redirect blocked requests to'}),
            'email_host': forms.TextInput(attrs={'class': 'form-control', 'title': 'Enter your email provider\'s SMTP server (e.g., smtp.gmail.com)'}),
            'email_port': forms.NumberInput(attrs={'class': 'form-control', 'title': 'Enter your email provider\'s SMDB port (e.g., 587 for Gmail TLS)'}),
            'email_host_user': forms.EmailInput(attrs={'class': 'form-control', 'title': 'Enter the email address to send from (e.g., yourname@gmail.com)'}),
            'email_use_tls': forms.CheckboxInput(attrs={'class': 'form-check-input', 'title': 'Check this box to use TLS encryption for SMTP (recommended)'}),
            'active_movie_player': forms.Select(attrs={'class': 'form-select', 'title': 'Select the default player to use for movies'}),
            'active_tv_player': forms.Select(attrs={'class': 'form-select', 'title': 'Select the default player to use for TV shows'}),
            'watch_region': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., US, GB, IN', 'title': 'Enter your region for watch provider information'}),
            'url_format': forms.Select(attrs={'class': 'form-select', 'title': 'Choose whether to use title slugs or TMDB IDs in movie/series detail page URLs'}),
            'tmdb_db_host': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'localhost'}),
            'tmdb_db_port': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '5432'}),
            'tmdb_db_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'tmdb'}),
            'tmdb_db_user': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'tmdb'}),
            'tmdb_db_password': forms.PasswordInput(render_value=True, attrs={'class': 'form-control', 'placeholder': 'tmdb123!'}),
            'hide_live_tv': forms.CheckboxInput(attrs={'class': 'form-check-input', 'title': 'Check this box to hide Live TV from navigation'}),
            'adsense_verification_meta': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'googlea1b2c3d4e5f6a7b8', 'title': 'Enter the Google AdSense verification meta tag content (the value of the "content" attribute)'}),
            'adsense_client_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'ca-pub-1234567890123456', 'title': 'Enter your Google AdSense client ID'}),
            'auto_click_every_clicks': forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'step': '1', 'placeholder': 'e.g., 10', 'title': 'Number of user clicks between ad auto-opens. After N clicks the NEXT click triggers the ad. Set 1 for every click.'}),
            'default_clicks_required': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1', 'placeholder': 'e.g., 2', 'title': 'Default click threshold that must be reached before ANY ad content loads on page. Applied to ads whose clicks_required=0. Set 0 to only use per-Ad values.'}),
            'require_ad_consent': forms.CheckboxInput(attrs={'class': 'form-check-input', 'title': 'Require explicit user consent (GDPR/CCPA) before loading any ad assets or scripts. Shows consent banner until user responds.'}),
            'ad_consent_message': forms.Textarea(attrs={'rows': 3, 'cols': 80, 'class': 'form-control', 'title': 'Copy shown in the consent banner when require_ad_consent is enabled.'}),
            'max_ad_load_retries': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1', 'placeholder': 'e.g., 3', 'title': 'Maximum automatic retries for failed ad network script/asset loads before the slot is marked failed.'}),
            'idm_visibility': forms.Select(attrs={'class': 'form-select', 'title': 'Choose who can see and use the IDM Auto-Extract feature on movie and TV detail pages.'}),
            'tile_ad_every_n': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1', 'placeholder': 'e.g., 10', 'title': 'Insert an in-grid ad tile every N movie/series cards. Set 0 to disable. Example: 10 means one ad tile after every 10 cards.'}),
            'enable_tile_click_gating': forms.CheckboxInput(attrs={'class': 'form-check-input', 'title': 'If enabled, the first click on any movie/series card opens an ad in a new tab; the second click on the same card navigates to the detail page.'}),
            'tile_ad_source': forms.Select(attrs={'class': 'form-select', 'title': 'Choose where the in-grid ad tile content comes from — individual Tile-position Ad records, or the Amazon Affiliate Product list.'}),
            'tile_gating_source': forms.Select(attrs={'class': 'form-select', 'title': 'Choose where the first-click-gating ad URL comes from — the global autoclick pool, or the Amazon Affiliate Product list.'}),
        }


class BrandingSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['brand_name', 'brand_tagline', 'brand_color']
        widgets = {
            'brand_color': forms.TextInput(attrs={'type': 'color', 'class': 'form-control form-control-color'}),
            'brand_name': forms.TextInput(attrs={'class': 'form-control'}),
            'brand_tagline': forms.TextInput(attrs={'class': 'form-control'}),
        }


class FooterSettingsForm(forms.ModelForm):
    footer_title = forms.CharField(
        label='Footer Title',
        help_text='Primary footer heading.',
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    footer_description = forms.CharField(
        label='Footer Description',
        help_text='Short supporting footer text.',
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    footer_bottom_text = forms.CharField(
        label='Footer Bottom Text',
        help_text='Bottom-right supporting line.',
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )

    class Meta:
        model = SiteSettings
        fields = [
            'footer_enabled',
            'footer_title', 'footer_description', 'footer_bottom_text',
            'footer_links_title', 'footer_links',
            'footer_genres_title', 'footer_genres',
            'footer_countries_title', 'footer_countries',
            'footer_subscribe_title', 'footer_subscribe_text', 'footer_subscribe_placeholder', 'footer_subscribe_button_text',
            'footer_logo_text', 'footer_logo_tagline', 'footer_copyright_text', 'footer_disclaimer_text'
        ]
        widgets = {
            'footer_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'footer_links_title': forms.TextInput(attrs={'class': 'form-control'}),
            'footer_links': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'footer_genres_title': forms.TextInput(attrs={'class': 'form-control'}),
            'footer_genres': forms.Textarea(attrs={'class': 'form-control', 'rows': 5}),
            'footer_countries_title': forms.TextInput(attrs={'class': 'form-control'}),
            'footer_countries': forms.Textarea(attrs={'class': 'form-control', 'rows': 5}),
            'footer_subscribe_title': forms.TextInput(attrs={'class': 'form-control'}),
            'footer_subscribe_text': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'footer_subscribe_placeholder': forms.TextInput(attrs={'class': 'form-control'}),
            'footer_subscribe_button_text': forms.TextInput(attrs={'class': 'form-control'}),
            'footer_logo_text': forms.TextInput(attrs={'class': 'form-control'}),
            'footer_logo_tagline': forms.TextInput(attrs={'class': 'form-control'}),
            'footer_copyright_text': forms.TextInput(attrs={'class': 'form-control'}),
            'footer_disclaimer_text': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }


class DisplaySettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['items_per_row', 'card_size', 'title_size', 'text_size', 'theme_style', 'font_family']
        widgets = {
            'items_per_row': forms.Select(attrs={'class': 'form-select'}),
            'card_size': forms.Select(attrs={'class': 'form-select'}),
            'title_size': forms.Select(attrs={'class': 'form-select'}),
            'text_size': forms.Select(attrs={'class': 'form-select'}),
            'theme_style': forms.Select(attrs={'class': 'form-select'}),
            'font_family': forms.Select(attrs={'class': 'form-select'}),
        }


class DataSourceSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['data_source', 'watch_region', 'curated_top_movie_ids', 'curated_top_series_ids', 'hide_live_tv']
        widgets = {
            'data_source': forms.Select(attrs={'class': 'form-select'}),
            'watch_region': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., US, GB, IN'}),
            'curated_top_movie_ids': forms.Textarea(attrs={'rows': 3, 'class': 'form-control', 'placeholder': 'Comma-separated TMDB IDs'}),
            'curated_top_series_ids': forms.Textarea(attrs={'rows': 3, 'class': 'form-control', 'placeholder': 'Comma-separated TMDB IDs'}),
            'hide_live_tv': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class TMDBDBSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['tmdb_db_host', 'tmdb_db_port', 'tmdb_db_name', 'tmdb_db_user', 'tmdb_db_password', 'tmdb_db_enable_api_fallback']
        widgets = {
            'tmdb_db_host': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'localhost'}),
            'tmdb_db_port': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '5432'}),
            'tmdb_db_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'tmdb'}),
            'tmdb_db_user': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'tmdb'}),
            'tmdb_db_password': forms.PasswordInput(render_value=True, attrs={'class': 'form-control', 'placeholder': 'tmdb123!'}),
            'tmdb_db_enable_api_fallback': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class PlayerSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['active_movie_player', 'active_tv_player']
        widgets = {
            'active_movie_player': forms.Select(attrs={'class': 'form-select'}),
            'active_tv_player': forms.Select(attrs={'class': 'form-select'}),
        }


class URLBlockingSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['enable_url_blocking', 'blocked_urls', 'redirect_url']
        widgets = {
            'enable_url_blocking': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'blocked_urls': forms.Textarea(attrs={'rows': 6, 'class': 'form-control'}),
            'redirect_url': forms.TextInput(attrs={'class': 'form-control'}),
        }


class EmailSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['email_host', 'email_port', 'email_host_user', 'email_host_password', 'email_use_tls']
        widgets = {
            'email_host': forms.TextInput(attrs={'class': 'form-control'}),
            'email_port': forms.NumberInput(attrs={'class': 'form-control'}),
            'email_host_user': forms.EmailInput(attrs={'class': 'form-control'}),
            'email_host_password': forms.PasswordInput(render_value=True, attrs={'class': 'form-control'}),
            'email_use_tls': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
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
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter row title'}),
            'media_type': forms.Select(attrs={'class': 'form-select'}),
            'row_type': forms.Select(attrs={'class': 'form-select'}),
            'genre_tmdb_id': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'TMDB Genre ID'}),
            'region': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., US, GB, IN'}),
            'language': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., en-US, es-ES'}),
            'sort_by': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., popularity.desc, vote_average.desc'}),
            'filter_params': forms.Textarea(attrs={'rows': 4, 'class': 'form-control', 'placeholder': 'JSON e.g., {"vote_average.gte": 7}'}),
            'items_per_page': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Number of items per page'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'auto_scroll': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Display order'}),
        }


class PlayerConfigurationForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['custom_iframe_id_type'].initial = self.initial.get('custom_iframe_id_type', 'tmdb') or 'tmdb'
        self.fields['custom_iframe_id_type'].required = False

    class Meta:
        model = PlayerConfiguration
        fields = [
            'name',
            'media_type',
            'is_active',
            'order',
            'use_for_android',
            'player_color',
            'player_width',
            'player_height',
            'frameborder',
            'allowfullscreen',
            'custom_iframe_mode',
            'custom_iframe_id_type',
            'custom_iframe_url',
            'custom_movie_iframe_url',
            'custom_tv_iframe_url',
            'custom_iframe_html',
            'custom_movie_iframe_html',
            'custom_tv_iframe_html'
        ]
        widgets = {
            'player_color': forms.TextInput(attrs={'placeholder': 'e.g., e50914 (no #)'}),
            'player_width': forms.TextInput(attrs={'placeholder': 'e.g., 100%, 800px'}),
            'player_height': forms.TextInput(attrs={'placeholder': 'e.g., 600px, 100%'}),
            'custom_iframe_mode': forms.Select(attrs={'class': 'form-select'}),
            'custom_iframe_id_type': forms.Select(attrs={'class': 'form-select'}),
            'custom_iframe_url': forms.Textarea(attrs={
                'placeholder': 'Shared iframe URL (used when movie/TV specific URL is empty)',
                'rows': 3,
                'style': 'width: 100%;'
            }),
            'custom_movie_iframe_url': forms.Textarea(attrs={
                'placeholder': 'Movie iframe URL, e.g. https://vidcore.net/movie/{imdb_id}?autoPlay=true&title=true&hideserver=true&poster=true',
                'rows': 3,
                'style': 'width: 100%;'
            }),
            'custom_tv_iframe_url': forms.Textarea(attrs={
                'placeholder': 'TV iframe URL, e.g. https://vidcore.net/tv/{imdb_id}/{season}/{episode}?autoPlay=true&title=true&hideserver=true&poster=true',
                'rows': 3,
                'style': 'width: 100%;'
            }),
            'custom_iframe_html': forms.Textarea(attrs={
                'placeholder': 'Shared full iframe HTML',
                'rows': 5,
                'style': 'width: 100%; font-family: monospace;'
            }),
            'custom_movie_iframe_html': forms.Textarea(attrs={
                'placeholder': 'Movie-specific full iframe HTML',
                'rows': 5,
                'style': 'width: 100%; font-family: monospace;'
            }),
            'custom_tv_iframe_html': forms.Textarea(attrs={
                'placeholder': 'TV-specific full iframe HTML',
                'rows': 5,
                'style': 'width: 100%; font-family: monospace;'
            }),
        }


class AndroidAppForm(forms.ModelForm):
    json_payload_input = forms.CharField(
        label='JSON Data',
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 12, 'placeholder': '{\n  "key": "value"\n}'}),
        help_text='Paste valid JSON that should be returned by this Android app endpoint.'
    )

    class Meta:
        model = AndroidApp
        fields = ['name', 'slug', 'allowed_endpoint', 'allowed_build_id', 'apk_file', 'access_username', 'access_password', 'is_active', 'data_retention_days']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Android app name'}),
            'slug': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'auto-generated-if-empty'}),
            'allowed_endpoint': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'com.example.app or package/build endpoint'}),
            'allowed_build_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '1.0.0, 102, build-2026-07'}),
            'apk_file': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'access_username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Endpoint username'}),
            'access_password': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Endpoint password'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'data_retention_days': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'max': 3650, 'placeholder': '30'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['json_payload_input'].initial = json.dumps(self.instance.json_payload, indent=2)

    def clean_json_payload_input(self):
        value = self.cleaned_data['json_payload_input']
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f'Invalid JSON: {exc}')
        if not isinstance(parsed, (dict, list)):
            raise forms.ValidationError('JSON data must be an object or array.')
        self.cleaned_data['parsed_json_payload'] = parsed
        return value

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.json_payload = self.cleaned_data['parsed_json_payload']
        if commit:
            instance.save()
        return instance


class TMDBApiKeyForm(forms.ModelForm):
    class Meta:
        model = TMDBApiKey
        fields = ['key', 'is_active']
        widgets = {
            'key': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter TMDB API Key'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class TMDBApiKeyEditForm(forms.ModelForm):
    class Meta:
        model = TMDBApiKey
        fields = ['key', 'is_active']
        widgets = {
            'key': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter TMDB API Key'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class ProviderItemForm(forms.ModelForm):
    class Meta:
        model = ProviderItem
        fields = ['name', 'slug', 'tmdb_provider_id', 'logo_path', 'display_priority', 'supports_movies', 'supports_tv', 'url', 'is_enabled']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Provider name'}),
            'slug': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'provider-slug'}),
            'tmdb_provider_id': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'TMDB provider ID'}),
            'logo_path': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '/provider-logo.jpg'}),
            'display_priority': forms.NumberInput(attrs={'class': 'form-control'}),
            'supports_movies': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'supports_tv': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com'}),
            'is_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class WatchRegionForm(forms.ModelForm):
    class Meta:
        model = WatchRegion
        fields = ['code', 'name', 'display_order', 'is_enabled']
        widgets = {
            'code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'US', 'maxlength': 2}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'United States'}),
            'display_order': forms.NumberInput(attrs={'class': 'form-control'}),
            'is_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def clean_code(self):
        code = (self.cleaned_data.get('code') or '').strip().upper()
        if len(code) != 2 or not code.isalpha():
            raise forms.ValidationError('Enter a valid two-letter ISO 3166-1 country code.')
        return code


class NavbarItemForm(forms.ModelForm):
    class Meta:
        model = NavbarItem
        fields = ['name', 'item_type', 'built_in_id', 'url', 'icon', 'is_active', 'order', 'dropdown_items']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter item name'}),
            'built_in_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., home, movies, tv'}),
            'url': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter URL for custom item'}),
            'icon': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Font Awesome icon e.g., fas fa-home'}),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
            'dropdown_items': forms.Textarea(attrs={'rows': 4, 'class': 'form-control', 'placeholder': 'JSON array of dropdown items'}),
        }


class AdForm(forms.ModelForm):
    class Meta:
        model = Ad
        fields = ['name', 'provider', 'position', 'script', 'affiliate_url', 'affiliate_image_url', 'affiliate_title', 'affiliate_price', 'image_url', 'link_url', 'alt_text', 'clicks_required', 'is_active', 'use_for_android', 'order']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter ad name'}),
            'provider': forms.Select(attrs={'class': 'form-select'}),
            'position': forms.Select(attrs={'class': 'form-select'}),
            'script': forms.Textarea(attrs={'rows': 8, 'class': 'form-control', 'placeholder': 'Enter ad script code'}),
            'affiliate_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://www.amazon.com/dp/...?...tag=yourtag-20'}),
            'affiliate_image_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'Amazon product image URL'}),
            'affiliate_title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Product title'}),
            'affiliate_price': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '$19.99'}),
            'image_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'Enter banner image URL'}),
            'link_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'Enter destination URL'}),
            'alt_text': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter image alt text'}),
            'clicks_required': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Number of clicks required'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'use_for_android': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Display order'}),
        }

