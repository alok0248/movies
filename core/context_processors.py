import json
import re
from .models import SiteSettings, NavbarItem, Ad, AdsterraLink, AmazonAffiliateProduct, ProviderItem, WatchRegion
from .google_auth import google_login_enabled, resolve_client_id


def _json_for_inline_script(value):
    """Serialise ``value`` for embedding inside an inline <script> block.

    json.dumps leaves ``<``, ``>`` and ``&`` as themselves, so an ad title or
    URL containing ``</script>`` would end the template's script element and
    the remainder of the value would then be parsed by the browser as page
    markup. Escaping them as \\uXXXX keeps the value byte-identical once the
    page reads it back (the template evaluates these blobs as JS literals), so
    nothing an ad shows changes.
    """
    return (
        json.dumps(value)
        .replace('<', '\\u003C')
        .replace('>', '\\u003E')
        .replace('&', '\\u0026')
        .replace('\u2028', '\\u2028')
        .replace('\u2029', '\\u2029')
    )


def _ad_consent_value(request):
    """The visitor's stored ad-consent choice: 'given', 'denied' or ''."""
    try:
        return (request.session.get('ad_consent') or '').strip().lower()
    except Exception:
        return ''


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
        'consent_given': _ad_consent_value(request) == 'given',
        'consent_denied': _ad_consent_value(request) == 'denied',
        'autoclick_enabled': bool(ss.enable_autoclick_ads),
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

    # Adsterra ads: URL units rotate/open after N clicks; JS units are injected.
    adsterra_links = []
    adsterra_js_codes = []
    adsterra_banner_url = ''
    for link in AdsterraLink.objects.filter(is_active=True).order_by('order', 'id')[:100]:
        if link.url:
            adsterra_links.append({'id': link.id, 'url': link.url})
            if not adsterra_banner_url:
                adsterra_banner_url = link.url
        code = (link.code or '').strip()
        if code:
            adsterra_js_codes.append(code)

    # First script src among the Adsterra JS units - the ad-blocker notice probes it
    # to see whether the ad host is reachable from the visitor's browser.
    adsterra_probe_url = ''
    for _code in adsterra_js_codes:
        _m = re.search(r'src="([^"]+)"', _code) or re.search(r"src='([^']+)'", _code)
        if _m:
            adsterra_probe_url = _m.group(1)
            break

    # Providers and watch regions for navbar
    enabled_providers = ProviderItem.objects.filter(is_enabled=True).order_by('display_priority')[:100]
    watch_regions_nav = WatchRegion.objects.filter(is_enabled=True).order_by('display_order')[:50]

    return {
        'site_settings': ss,
        'navbar_items': NavbarItem.objects.filter(is_active=True).order_by('order'),
        'enabled_providers': enabled_providers,
        'watch_regions_nav': watch_regions_nav,
        'auto_click_every_clicks': ss.auto_click_every_clicks or 10,
        'deferred_ads_settings_json': _json_for_inline_script(deferred_settings),
        'autoclick_ad_urls_json': _json_for_inline_script(autoclick_urls),
        'tile_ads_json': _json_for_inline_script(tile_ads),
        'gating_ad_urls_json': _json_for_inline_script(gating_ad_urls),
        'amazon_products_json': _json_for_inline_script(amazon_products),
        'enable_tile_click_gating': ss.enable_tile_click_gating,
        'tile_gating_source': gating_source,
        'tile_ad_source': tile_ad_source,
        'adsterra_links_json': _json_for_inline_script(adsterra_links),
        'adsterra_js_codes': adsterra_js_codes,
        'adsterra_probe_url': adsterra_probe_url,
        'adsterra_banner_url': adsterra_banner_url,
        'adsterra_has_links': bool(adsterra_links),
        'adsterra_clicks_required': int(ss.adsterra_clicks_required or 0),
        'adsterra_show_banner': bool(ss.adsterra_show_banner),
        'enable_adsterra_links': bool(ss.enable_adsterra_links),
        'enable_autoclick_ads': bool(ss.enable_autoclick_ads),
        # Google Sign-In: whether the popup should offer the Google button, and
        # which OAuth client ID the browser should initialise with. Both come
        # from the admin toggle so the button can be switched off without a
        # deploy. The button is hidden (not merely rejected) when off.
        'google_login_enabled': google_login_enabled(),
        'google_login_client_id': resolve_client_id(),
    }


def settings_wrapper(request):
    """Let a settings page render as a bare panel instead of a whole page.

    The Site / Data & APIs / Delivery hubs show one settings panel at a time.
    A hub sets ``request._settings_embed`` before calling the existing settings
    view, and this processor then supplies ``settings_wrapper`` so the page's
    own template extends the bare fragment base rather than the admin shell.

    Templates read ``settings_wrapper|default:'core/admin_base.html'``, so a
    settings page opened on its own URL keeps rendering with its full chrome —
    old bookmarks still work, they just land on the one page they name.
    """
    if getattr(request, '_settings_embed', False):
        return {'settings_wrapper': 'core/settings_panel_base.html'}
    return {}


