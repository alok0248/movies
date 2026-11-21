"""The short name a link should be shown under.

A notification can carry a link, and the admin often writes the URL straight
into the message. Neither should read as a 120-character address: the reader
sees the file or page name and tapping it opens the real URL in the browser.
"""
import re
from urllib.parse import unquote, urlsplit

from django.utils.html import escape
from django.utils.safestring import mark_safe

URL_RE = re.compile(r'https?://[^\s<>"\'`]+', re.IGNORECASE)
MAX_LABEL = 48
TRAILING_PUNCTUATION = '.,;:)]}\'"'


def clean_url(url):
    """Strip the punctuation a URL picks up from the sentence around it."""
    cleaned = (url or '').strip()
    while cleaned and cleaned[-1] in TRAILING_PUNCTUATION:
        cleaned = cleaned[:-1]
    return cleaned


def link_label(url):
    """``app.apk`` for a file, ``example.com`` for a site root, '' for no link."""
    cleaned = clean_url(url)
    if not cleaned:
        return ''
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return cleaned[:MAX_LABEL]
    name = unquote((parts.path or '').rstrip('/').rsplit('/', 1)[-1]).strip()
    if name:
        return name[:MAX_LABEL]
    host = (parts.netloc or '').strip()
    if host:
        if host.lower().startswith('www.'):
            host = host[4:]
        return host[:MAX_LABEL]
    return cleaned[:MAX_LABEL]


def linkify(text):
    """Escape the message and turn every URL in it into a named link.

    The URL keeps its place in the sentence but is shown under its name, so a
    long address never pushes the text around or has to be read in full.
    """
    source = text or ''
    pieces, cursor = [], 0
    for match in URL_RE.finditer(source):
        url = clean_url(match.group(0))
        if not url:
            continue
        pieces.append(escape(source[cursor:match.start()]))
        pieces.append(
            '<a class="nt-link" href="%s" target="_blank" rel="noopener noreferrer" '
            'title="%s"><i class="fas fa-link me-1"></i>%s</a>'
            % (escape(url), escape(url),
               escape(link_label(url))))
        cursor = match.end()
    pieces.append(escape(source[cursor:]))
    return mark_safe(''.join(pieces))
