"""The Amazon affiliate links saved on the Ads page, and where they end up.

That page is the only place these links are edited. The app config's
``ads1.amazon1.url`` list is derived from it in two places: when the config is
built for a request, and in each app's stored payload, so the app and the admin
page show the same links instead of an empty list next to a full one.
"""
from django.core.cache import cache

from .models import AmazonAffiliateProduct, AndroidApp

CACHE_KEY = 'android_endpoint_payload_%s'


def active_urls():
    """The enabled links in their saved order, each one once."""
    urls, seen = [], set()
    for url in (AmazonAffiliateProduct.objects.filter(is_active=True)
                .order_by('order', 'id')
                .values_list('affiliate_url', flat=True)):
        url = (url or '').strip()
        if not url or url.lower() in seen:
            continue
        seen.add(url.lower())
        urls.append(url)
    return urls


def with_links(payload, urls=None):
    """A copy of the payload whose ``ads1.amazon1.url`` is the saved links.

    The block's tag and everything else around it are kept as they are; a
    payload without that block comes back untouched.
    """
    if not isinstance(payload, dict):
        return payload
    ads1 = payload.get('ads1')
    if not isinstance(ads1, dict) or not isinstance(ads1.get('amazon1'), dict):
        return payload
    amazon1 = dict(ads1['amazon1'])
    amazon1['url'] = active_urls() if urls is None else list(urls)
    ads1 = dict(ads1)
    ads1['amazon1'] = amazon1
    payload = dict(payload)
    payload['ads1'] = ads1
    return payload


def clear_payload_caches():
    """Drop the cached app configs, which carry these links."""
    for slug in AndroidApp.objects.values_list('slug', flat=True):
        cache.delete(CACHE_KEY % slug)


def sync_app_payloads():
    """Write the saved links into every app's stored payload.

    Saved with ``update()`` so no model signal fires back into this path, and
    the cache is dropped afterwards: what the admin page reads and what the app
    is served are then the same list.
    """
    urls = active_urls()
    changed = 0
    for app in AndroidApp.objects.all():
        current = app.json_payload
        filled = with_links(current, urls)
        if filled == current:
            continue
        AndroidApp.objects.filter(pk=app.pk).update(json_payload=filled)
        changed += 1
    clear_payload_caches()
    return changed
