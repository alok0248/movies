"""
CinePlayer views — serve the browser-based player page.
All API calls go through the browser service worker (sw-proxy.js).
No server-side proxy involved.
"""

from django.shortcuts import render
from django.views.decorators.http import require_GET
import os

GATEWAY = os.environ.get('CINEPLAYER_GATEWAY', 'http://127.0.0.1:8787')


@require_GET
def cineplayer_index(request):
    """Render the CinePlayer page. All API calls happen in the browser."""
    return render(request, 'cineplayer/player.html', {
        'gateway': GATEWAY,
    })
