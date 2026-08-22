from django import template
from datetime import date

register = template.Library()


@register.filter(name='get_item')
def get_item(dictionary, key):
    """Get an item from a dictionary by key."""
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None


@register.filter(name='tojson')
def tojson(value):
    """Convert value to JSON string."""
    import json
    return json.dumps(value)


@register.filter(name='is_future_date')
def is_future_date(value):
    """Return True if the date is in the future."""
    if not value:
        return False
    try:
        if isinstance(value, str):
            from datetime import datetime
            d = datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
        else:
            d = value
        return d > date.today()
    except (ValueError, TypeError):
        return False
