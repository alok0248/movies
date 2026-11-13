"""Middleware to add browser cache headers for better offline/return experience."""


class BrowserCacheMiddleware:
    """Set Cache-Control headers so browsers keep pages cached."""

    def __init__(self, get_response):
        self.get_response = get_response

    # Paths that must never be stored by a browser or a shared cache: they
    # answer with account data (API/AJAX/admin) or are already revalidated per
    # request. Static and media files keep their own long-lived headers.
    PRIVATE_PREFIXES = ('/admin', '/ajax/', '/api/')

    def __call__(self, request):
        response = self.get_response(request)

        # A response that already asked not to be stored at all (the blocked-IP
        # page, downloads) keeps its own header — never soften it into a
        # revalidating one.
        if 'no-store' in (response.get('Cache-Control') or ''):
            return response

        # Static/media assets are not account data and keep their own caching
        # (nginx serves them directly in production anyway).
        if request.path.startswith(('/static/', '/media/')):
            return response

        # HEAD is handled like GET so a HEAD probe reports the same caching
        # policy as the body response.
        is_private = (
            request.method not in ('GET', 'HEAD')
            or request.path.startswith(self.PRIVATE_PREFIXES)
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or getattr(getattr(request, 'user', None), 'is_authenticated', False)
        )
        if is_private:
            # Personalised or per-request responses: `no-store` keeps them out
            # of the browser cache and out of any intermediary cache, so a
            # shared device or proxy can never replay another user's pages.
            response['Cache-Control'] = 'private, no-store'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'
            return response

        # Public HTML pages: always revalidate so changes appear on reload
        content_type = response.get('Content-Type', '')
        if 'text/html' in content_type:
            response['Cache-Control'] = 'no-cache, must-revalidate'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'

        return response
