import json
import logging
import random

from django.conf import settings as django_settings
from django.utils import timezone

from .models import SiteSettings, NavbarItem, ProviderItem, WatchRegion, Ad, UserActivity, AmazonAffiliateProduct
from .tmdb_client import get_data_client

logger = logging.getLogger(__name__)


def get_user_today_clicks(user, ip_address):
    today = timezone.now().date()
    try:
        if user and user.is_authenticated:
            activity, created = UserActivity.objects.get_or_create(user=user, activity_date=today, defaults={'ip_address': ip_address})
        else:
            activity, created = UserActivity.objects.get_or_create(ip_address=ip_address, activity_date=today, defaults={'user': None})
        return activity.clicks_today
    except Exception:
        return 0


def site_settings(request):
    settings = SiteSettings.get_settings()
    title_sizes = {
        'small': '0.9rem',
        'medium': '1.1rem',
        'large': '1.3rem',
        'xl': '1.5rem'
    }
    text_sizes = {
        'small': '0.8rem',
        'medium': '0.9rem',
        'large': '1rem',
        'xl': '1.1rem'
    }

    movie_genres = []
    series_genres = []
    try:
        client = get_data_client()
        movie_genres = client.get_movie_genres().get('genres', [])
        series_genres = client.get_series_genres().get('genres', [])
    except Exception as e:
        logger.error("Error fetching genres: %s", e)

    theme_colors = {
        'cinevault': {'primary': '#00c896', 'bg': '#0a0a0f', 'bg_secondary': '#121018'},
        'netflix': {'primary': '#e50914', 'bg': '#141414', 'bg_secondary': '#1f1f1f'},
        'amazon': {'primary': '#00a8e1', 'bg': '#0f171e', 'bg_secondary': '#1a242e'},
        'hbo': {'primary': '#9333ea', 'bg': '#080808', 'bg_secondary': '#1a1a1a'},
        'disney': {'primary': '#0063e3', 'bg': '#040714', 'bg_secondary': '#0b0d17'},
        'spotify': {'primary': '#1db954', 'bg': '#121212', 'bg_secondary': '#181818'},
    }

    theme = theme_colors.get(settings.theme_style, theme_colors['netflix'])

    if NavbarItem.objects.count() == 0:
        defaults = [
            {'name': 'Home', 'built_in_id': 'home', 'item_type': 'built_in', 'order': 0, 'is_active': True, 'icon': 'fas fa-home'},
            {'name': 'Movies', 'built_in_id': 'movies', 'item_type': 'built_in', 'order': 1, 'is_active': True, 'icon': 'fas fa-film'},
            {'name': 'TV Shows', 'built_in_id': 'tv_shows', 'item_type': 'built_in', 'order': 2, 'is_active': True, 'icon': 'fas fa-tv'},
            {'name': 'Live TV', 'built_in_id': 'live_tv', 'item_type': 'built_in', 'order': 3, 'is_active': True, 'icon': 'fas fa-broadcast-tower'},
            {'name': 'My Watch List', 'built_in_id': 'watchlist', 'item_type': 'built_in', 'order': 4, 'is_active': True, 'icon': 'fas fa-bookmark'},
            {'name': 'Genres', 'built_in_id': 'genres', 'item_type': 'built_in', 'order': 5, 'is_active': True, 'icon': 'fas fa-tags'},
            {'name': 'Provider', 'built_in_id': 'provider', 'item_type': 'built_in', 'order': 6, 'is_active': True, 'icon': 'fas fa-play-circle'},
            {'name': 'Calendar', 'built_in_id': 'calendar', 'item_type': 'built_in', 'order': 7, 'is_active': True, 'icon': 'fas fa-calendar-alt'},
            {'name': 'Upcoming', 'built_in_id': 'upcoming', 'item_type': 'built_in', 'order': 8, 'is_active': True, 'icon': 'fas fa-rocket'},
        ]
        for item_data in defaults:
            NavbarItem.objects.create(**item_data)

    navbar_items = NavbarItem.objects.filter(is_active=True).order_by('order')
    enabled_providers = ProviderItem.objects.filter(is_enabled=True).order_by('display_priority', 'name')[:50]
    active_provider = None
    active_provider_slug = request.session.get('active_provider_slug')
    if active_provider_slug:
        # Search all providers for active one (not just top 50)
        active_provider = ProviderItem.objects.filter(slug=active_provider_slug, is_enabled=True).first()
    enabled_watch_regions = WatchRegion.objects.filter(is_enabled=True).order_by('display_order', 'name')
    
    # Get eligible ads for the current user
    user = request.user
    ip_address = request.META.get('REMOTE_ADDR')
    today_clicks = get_user_today_clicks(user, ip_address)
    eligible_ads = Ad.objects.filter(is_active=True, clicks_required__lte=today_clicks).order_by('order', 'name')
    ads_by_position = {}
    for ad in eligible_ads:
        ads_by_position.setdefault(ad.position, []).append(ad)
    sitewide_ads = [ad for ad in eligible_ads if ad.position in ('head', 'sidebar', 'footer', 'popup')]

    # Deferred ad loading: build full eligible ad list for context, but DO NOT pre-render HTML.
    # Actual ad HTML is served by /ajax/click-status/ ONLY when threshold_met AND consent ok.
    # Here we just pass settings and positional slot info (empty until hydrated client-side).
    default_clicks_required = max(0, int(getattr(settings, 'default_clicks_required', 0) or 0))
    require_ad_consent = bool(getattr(settings, 'require_ad_consent', False))
    max_ad_load_retries = max(0, int(getattr(settings, 'max_ad_load_retries', 3) or 0))
    ad_consent_message = str(getattr(settings, 'ad_consent_message', '') or '')
    auto_click_every_clicks = max(1, int(getattr(settings, 'auto_click_every_clicks', 10) or 10))

    # Build the list of ad URLs to open in a new tab after every-N-clicks cadence.
    # Consider ALL active ads (not only popup position), picking affiliate_url first,
    # falling back to link_url. These are refreshed client-side via /ajax/autoclick-ads/
    # so newly-eligible ads are picked up without a page reload.
    autoclick_ad_urls = []
    try:
        for ad in Ad.objects.filter(is_active=True).order_by('order', 'name'):
            url = (getattr(ad, 'affiliate_url', None) or '').strip()
            if not url:
                url = (getattr(ad, 'link_url', None) or '').strip()
            if url:
                autoclick_ad_urls.append(url)
    except Exception:
        autoclick_ad_urls = []
    autoclick_ad_urls_json = json.dumps(autoclick_ad_urls)

    # Session-level values (current state at render time)
    session_clicks = int(request.session.get('ad_valid_clicks', 0) or 0)
    consent_val = request.session.get('ad_consent')
    consent_given = False
    if consent_val == 'given':
        consent_given = True
    elif consent_val == 'denied':
        consent_given = False
    else:
        consent_given = not require_ad_consent

    # Effective required threshold (max default + any per-Ad values) at page render time
    try:
        _all_active_ads = Ad.objects.filter(is_active=True)
        _per_ad_req = [max(0, int(a.clicks_required or 0)) for a in _all_active_ads]
        effective_threshold = default_clicks_required
        if _per_ad_req:
            effective_threshold = max(effective_threshold, *_per_ad_req)
    except Exception:
        effective_threshold = default_clicks_required

    threshold_met = today_clicks >= effective_threshold
    can_load_ads_on_page = threshold_met and ((not require_ad_consent) or consent_given)

    idm_visibility_raw = str(getattr(settings, 'idm_visibility', 'all_users') or 'all_users')
    if idm_visibility_raw not in ('hide', 'logged_in', 'admin_only', 'all_users'):
        idm_visibility_raw = 'all_users'
    _user = getattr(request, 'user', None)
    _is_auth = bool(_user and getattr(_user, 'is_authenticated', False))
    _is_admin = bool(_user and (getattr(_user, 'is_staff', False) or getattr(_user, 'is_superuser', False)))
    if idm_visibility_raw == 'hide':
        can_show_idm = False
    elif idm_visibility_raw == 'admin_only':
        can_show_idm = _is_admin
    elif idm_visibility_raw == 'logged_in':
        can_show_idm = _is_auth
    else:
        can_show_idm = True
    idm_visibility = idm_visibility_raw

    tile_ad_every_n = max(0, int(getattr(settings, 'tile_ad_every_n', 10) or 10))
    enable_tile_click_gating = bool(getattr(settings, 'enable_tile_click_gating', True))
    tile_ad_source = str(getattr(settings, 'tile_ad_source', 'ads_table') or 'ads_table')
    tile_gating_source = str(getattr(settings, 'tile_gating_source', 'autoclick_urls') or 'autoclick_urls')
    if tile_ad_source not in ('ads_table', 'amazon_random', 'amazon_sequence'):
        tile_ad_source = 'ads_table'
    if tile_gating_source not in ('autoclick_urls', 'amazon_random', 'amazon_sequence'):
        tile_gating_source = 'autoclick_urls'

    try:
        amazon_active_qs = AmazonAffiliateProduct.objects.filter(is_active=True).order_by('order', 'title')
        amazon_products_base = [
            {
                'id': p.id,
                'affiliate_url': p.affiliate_url or '',
                'title': p.title or '',
                'image_url': p.image_url or '',
                'price': p.price or '',
            }
            for p in amazon_active_qs
        ]
    except Exception:
        amazon_products_base = []

    def _to_amazon_tile_ad(p):
        return {
            'id': 'azp_' + str(p.get('id', '')),
            'name': p.get('title', '') or 'Amazon Product',
            'provider': 'amazon_affiliate',
            'script': '',
            'affiliate_url': p.get('affiliate_url', '') or '',
            'affiliate_image_url': p.get('image_url', '') or '',
            'affiliate_title': p.get('title', '') or 'Amazon Product',
            'affiliate_price': p.get('price', '') or '',
            'image_url': '',
            'link_url': '',
            'alt_text': p.get('title', '') or 'Amazon Product',
        }

    # --- Tile ads list ---
    if can_load_ads_on_page and tile_ad_source in ('amazon_random', 'amazon_sequence'):
        products_for_tiles = list(amazon_products_base)
        if tile_ad_source == 'amazon_random':
            try:
                random.shuffle(products_for_tiles)
            except Exception:
                pass
        tile_ads_raw = []
        tile_ads_data = [_to_amazon_tile_ad(p) for p in products_for_tiles]
    else:
        tile_ads_raw = list(eligible_ads.filter(position='tile')) if can_load_ads_on_page else []
        tile_ads_data = []
        for tad in tile_ads_raw:
            ad_obj = {
                'id': tad.id,
                'name': tad.name,
                'provider': tad.provider,
                'script': tad.script or '',
                'affiliate_url': tad.affiliate_url or '',
                'affiliate_image_url': tad.affiliate_image_url or '',
                'affiliate_title': tad.affiliate_title or tad.name,
                'affiliate_price': tad.affiliate_price or '',
                'image_url': tad.image_url or '',
                'link_url': tad.link_url or '',
                'alt_text': tad.alt_text or tad.name,
            }
            tile_ads_data.append(ad_obj)
    tile_ads_json = json.dumps(tile_ads_data)

    # --- Gating ad URLs (and the Amazon gating product list for tiles rendering) ---
    if tile_gating_source in ('amazon_random', 'amazon_sequence'):
        products_for_gating = list(amazon_products_base)
        if tile_gating_source == 'amazon_random':
            try:
                random.shuffle(products_for_gating)
            except Exception:
                pass
        gating_ad_urls = [p.get('affiliate_url', '') for p in products_for_gating if p.get('affiliate_url')]
        amazon_products_for_gating_json = json.dumps(products_for_gating)
        amazon_tile_products_for_gating_json = json.dumps([_to_amazon_tile_ad(p) for p in products_for_gating])
    else:
        gating_ad_urls = list(autoclick_ad_urls)
        amazon_products_for_gating_json = json.dumps([])
        amazon_tile_products_for_gating_json = json.dumps([])

    amazon_products_json = json.dumps(amazon_products_base)

    deferred_settings_json = json.dumps({
        'default_clicks_required': default_clicks_required,
        'require_ad_consent': require_ad_consent,
        'max_ad_load_retries': max_ad_load_retries,
        'ad_consent_message': ad_consent_message,
        'session_clicks': session_clicks,
        'today_clicks': today_clicks,
        'effective_threshold': effective_threshold,
        'consent_given': consent_given,
        'consent_denied': consent_val == 'denied',
        'can_load_ads': can_load_ads_on_page,
    })

    return {
        'site_settings': settings,
        'title_size': title_sizes[settings.title_size],
        'text_size': text_sizes[settings.text_size],
        'font_family': settings.font_family,
        'theme_primary': theme['primary'],
        'theme_bg': theme['bg'],
        'theme_bg_secondary': theme['bg_secondary'],
        'settings': django_settings,
        'movie_genres': movie_genres,
        'series_genres': series_genres,
        'navbar_items': navbar_items,
        'enabled_providers': enabled_providers,
        'active_provider': active_provider,
        'enabled_watch_regions': enabled_watch_regions,
        'eligible_ads': eligible_ads,
        'ads_by_position': ads_by_position,
        'sitewide_ads': sitewide_ads,
        'autoclick_ad_urls': autoclick_ad_urls,
        'autoclick_ad_urls_json': autoclick_ad_urls_json,
        'auto_click_every_clicks': auto_click_every_clicks,
        'deferred_ads_settings_json': deferred_settings_json,
        'default_clicks_required': default_clicks_required,
        'require_ad_consent': require_ad_consent,
        'max_ad_load_retries': max_ad_load_retries,
        'ad_consent_message': ad_consent_message,
        'can_load_ads': can_load_ads_on_page,
        'ads_threshold_effective': effective_threshold,
        'ads_today_clicks': today_clicks,
        'idm_visibility': idm_visibility,
        'can_show_idm': can_show_idm,
        'tile_ad_every_n': tile_ad_every_n,
        'enable_tile_click_gating': enable_tile_click_gating,
        'tile_ads': tile_ads_raw,
        'tile_ads_json': tile_ads_json,
        'tile_ad_source': tile_ad_source,
        'tile_gating_source': tile_gating_source,
        'gating_ad_urls': gating_ad_urls,
        'gating_ad_urls_json': json.dumps(gating_ad_urls),
        'amazon_products_json': amazon_products_json,
        'amazon_products_for_gating_json': amazon_products_for_gating_json,
        'amazon_tile_products_for_gating_json': amazon_tile_products_for_gating_json,
    }
