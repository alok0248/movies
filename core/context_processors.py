from .models import SiteSettings, NavbarItem


def site_settings(request):
    """Make site settings and navbar items available to all templates."""
    return {
        'site_settings': SiteSettings.get_settings(),
        'navbar_items': NavbarItem.objects.filter(is_active=True).order_by('order'),
    }
