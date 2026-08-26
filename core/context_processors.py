import json
from .models import SiteSettings, NavbarItem, Ad, AmazonAffiliateProduct, ProviderItem, WatchRegion


def site_settings(request):
    """Make site settings, navbar items, and ad data available to all templates."""
    ss = SiteSettings.get_settings()

    # Autoclick ad URLs
    autoclick_ads = Ad.objects.filter(
        is_active=True,
        provider__in=['amazon_affiliate', 'custom_image'],
    ).order_by('order')
    autoclick_urls = []
    for ad in autoclick_ads:
        if ad.provider == 'amazon_affiliate' and ad.affiliate_url:
            autoclick_urls.append(ad.affiliate_url)
        elif ad.link_url:
            autoclick_urls.append(ad.link_url)

    # Deferred ads settings JSON
    deferred_settings = {
        'default_clicks_required': ss.default_clicks_required or 0,
        'require_ad_consent': bool(ss.require_ad_consent),
        'ad_consent_message': ss.ad_consent_message or '',
        'max_ad_load_retries': ss.max_ad_load_retries or 3,
    }

    # Tile ads
    tile_ad_source = ss.tile_ad_source or 'ads_table'
    tile_ads = []
    if tile_ad_source == 'ads_table':
        for ad in Ad.objects.filter(is_active=True, position='tile').order_by('order'):
            tile_ads.append({
                'id': ad.id,
                'affiliate_url': ad.affiliate_url or '',
                'affiliate_image_url': ad.affiliate_image_url or '',
                'affiliate_title': ad.affiliate_title or '',
                'affiliate_price': ad.affiliate_price or '',
                'image_url': ad.image_url or '',
                'link_url': ad.link_url or '',
            })
    else:
        for p in AmazonAffiliateProduct.objects.filter(is_active=True).order_by('order')[:50]:
            tile_ads.append({
                'id': p.id,
                'affiliate_url': p.affiliate_url or '',
                'affiliate_image_url': p.image_url or '',
                'affiliate_title': p.title or '',
                'affiliate_price': p.price or '',
            })

    # Gating ad URLs
    gating_source = ss.tile_gating_source or 'autoclick_urls'
    gating_ad_urls = list(autoclick_urls)  # default pool
    if gating_source in ('amazon_random', 'amazon_sequence'):
        order = 'order' if gating_source == 'amazon_sequence' else '?'
        gating_ad_urls = [p.affiliate_url for p in AmazonAffiliateProduct.objects.filter(is_active=True).order_by(order)[:50] if p.affiliate_url]

    # Amazon products
    amazon_products = []
    for p in AmazonAffiliateProduct.objects.filter(is_active=True).order_by('order')[:50]:
        amazon_products.append({
            'id': p.id,
            'title': p.title or '',
            'affiliate_url': p.affiliate_url or '',
            'image_url': p.image_url or '',
            'price': p.price or '',
        })

    # Providers and watch regions for navbar
    enabled_providers = ProviderItem.objects.filter(is_enabled=True).order_by('display_priority')[:100]
    watch_regions_nav = WatchRegion.objects.filter(is_enabled=True).order_by('display_order')[:50]

    return {
        'site_settings': ss,
        'navbar_items': NavbarItem.objects.filter(is_active=True).order_by('order'),
        'enabled_providers': enabled_providers,
        'watch_regions_nav': watch_regions_nav,
        'auto_click_every_clicks': ss.auto_click_every_clicks or 10,
        'deferred_ads_settings_json': json.dumps(deferred_settings),
        'autoclick_ad_urls_json': json.dumps(autoclick_urls),
        'tile_ads_json': json.dumps(tile_ads),
        'gating_ad_urls_json': json.dumps(gating_ad_urls),
        'amazon_products_json': json.dumps(amazon_products),
        'enable_tile_click_gating': ss.enable_tile_click_gating,
        'tile_gating_source': gating_source,
        'tile_ad_source': tile_ad_source,
    }
