"""The middleware behind the admin's "login as" preview.

A preview link carries a session key (``/preview/<key>/...``), so the pages it
opens are served with that session - and therefore as that user - while the
browser's own ``sessionid`` cookie is ignored. That is what makes the preview
work in an incognito window, and what keeps the admin's session in their other
tab exactly as it was.

The key is only honoured when the session carries the ``preview_by`` marker
that ``core.user_chat.login_as_preview`` writes, so a real sign-in session can
never be replayed through a URL.
"""

import re

from django.conf import settings
from django.http import HttpResponse
from django.urls import set_script_prefix
from django.utils.module_loading import import_string

PREVIEW_PATH = re.compile(r'^/preview/([0-9a-fA-F]{32})(?=/|$)')


def session_store():
    """The project's session engine, used to mint and read preview sessions."""
    return import_string(settings.SESSION_ENGINE + '.SessionStore')


class PreviewSessionMiddleware:
    """Serve ``/preview/<key>/...`` with the session that key names.

    ``SCRIPT_NAME`` and the script prefix are set to the same prefix, so every
    ``{% url %}`` in the rendered page stays inside the preview window and the
    admin's pages keep their normal, unprefixed URLs.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        match = PREVIEW_PATH.match(request.path_info)
        if not match:
            return self.get_response(request)

        key = match.group(1)
        try:
            session = session_store()(session_key=key)
            data = session.load()
        except Exception:
            data = None
        if not data or not data.get('_auth_user_id') or not data.get('preview_by'):
            return HttpResponse(
                '<!doctype html><html><head><title>Preview link expired</title>'
                '</head><body style="font-family:system-ui;background:#0b1220;'
                'color:#e2e8f0;padding:2rem;"><h1 style="font-size:1.1rem;">'
                'This preview link has expired</h1><p style="opacity:.7;'
                'font-size:.9rem;">Preview sessions last a few hours. Close this '
                'window and use Login as again from the user list.</p></body>'
                '</html>', status=404)

        prefix = '/preview/%s' % key
        # The path Django resolves inside the preview window. ``request.path``
        # keeps the prefix, so redirects and links built from it stay inside
        # the window too.
        request.path_info = request.path_info[len(prefix):] or '/'
        request.session = session
        request.META['SCRIPT_NAME'] = prefix
        request.preview_session_key = key
        set_script_prefix(prefix + '/')
        try:
            response = self.get_response(request)
        finally:
            set_script_prefix('/')
        # A preview is one person's private pages: never share it through a
        # cache.
        response['Cache-Control'] = 'private, no-store'
        return response
