"""Middleware to add browser cache headers for better offline/return experience."""


class BrowserCacheMiddleware:
    """Set Cache-Control headers so browsers keep pages cached."""

    # Pages that can be cached in the browser
    CACHEABLE_PATHS = ('/movies/', '/series/', '/collection/', '/upcoming/', '/search/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Don't cache admin, AJAX, login, or POST requests
        if request.method != 'GET' or request.path.startswith('/admin') or request.path.startswith('/ajax/'):
            return response
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return response

        path = request.path
        # Detail pages: cache for 1 hour
        if any(path.startswith(p) for p in self.CACHEABLE_PATHS):
            response['Cache-Control'] = 'public, max-age=3600, stale-while-revalidate=86400'
        # Home page: cache for 10 minutes
        elif path == '/':
            response['Cache-Control'] = 'public, max-age=600, stale-while-revalidate=3600'

        return response
