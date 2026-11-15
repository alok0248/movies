
import logging

from django.shortcuts import redirect
from django.conf import settings
from django.utils import timezone
from django.core.cache import cache
from .models import SiteSettings, WebsiteVisitor, WebsiteVisitorVisit
import uuid

logger = logging.getLogger(__name__)


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
            from .request_log import record_request, should_record
            duration_ms = int((time.monotonic() - start) * 1000)
            record_request(request, getattr(response, 'status_code', 0), duration_ms,
                           response=response)
            # Device <-> IP history, with the country lookup queued as traffic
            # arrives so the admin pages never wait on ipwho.is.
            if should_record(request):
                from .ip_tracking import record as record_device_ip
                record_device_ip(request, user=getattr(request, 'user', None))
        except Exception:
            pass
        return response


class ContentSecurityPolicyMiddleware:
    """Clickjacking protection via CSP, with everything else left open.

    The policy is deliberately permissive: scripts, styles, images, frames and
    connections from any origin (including inline and eval) stay allowed, so
    the third-party players, ad iframes and extractors keep working exactly as
    before. The only restriction is ``frame-ancestors 'self'`` — nobody but
    this site may frame our pages — which browsers honour even where
    X-Frame-Options is ignored. Views that set their own policy (e.g.
    ``proxy_embed``) keep it.
    """

    POLICY = (
        "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "script-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "style-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "img-src * data: blob:; "
        "font-src * data: blob:; "
        "media-src * data: blob:; "
        "connect-src * data: blob: ws: wss:; "
        "frame-src * data: blob:; "
        "frame-ancestors 'self'; "
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if 'Content-Security-Policy' not in response:
            response['Content-Security-Policy'] = self.POLICY
        return response


class HumanVerificationMiddleware:
    """Layer 1: hold every HTML visitor until they solve the Turnstile challenge.

    Sits immediately after SessionMiddleware, so the solved-challenge mark is
    available and nothing later in the stack has run.

    What gets through untouched:

    * anything while the gate is unconfigured — no keys, no gate, so a deploy
      that lands before the keys are added cannot lock the site;
    * a session that solved the challenge within the last 24 hours;
    * the challenge page itself, static and media files, Django's admin, and the
      whole ``/api/`` surface plus ``/apps/download/`` — the Android app and the
      API clients never see a redirect they cannot follow;
    * Google's crawlers, because losing search indexing is not an acceptable
      side effect. Every other crawler has to solve the challenge, which a
      crawler cannot, so only Google is indexed.

    An unverified browser is redirected to ``/verify/?next=<path>``; an
    unverified state-changing request (POST/PUT/PATCH/DELETE) on a non-API route
    is refused with 403 instead, because silently redirecting a POST would lose
    the body.
    """

    SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .human_verification import (is_app_request, is_configured,
                                         is_google_crawler, is_human, skip_path)

        if not is_configured() or is_human(request):
            return self.get_response(request)
        if skip_path(request) or is_app_request(request):
            return self.get_response(request)
        if is_google_crawler(request.META.get('HTTP_USER_AGENT', '')):
            return self.get_response(request)

        if request.method in self.SAFE_METHODS:
            from django.utils.http import urlencode
            return redirect('/verify/?' + urlencode({'next': request.get_full_path()}))

        # A POST cannot be answered with a redirect without losing its body, so
        # it is refused. AJAX callers get JSON so the page's own error handling
        # can show it instead of choking on HTML.
        from django.http import HttpResponse, JsonResponse

        path = getattr(request, 'path', '') or ''
        if path.startswith('/ajax/') or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'code': 'human_verification_required',
                'message': 'Please complete the verification challenge and try again.',
            }, status=403)
        return HttpResponse(
            '<!doctype html><meta charset="utf-8">'
            '<title>Verification required</title>'
            '<p>Please complete the verification challenge before continuing.</p>'
            '<p><a href="/verify/">Open the verification page</a></p>',
            status=403, content_type='text/html; charset=utf-8')


