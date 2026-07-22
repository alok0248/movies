
from django.shortcuts import redirect
from django.conf import settings
from django.utils import timezone
from .models import SiteSettings, WebsiteVisitor, WebsiteVisitorVisit
import uuid


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def _split_bot_config(raw_value):
    if not raw_value:
        return []
    normalized = raw_value.replace('\n', ',')
    return [item.strip() for item in normalized.split(',') if item.strip()]


def is_bot_request(request, client_ip, site_settings):
    configured_ips = set(_split_bot_config(site_settings.bot_ips))
    configured_user_agents = [value.lower() for value in _split_bot_config(site_settings.bot_user_agents)]
    user_agent = (request.META.get('HTTP_USER_AGENT', '') or '').lower()

    if client_ip and client_ip in configured_ips:
        return True

    if user_agent and any(bot_signature in user_agent for bot_signature in configured_user_agents):
        return True

    return False


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


class WebsiteVisitorTrackingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        # Skip tracking for excluded paths
        excluded_paths = [
            '/admin/',
            '/admin-dashboard/',
            '/api/',
            '/static/',
            '/media/',
            '/favicon.ico',
            '/manifest.json',
            '/service-worker.js'
        ]
        path = request.path
        if any(path.startswith(excluded) for excluded in excluded_paths):
            return self.get_response(request)
        
        # Only track GET and HEAD requests
        if request.method not in ['GET', 'HEAD']:
            return self.get_response(request)
        
        # Get visitor id from cookie
        visitor_id_str = request.COOKIES.get('website_visitor_id', '')
        visitor = None
        set_cookie = False
        new_visitor_id = None
        client_ip = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')
        site_settings = None
        is_bot = False

        try:
            site_settings = SiteSettings.get_settings()
            is_bot = is_bot_request(request, client_ip, site_settings)
        except Exception:
            site_settings = None
            is_bot = False
        
        try:
            if visitor_id_str:
                # Try to parse UUID
                visitor_id = uuid.UUID(visitor_id_str)
                # Try to get existing visitor
                try:
                    visitor = WebsiteVisitor.objects.get(visitor_id=visitor_id)
                except WebsiteVisitor.DoesNotExist:
                    # Visitor not found, create new
                    new_visitor_id = visitor_id
                    set_cookie = True
            else:
                # No visitor id, create new
                new_visitor_id = uuid.uuid4()
                set_cookie = True
        except ValueError:
            # Invalid UUID, create new
            new_visitor_id = uuid.uuid4()
            set_cookie = True
        
        # Create visitor if needed
        if visitor is None and new_visitor_id is not None:
            visitor = WebsiteVisitor.objects.create(
                visitor_id=new_visitor_id,
                user=request.user if request.user.is_authenticated else None,
                last_path=path,
                total_visits=1,
                last_ip_address=client_ip,
                user_agent=user_agent,
            )
        elif visitor is not None:
            # Update existing visitor
            update_fields = ['last_seen_at', 'total_visits', 'last_path', 'last_ip_address', 'user_agent']
            if request.user.is_authenticated and visitor.user != request.user:
                visitor.user = request.user
                update_fields.append('user')
            visitor.last_path = path
            visitor.total_visits += 1
            visitor.last_ip_address = client_ip
            visitor.user_agent = user_agent
            visitor.save(update_fields=update_fields)
        
        # Record visit
        if visitor is not None:
            WebsiteVisitorVisit.objects.create(
                visitor=visitor,
                path=path,
                ip_address=client_ip,
                is_bot=is_bot,
            )
        
        # Get response
        response = self.get_response(request)
        
        # Set cookie if needed
        if set_cookie and new_visitor_id is not None:
            response.set_cookie(
                'website_visitor_id',
                str(new_visitor_id),
                httponly=True,
                samesite='Lax',
                secure=request.is_secure(),
                max_age=60*60*24*365*2  # 2 years
            )
        
        return response


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Set a permissive CSP that allows embedding external video players
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://image.tmdb.org https://*.codespecters.com; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-src 'self' https://*.vidking.net https://*.vidcore.net https://*.codespecters.com https://*.2embed.cc https://*.vidsrc.me https://*.vidsrc.cc https://*.vidsrc.net https://*.vidsrc.xyz https://*.multiembed.mov https://*.2embed.to https://*.embed.su; "
            "frame-ancestors 'self'; "
        )
        response["Content-Security-Policy"] = csp
        return response
