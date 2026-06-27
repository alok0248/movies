
from django.shortcuts import redirect
from django.conf import settings
from .models import SiteSettings

class URLBlockMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Check if we should apply blocking
        try:
            site_settings = SiteSettings.get_settings()
            if not site_settings.enable_url_blocking:
                return self.get_response(request)
        except Exception:
            return self.get_response(request)

        # Allow admin URLs always
        if request.path.startswith('/admin'):
            return self.get_response(request)

        # Check if we should block all except admin
        blocked_urls_text = site_settings.blocked_urls or ''
        blocked_urls_list = [line.strip() for line in blocked_urls_text.splitlines() if line.strip()]

        should_block = False
        if 'all' in blocked_urls_list and not request.path == '/':
            should_block = True
        else:
            for blocked_url in blocked_urls_list:
                if blocked_url and (blocked_url in request.path or request.path.startswith(blocked_url)):
                    should_block = True
                    break

        if should_block:
            redirect_to = site_settings.redirect_url or '/'
            if request.path != redirect_to:
                return redirect(redirect_to)

        return self.get_response(request)

class EmailSettingsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Apply email settings from SiteSettings
        try:
            site_settings = SiteSettings.get_settings()
            if site_settings.email_host_user:
                settings.EMAIL_HOST = site_settings.email_host
                settings.EMAIL_PORT = site_settings.email_port
                settings.EMAIL_HOST_USER = site_settings.email_host_user
                settings.EMAIL_HOST_PASSWORD = site_settings.email_host_password
                settings.EMAIL_USE_TLS = site_settings.email_use_tls
                settings.DEFAULT_FROM_EMAIL = site_settings.email_host_user
        except Exception:
            pass
        
        return self.get_response(request)
