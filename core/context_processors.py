"""Values the admin chrome needs on every page."""

from django.core.cache import cache

ALERT_CACHE_KEY = 'data_health_alert_v1'
ALERT_CACHE_SECONDS = 120


def health_alert_rows():
    """The checks that currently need a look, cached for two minutes."""
    from .health import check_rows

    rows = cache.get(ALERT_CACHE_KEY)
    if rows is None:
        try:
            rows = [{'slug': slug, 'label': label, 'count': count}
                    for slug, label, count in check_rows() if count]
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
