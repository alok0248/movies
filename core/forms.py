from django import forms
from django.core.cache import cache
from .models import SiteSettings, ContentRow, PlayerConfiguration, TMDBApiKey, NavbarItem, ProviderItem, WatchRegion, AndroidApp, Ad, AdUnit, AppVersionPolicy
import json
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
        fields = ['active_movie_player', 'active_tv_player', 'player_ui_style']
        widgets = {
            'active_movie_player': forms.Select(attrs={'class': 'form-select'}),
            'active_tv_player': forms.Select(attrs={'class': 'form-select'}),
            'player_ui_style': forms.Select(attrs={'class': 'form-select'}),
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


def _payload_flag(value):
    """Read one of the app's 0/1 switches out of a saved JSON payload."""
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


class AndroidAppForm(forms.ModelForm):
    # The four values the app reads from the top of its config response. They
    # live inside json_payload, but a slider is easier (and safer) to use than
    # editing the JSON by hand, so the form carries them and writes them back
    # into the payload on save.
    login_required = forms.BooleanField(
        label='Login required', required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        help_text='1 asks the app to sign the user in before it shows content; 0 opens on the content.')
    subscription = forms.BooleanField(
        label='Subscription', required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        help_text='1 turns the app\'s subscription gate on; 0 leaves the app free to browse.')
    without_server = forms.BooleanField(
        label='Work without the server', required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        help_text='1 lets the app keep working when this server is unreachable.')
    sync = forms.IntegerField(
        label='Sync timer (seconds)', required=False, min_value=30, max_value=86400,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'min': 30, 'max': 86400, 'step': 1,
                                        'placeholder': '300'}),
        help_text='How often, in seconds, the app re-reads this configuration ("sync": 300).')

    json_payload_input = forms.CharField(
        label='JSON Data',
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 12, 'placeholder': '{\n  "key": "value"\n}', 'style': 'font-family: monospace; font-size: 0.9rem;'}),
        help_text='Paste valid JSON that should be returned by this Android app endpoint. Leave empty for an empty payload.'
    )

    class Meta:
        model = AndroidApp
        fields = ['name', 'slug', 'allowed_endpoint', 'allowed_build_id', 'apk_file', 'access_username', 'access_password', 'is_active', 'data_retention_days', 'log_collection_enabled', 'log_retention_days']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., My Movie App'}),
            'slug': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'auto-generated-if-empty'}),
            'allowed_endpoint': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., com.example.app or #225, #226, #225-#250'}),
            'allowed_build_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., 1.0.0, 102, build-2026-07'}),
            'apk_file': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'access_username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Endpoint username for Basic Auth'}),
            'access_password': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Endpoint password for Basic Auth'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'data_retention_days': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'max': 3650, 'placeholder': '30'}),
            'log_collection_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'log_retention_days': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'max': 3650, 'placeholder': '30'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['json_payload_input'].initial = json.dumps(self.instance.json_payload, indent=2)
        else:
            self.fields['json_payload_input'].initial = '{\n}'
        # The switches are only readable from an object payload; anything else
        # leaves them off and the raw JSON editor stays the way to edit it.
        payload = self.instance.json_payload if isinstance(self.instance.json_payload, dict) else {}
        for key in ('login_required', 'subscription', 'without_server'):
            self.fields[key].initial = _payload_flag(payload.get(key))
        sync_value = payload.get('sync')
        self.fields['sync'].initial = sync_value if sync_value not in (None, '') else 300

    def clean_json_payload_input(self):
        value = self.cleaned_data.get('json_payload_input', '')
        value = (value or '').strip()
        if not value:
            self.cleaned_data['parsed_json_payload'] = {}
            return '{}'
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f'Invalid JSON: {exc}')
        if not isinstance(parsed, (dict, list)):
            raise forms.ValidationError('JSON data must be an object or array.')
        self.cleaned_data['parsed_json_payload'] = parsed
        return value

    def clean(self):
        cleaned = super().clean()
        payload = cleaned.get('parsed_json_payload')
        if payload is None:
            return cleaned
        if not isinstance(payload, dict):
            # The switches are keys of the object, so an array payload cannot
            # carry them. Say so instead of saving sliders that do nothing.
            self.add_error('json_payload_input',
                           'The payload must be a JSON object to use the app switches.')
            return cleaned
        for key in ('login_required', 'subscription', 'without_server'):
            payload[key] = 1 if cleaned.get(key) else 0
        if cleaned.get('sync') is not None:
            payload['sync'] = cleaned['sync']
        elif 'sync' not in payload:
            payload['sync'] = 300
        cleaned['parsed_json_payload'] = payload
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.json_payload = self.cleaned_data['parsed_json_payload']
        if commit:
            instance.save()
            # The app endpoint caches its answer for five minutes. Saving here
            # has to drop that copy, or a flipped switch would keep reaching
            # the app as its old value. Same key as the endpoint builds.
            if instance.slug:
                cache.delete('android_endpoint_payload_%s' % instance.slug)
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



