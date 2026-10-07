
from django.shortcuts import redirect
from django.conf import settings
from django.utils import timezone
from django.core.cache import cache
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


class IPBlockMiddleware:
    """Deny every public request from an IP the admin has blocked.

    Placed LAST in MIDDLEWARE — inside RequestLogMiddleware — so a blocked
    request is still written to the request log. That is deliberate: it is what
    lets the Blocked IPs page show how many requests an address kept making
    after the block and which pages it kept asking for.

    A blocked address is not sealed off completely: the block page shows only
    the sign-in and registration forms, signing in or registering lifts a normal
    block right away, the auth endpoints stay reachable so that is actually
    possible, and staff plus the Django admin are exempt so a mistyped block can
    never lock the operator out of the dashboard.

    The same middleware also watches the request rate: an address that asks for
    far more pages in a few minutes than a person could is treated as a bot and
    blocked, permanently when it was signed in. Scraping again after being
    warned by the block page escalates a block to permanent too.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _hard_exempt(request):
        """True for paths that stay reachable even from a blocked address.

        The dashboard (so a mistyped block can never lock the operator out),
        static and media files, and the site's own sign-in / registration
        endpoints — the block page posts to those, so blocking them would close
        the way out the page offers.
        """
        path = getattr(request, 'path', '') or ''
        if path.startswith('/admin') or path.startswith('/static/') or path.startswith('/media/'):
            return True
        from .ip_block import is_auth_entry
        return is_auth_entry(request)

    @staticmethod
    def _operator(request):
        """True for staff / superuser traffic, which is never rate-watched."""
        try:
            user = getattr(request, 'user', None)
            return bool(user is not None and user.is_authenticated
                        and (user.is_staff or user.is_superuser))
        except Exception:
            return False

    @staticmethod
    def _signed_in(request):
        try:
            user = getattr(request, 'user', None)
            return bool(user is not None and user.is_authenticated)
        except Exception:
            return False

    def __call__(self, request):
        try:
            return self._check(request)
        except Exception:
            # Blocking must never break the site; on any doubt, serve the request.
            return self.get_response(request)

    def _check(self, request):
        from .ip_block import (should_count_for_bot_rate, note_request,
                               auto_block_bot, blocked_ip_for, blocked_response,
                               is_bot_block)
        client_ip = get_client_ip(request)
        signed_in = self._signed_in(request)
        # Watch the request rate *before* the exemptions, so a signed-in
        # scraper is caught too. should_count_for_bot_rate() keeps the app's own
        # user-data polling, assets and the dashboard out of the count, and staff
        # traffic is skipped outright.
        if client_ip and not self._operator(request):
            if should_count_for_bot_rate(getattr(request, 'path', '') or ''):
                if note_request(client_ip):
                    auto_block_bot(client_ip, signed_in=signed_in)
        if self._hard_exempt(request):
            return self.get_response(request)
        if not client_ip:
            return self.get_response(request)
        row = blocked_ip_for(client_ip)
        if row is None:
            return self.get_response(request)
        # A signed-in visitor already followed the block page's way out, so a
        # hand-made block does not stop them — but a bot block, or a permanent
        # one, is not something holding a session gets to bypass.
        if signed_in and not (row.is_permanent or is_bot_block(row)):
            return self.get_response(request)
        return blocked_response(request, client_ip, row)


class EmailSettingsMiddleware:
    """Apply email settings from SiteSettings, but only update Django's global
    settings when values actually change to avoid unnecessary global-state
    mutation on every request.
    """
    _cached_hash = None

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            site_settings = SiteSettings.get_settings()
            if site_settings.email_host_user:
                # Build a hashable snapshot; skip update if nothing changed
                snapshot = (
                    site_settings.email_host,
                    site_settings.email_port,
                    site_settings.email_host_user,
                    site_settings.email_host_password,
                    site_settings.email_use_tls,
                )
                if snapshot != EmailSettingsMiddleware._cached_hash:
                    settings.EMAIL_HOST = site_settings.email_host
                    settings.EMAIL_PORT = site_settings.email_port
                    settings.EMAIL_HOST_USER = site_settings.email_host_user
                    settings.EMAIL_HOST_PASSWORD = site_settings.email_host_password
                    settings.EMAIL_USE_TLS = site_settings.email_use_tls
                    settings.DEFAULT_FROM_EMAIL = site_settings.email_host_user
                    EmailSettingsMiddleware._cached_hash = snapshot
        except Exception:
            pass
        return self.get_response(request)


class WebsiteVisitorTrackingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip tracking for excluded paths
        excluded_paths = (
            '/admin/', '/admin-dashboard/', '/api/', '/static/',
            '/media/', '/favicon.ico', '/manifest.json', '/service-worker.js',
        )
        path = request.path
        if any(path.startswith(p) for p in excluded_paths):
            return self.get_response(request)

        # Only track GET and HEAD requests
        if request.method not in ('GET', 'HEAD'):
            return self.get_response(request)

        client_ip = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')

        # A blocked address still gets served (it will be denied a 403 further
        # down the stack) but is kept out of the visitor tables so it cannot
        # inflate the Active Users list.
        try:
            from .ip_block import is_ip_blocked
            if client_ip and is_ip_blocked(client_ip):
                return self.get_response(request)
        except Exception:
            pass

        # Determine bot status once and early-exit for bots
        is_bot = False
        try:
            is_bot = is_bot_request(request, client_ip, SiteSettings.get_settings())
        except Exception:
            pass

        # Parse or generate visitor id
        set_cookie = False
        new_visitor_id = None
        try:
            visitor_id = uuid.UUID(request.COOKIES.get('website_visitor_id', ''))
        except (ValueError, AttributeError):
            visitor_id = None

        try:
            if visitor_id is not None:
                # Cache the visitor PK so the common case skips the per-request
                # upsert SELECT; the row is still updated atomically by pk.
                # Falls back to a full upsert whenever the cache is cold.
                pk_cache_key = f'wv_pk_{visitor_id.hex}'
                visitor_pk = cache.get(pk_cache_key)
                if visitor_pk is None:
                    from django.db.models import F
                    visitor, _ = WebsiteVisitor.objects.update_or_create(
                        visitor_id=visitor_id,
                        defaults={
                            'user': request.user if request.user.is_authenticated else None,
                            'last_path': path,
                            'total_visits': F('total_visits') + 1,
                            'last_ip_address': client_ip,
                            'user_agent': user_agent,
                        },
                    )
                    visitor_pk = visitor.pk
                    try:
                        cache.set(pk_cache_key, visitor_pk, 600)
                    except Exception:
                        pass
                else:
                    from django.db.models import F
                    WebsiteVisitor.objects.filter(pk=visitor_pk).update(
                        user=request.user if request.user.is_authenticated else None,
                        last_path=path,
                        last_seen_at=timezone.now(),
                        total_visits=F('total_visits') + 1,
                        last_ip_address=client_ip,
                        user_agent=user_agent,
                    )

                # Record visit (skip for bots to reduce noise). Passing the FK id
                # directly avoids the ORM's SELECT on the parent row.
                if not is_bot:
                    WebsiteVisitorVisit.objects.create(
                        visitor_id=visitor_pk,
                        path=path,
                        ip_address=client_ip,
                        is_bot=is_bot,
                    )
            else:
                new_visitor_id = uuid.uuid4()
                set_cookie = True
                visitor = WebsiteVisitor.objects.create(
                    visitor_id=new_visitor_id,
                    user=request.user if request.user.is_authenticated else None,
                    last_path=path,
                    total_visits=1,
                    last_ip_address=client_ip,
                    user_agent=user_agent,
                )

                # Record visit (skip for bots to reduce noise)
                if not is_bot:
                    WebsiteVisitorVisit.objects.create(
                        visitor=visitor,
                        path=path,
                        ip_address=client_ip,
                        is_bot=is_bot,
                    )
        except Exception:
            pass

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


class RequestLogMiddleware:
    """Record every incoming request (page, Ajax and API) for the admin log.

    Placed last in MIDDLEWARE so its timer wraps the whole downstream stack
    (including the error monitor) and measures the true response time. The
    write is best-effort — a logging failure never breaks the response.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        import time
        start = time.monotonic()
        response = self.get_response(request)
        try:
            from .request_log import record_request
            duration_ms = int((time.monotonic() - start) * 1000)
            record_request(request, getattr(response, 'status_code', 0), duration_ms,
                           response=response)
        except Exception:
            pass
        return response


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Set a very permissive CSP to avoid breaking any site features
        csp = (
            "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
            "script-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
            "style-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
            "img-src * 'unsafe-inline' data: blob:; "
            "font-src * 'unsafe-inline' data: blob:; "
            "connect-src * 'unsafe-inline' data: blob:; "
            "frame-src * 'unsafe-inline' data: blob:; "
            "frame-ancestors * 'unsafe-inline'; "
        )
        response["Content-Security-Policy"] = csp
        return response
