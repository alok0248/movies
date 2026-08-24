"""Middleware to add browser cache headers for better offline/return experience."""


class BrowserCacheMiddleware:
    """Set Cache-Control headers so browsers keep pages cached."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Don't cache admin, AJAX, login, or POST requests
        if request.method != 'GET' or request.path.startswith('/admin') or request.path.startswith('/ajax/'):
            return response
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return response

        # All HTML pages: always revalidate so changes appear on reload
        content_type = response.get('Content-Type', '')
        if 'text/html' in content_type:
            response['Cache-Control'] = 'no-cache, must-revalidate'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'

        return response