class AdUnitForm(forms.ModelForm):
    """One AdMob banner unit for the app, saved from the Ads page."""

    class Meta:
        model = AdUnit
        fields = ['name', 'ad_unit_id', 'placement', 'refresh_seconds', 'is_active', 'order']
        labels = {
            'name': 'Ad unit name',
            'ad_unit_id': 'Ad unit ID',
            'placement': 'Where it shows in the app',
            'refresh_seconds': 'Refresh every (seconds)',
            'is_active': 'Serve this unit',
            'order': 'Rotation order',
        }
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'testing'}),
            'ad_unit_id': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'ca-app-pub-3676720856033816/3976174493'}),
            'placement': forms.Select(attrs={'class': 'form-select'}),
            'refresh_seconds': forms.NumberInput(attrs={
                'class': 'form-control', 'min': '0', 'max': '600', 'step': '5'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1'}),
        }


class AppVersionPolicyForm(forms.ModelForm):
    """One content rule: which app builds it covers and what stays available."""

    class Meta:
        model = AppVersionPolicy
        fields = ['name', 'android_app', 'min_build', 'max_build',
                  'allow_movies', 'allow_series', 'allow_tv_channels',
                  'message', 'is_active']
        labels = {
            'name': 'Rule name',
            'android_app': 'App',
            'min_build': 'From build',
            'max_build': 'Up to build',
            'allow_movies': 'Allow movies',
            'allow_series': 'Allow series',
            'allow_tv_channels': 'Allow live TV channels',
            'message': 'Message shown in the app',
            'is_active': 'Rule is active',
        }
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Retire beta 3.7.0'}),
            'android_app': forms.Select(attrs={'class': 'form-select'}),
            'min_build': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1'}),
            'max_build': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1'}),
            'allow_movies': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'allow_series': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'allow_tv_channels': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'message': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Please update to the latest version to keep watching.'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def clean_message(self):
        return (self.cleaned_data.get('message') or '').strip()

    def clean(self):
        cleaned = super().clean()
        lowest = cleaned.get('min_build') or 0
        highest = cleaned.get('max_build') or 0
        if highest and lowest and highest < lowest:
            self.add_error('max_build', 'The last build cannot be lower than the first build.')
        return cleaned


class AdSwitchesForm(forms.ModelForm):
    """The one-click master switches shown on the Ads control centre.

    Only the on/off switches live here, so the control centre can save them
    without touching any of the numeric settings.
    """

    class Meta:
        model = SiteSettings
        fields = [
            'enable_autoclick_ads',
            'enable_tile_click_gating',
            'require_ad_consent',
        ]
        labels = {
            'enable_autoclick_ads': 'Auto-click ads (open a sponsor after N clicks)',
            'enable_tile_click_gating': 'Tile click gating (first card click opens a sponsor)',
            'require_ad_consent': 'Require ad consent before any ad loads',
        }
        widgets = {
            'enable_autoclick_ads': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'enable_tile_click_gating': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'require_ad_consent': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class AdMonetizationSettingsForm(forms.ModelForm):
    """AdSense, auto-click / consent and tile-ad settings, grouped for the single Ads page."""

    class Meta:
        model = SiteSettings
        fields = [
            'adsense_verification_meta',
            'adsense_client_id',
            'auto_click_every_clicks',
            'require_ad_consent',
            'ad_consent_message',
            'max_ad_load_retries',
            'tile_ad_every_n',
            'enable_tile_click_gating',
            'tile_ad_source',
            'tile_gating_source',
        ]
        widgets = {
            'adsense_verification_meta': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'googlea1b2c3d4e5f6a7b8'}),
            'adsense_client_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'ca-pub-1234567890123456'}),
            'auto_click_every_clicks': forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'step': '1', 'placeholder': 'e.g., 10'}),
            'require_ad_consent': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'ad_consent_message': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'max_ad_load_retries': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1', 'placeholder': 'e.g., 3'}),
            'tile_ad_every_n': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1', 'placeholder': 'e.g., 10'}),
            'enable_tile_click_gating': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'tile_ad_source': forms.Select(attrs={'class': 'form-select'}),
            'tile_gating_source': forms.Select(attrs={'class': 'form-select'}),
        }