class EmailVerificationMiddleware:
    """Show an unconfirmed account nothing but the confirmation screen.

    Signing in is what sets the session cookie, and both registration flows
    leave a new account inactive until the mailed code is typed back, so in the
    ordinary course an unconfirmed account cannot sign in at all. This is the
    backstop for the other ways a session can exist — an administrator
    switching an account on by hand, or a confirmation being withdrawn — and it
    is what makes the rule hold for *every* page instead of only the sign-in
    form.

    Until the address is confirmed the visitor gets the confirmation screen and
    nothing else: the code box, a way to ask for a new code, and the sign-in /
    registration forms. Staff and superusers are exempt, because their mailbox
    may be gone and the dashboard must always open; the machine surfaces — the
    app's /api/, Django's own admin, static and media files, and the sign-in
    endpoints the screen itself posts to — pass straight through, so nothing
    else is disturbed.

    Runs after AuthenticationMiddleware, so request.user is already resolved,
    and before LoginRequiredMiddleware, so a half-registered visitor is shown
    the confirmation screen rather than the "sign in to browse" prompt.
    """

    # Reached even while unconfirmed, matched on the start of the path. The
    # sign-in, registration, OTP and resend endpoints all live under /ajax/,
    # so the confirmation screen could not work without it.
    OPEN_PREFIXES = (
        '/ajax/', '/api/', '/admin/', '/static/', '/media/',
        '/login/', '/logout/', '/reset-password/',
        '/favicon.ico', '/robots.txt', '/sitemap.xml', '/manifest.json',
        '/sw.js', '/service-worker.js',
        # The install page asks for no account at all.
        '/get-app/',
    )
    # A page that asks for one of its own parts (the dashboard's tab strips and
    # other fragment loads) must not be answered with the full screen.
    OPEN_PARAMS = ('partial',)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return self.get_response(request)
        if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
            return self.get_response(request)

        from .email_verification import needs_email_verification

        # needs_email_verification() answers "no" for an unauthenticated user
        # and fails closed (treats an unreadable profile as unconfirmed), so
        # this never needs its own exception handling.
        if not needs_email_verification(user):
            return self.get_response(request)
        if request.path_info.startswith(self.OPEN_PREFIXES):
            return self.get_response(request)
        if any(param in request.GET for param in self.OPEN_PARAMS):
            return self.get_response(request)

        from django.shortcuts import render
        return render(request, 'core/verification_required.html', status=403)


class StaffOtpMiddleware:
    """Layer 2 of the admin hardening: an emailed code before the dashboard.

    The password alone is no longer enough for a staff/superuser account. Until
    the second factor is satisfied, every ``/admin-dashboard/`` and ``/admin/``
    request is sent to ``/staff-otp/`` to enter the code mailed to the account's
    own address.

    Off unless ``settings.STAFF_OTP_REQUIRED`` is on (the test runner forces it
    off), and it never touches public pages or ``/api/``. A staff row with no
    email, or an SMTP outage with fail-open on, is let through rather than
    stranded — see core.staff_mfa.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .staff_mfa import challenge_needed

        if challenge_needed(request):
            from django.utils.http import urlencode
            return redirect('/staff-otp/?' + urlencode({'next': request.get_full_path()}))
        return self.get_response(request)


class LoginRequiredMiddleware:
    """Hold back every page until the visitor signs in.

    Off unless "Require sign-in to browse" is ticked on Settings → Security →
    Protection, so the site browses as it always has until an admin asks for
    it. Only pages are gated: the sign-in and password-reset flows, Django's
    own admin sign-in, the mobile app's /api/ endpoints, and static and media
    files stay open — otherwise nobody could reach the form, and the form
    could not load its own CSS.

    The sign-in form is a modal on the home page, opened by ``login_required``
    in the query string (that is where /login/ redirects), so a request that
    carries it must pass or the two would bounce off each other forever.

    Runs after AuthenticationMiddleware, so request.user is already resolved.
    """

    # Kept open while the site is locked, matched on the start of the path.
    OPEN_PREFIXES = (
        '/login/', '/logout/', '/ajax/', '/reset-password/',
        '/api/', '/admin/', '/static/', '/media/',
        '/sw.js', '/robots.txt', '/sitemap.xml', '/favicon.ico',
        '/manifest.json', '/service-worker.js',
        # The Android app's install page has its own pair of switches
        # (Settings → download page access, read through core/download_access),
        # so the site-wide gate must not second-guess it here.
        '/get-app/',
    )
    # Query flags that mean "this request is the sign-in prompt".
    OPEN_FLAGS = ('login_required', 'register')
    # How long a worker may reuse the flag before it has to look again. The
    # settings row itself is cached for a minute, which is far too long here:
    # turning the gate on would look broken on every worker that did not
    # handle the save.
    LOCK_CACHE_SECONDS = 5

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_locked(self):
        locked = cache.get(SiteSettings._LOGIN_GATE_CACHE_KEY)
        if locked is None:
            locked = bool(SiteSettings.get_settings().require_login_to_browse)
            cache.set(SiteSettings._LOGIN_GATE_CACHE_KEY, locked, self.LOCK_CACHE_SECONDS)
        return locked

    def __call__(self, request):
        if getattr(request.user, 'is_authenticated', False):
            return self.get_response(request)
        try:
            locked = self._is_locked()
        except Exception:
            # A settings row that cannot be read must never lock the site out.
            logger.debug('login-required: could not read the setting', exc_info=True)
            locked = False
        if not locked:
            return self.get_response(request)
        if request.path_info.startswith(self.OPEN_PREFIXES):
            return self.get_response(request)
        if any(flag in request.GET for flag in self.OPEN_FLAGS):
            return self.get_response(request)
        from django.utils.http import urlencode
        target = '/?' + urlencode({'login_required': 'true',
                                   'next': request.get_full_path()})
        return redirect(target)