def admin_badges(request):
    """Live counters for the admin sidebar.

    Four numbers that say something needs a look, cached so a page view does
    not pay for them every time:

    * ``blocked``       — addresses currently refused by the site,
    * ``blocked_today`` — app accounts blocked since midnight,
    * ``api_errors``    — errors recorded since this admin last opened the page
                          (per-browser watermark in the session),
    * ``alerts``        — health checks currently failing,
    * ``waiting_messages`` / ``overdue_messages`` — support messages without an
      answer, and those past the first-response target.

    Everything is wrapped: a database hiccup must never break a page render,
    and the whole processor only runs for staff inside /admin-dashboard/.
    """
    try:
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated and (user.is_staff or user.is_superuser)):
            return {}
        if not (getattr(request, 'path', '') or '').startswith('/admin-dashboard'):
            return {}
    except Exception:
        return {}

    badges = {'blocked': 0, 'blocked_today': 0, 'api_errors': 0, 'alerts': 0,
              'waiting_messages': 0, 'overdue_messages': 0}
    try:
        from django.core.cache import cache
        from django.utils import timezone
        from .models import BlockedIP, ErrorLog, SyncedUser

        blocked = cache.get('admin_badge_blocked')
        if blocked is None:
            blocked = BlockedIP.objects.filter(is_active=True).count()
            cache.set('admin_badge_blocked', blocked, 60)
        badges['blocked'] = int(blocked)

        today = cache.get('admin_badge_blocked_today')
        if today is None:
            today = SyncedUser.objects.filter(
                block_mode__gt='', blocked_at__date=timezone.localdate()).count()
            cache.set('admin_badge_blocked_today', today, 120)
        badges['blocked_today'] = int(today)

        seen = None
        try:
            seen = request.session.get('admin_api_errors_seen')
        except Exception:
            seen = None
        errors = ErrorLog.objects.all()
        if seen:
            from django.utils.dateparse import parse_datetime
            stamp = parse_datetime(seen)
            if stamp:
                errors = errors.filter(created_at__gt=stamp)
        else:
            errors = errors.filter(created_at__gte=timezone.now() - timezone.timedelta(hours=24))
        badges['api_errors'] = errors.count()

        failing = cache.get('admin_badge_health')
        if failing is None:
            try:
                from .health import run_checks
                from .models import AlertIncident
                findings = run_checks()          # also opens/updates incidents
                open_rows = AlertIncident.objects.filter(resolved_at__isnull=True).count()
                # before the first incident row exists the failing checks are
                # the same number, so the badge is never silently empty
                failing = open_rows or sum(1 for f in findings if f.get('ok') is False)
            except Exception:
                failing = 0
            cache.set('admin_badge_health', failing, 120)
        badges['alerts'] = int(failing)

        waiting = cache.get('admin_badge_waiting_messages')
        if waiting is None:
            from .models import UserMessage
            waiting = UserMessage.objects.filter(replied_at=None).count()
            cache.set('admin_badge_waiting_messages', waiting, 60)
        badges['waiting_messages'] = int(waiting)

        overdue = cache.get('admin_badge_overdue_messages')
        if overdue is None:
            from .support import stats as support_stats
            overdue = support_stats()['overdue']
            cache.set('admin_badge_overdue_messages', overdue, 120)
        badges['overdue_messages'] = int(overdue)
    except Exception:
        pass

    return {'admin_badges': badges}


"""Values the admin chrome needs on every page."""

from django.core.cache import cache

ALERT_CACHE_KEY = 'data_health_alert_v1'
ALERT_CACHE_SECONDS = 120


def health_alert_rows():
    """The checks that currently need a look, cached for two minutes."""
    from .health import open_check_rows

    rows = cache.get(ALERT_CACHE_KEY)
    if rows is None:
        try:
            rows = [{'slug': slug, 'label': label, 'count': count}
                    for slug, label, count in open_check_rows()]
        except Exception:      # a badge is never worth a broken page
            return []
        cache.set(ALERT_CACHE_KEY, rows, ALERT_CACHE_SECONDS)
    return rows


def health_alert_count():
    return len(health_alert_rows())


def data_health_alert(request):
    """Expose ``health_alerts`` and the open checks to the admin sidebar."""
    if not (getattr(request, 'path', '') or '').startswith('/admin-dashboard'):
        return {}
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated and user.is_staff):
        return {}

    rows = health_alert_rows()
    return {'health_alerts': len(rows), 'health_alert_rows': rows}