class GeneralSettingsForm(forms.ModelForm):
    """Site identity, measurements and ad behaviour.

    Every field here is owned by this page and by no other: the branding,
    display, footer, player, email, URL-blocking, data-source and TMDB database
    pages each keep their own fields, so a value can never be saved from two
    different screens and quietly overwrite the other.
    """

    class Meta:
        model = SiteSettings
        fields = [
            'url_format',
            'google_analytics_id',
            'adsense_client_id',
            'adsense_verification_meta',
            'google_login_enabled',
            'google_login_client_id',
            'google_login_allowed_domains',
            'enable_autoclick_ads',
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
            'url_format': forms.Select(attrs={'class': 'form-select'}),
            'google_analytics_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'G-XXXXXXXXXX'}),
            'adsense_client_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'ca-pub-0000000000000000'}),
            'adsense_verification_meta': forms.TextInput(attrs={'class': 'form-control'}),
            'google_login_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'google_login_client_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '…apps.googleusercontent.com'}),
            'google_login_allowed_domains': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'enable_autoclick_ads': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'auto_click_every_clicks': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'default_clicks_required': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'require_ad_consent': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'ad_consent_message': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'max_ad_load_retries': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'idm_visibility': forms.Select(attrs={'class': 'form-select'}),
            'tile_ad_every_n': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'enable_tile_click_gating': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'tile_ad_source': forms.Select(attrs={'class': 'form-select'}),
            'tile_gating_source': forms.Select(attrs={'class': 'form-select'}),
        }


class SecurityProtectionForm(forms.ModelForm):
    """Which traffic the site treats as its own bot, and browser protection."""

    class Meta:
        model = SiteSettings
        fields = ['bot_ips', 'bot_user_agents', 'dev_mode_protection',
                  'require_login_to_browse']
        widgets = {
            'bot_ips': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3, 'placeholder': '192.168.1.10, 10.0.0.5'}),
            'bot_user_agents': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3, 'placeholder': 'Googlebot, MyCustomCrawler'}),
            'dev_mode_protection': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'require_login_to_browse': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


# Which section title each field is shown under on the grouped settings pages.
# A field missing from here still renders, under "Other", so nothing is lost.
GENERAL_FIELD_SECTIONS = [
    ('Site identity', 'How detail-page links look and which measurements the site reports.',
     ['url_format', 'google_analytics_id', 'adsense_client_id', 'adsense_verification_meta']),
    ('Google sign-in', 'The "Continue with Google" button and who may use it.',
     ['google_login_enabled', 'google_login_client_id', 'google_login_allowed_domains']),
    ('Ads', 'When ads may open, how often, and who sees the IDM button.',
     ['enable_autoclick_ads', 'auto_click_every_clicks',
      'default_clicks_required', 'require_ad_consent', 'ad_consent_message',
      'max_ad_load_retries', 'idm_visibility', 'tile_ad_every_n',
      'enable_tile_click_gating', 'tile_ad_source', 'tile_gating_source']),
]

SECURITY_FIELD_SECTIONS = [
    ('Bot signatures',
     'The addresses and user-agent fragments this site treats as its own bot.',
     ['bot_ips', 'bot_user_agents']),
    ('Browser protection', 'Extra checks applied to visitors, not to staff.',
     ['dev_mode_protection']),
    ('Access control', 'Whether a visitor has to sign in before any page opens.',
     ['require_login_to_browse']),
]
