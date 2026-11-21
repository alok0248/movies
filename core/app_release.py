"""The version every installed build is told about.

Publishing a build writes ``app_version.json`` (version, build number and APK
URL) into the app's GitHub APK repo, and each release push replaces it. The
config payload the apps poll used to carry its own hand-edited copy of those
fields, which went stale as soon as a new build was published — the app then
compared itself against an old version and never offered the newer one.

``with_published_release()`` reads that manifest and merges it over the payload
for an app that already declares an update channel (an ``apk_url``,
``download_url``, ``latest_version`` or a repo). An app with no update channel
is left exactly as the operator saved it, and a failed fetch serves the stored
values instead of taking the config away.
"""
import json
import re
import urllib.request

from django.core.cache import cache

MANIFEST_BRANCH = 'main'
MANIFEST_TTL = 300          # fresh answers for five minutes
MANIFEST_FAILURE_TTL = 60   # retry a broken fetch a minute later, not every poll
MANIFEST_URL = 'https://raw.githubusercontent.com/{repo}/{branch}/app_version.json'
FETCH_TIMEOUT = 5

UPDATE_KEYS = ('latest_version', 'latest_build', 'apk_url', 'download_url')


def _repo_from_url(url):
    """``owner/repo`` from a GitHub release URL, or '' for anything else."""
    if not isinstance(url, str) or 'github' not in url:
        return ''
    match = re.search(r'raw\.githubusercontent\.com/([^/]+)/([^/]+)/', url)
    if match is None:
        match = re.search(r'github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/|$)', url)
    if match is None:
        return ''
    return '%s/%s' % (match.group(1), match.group(2))


def update_repo(payload):
    """The GitHub repo this app's payload points at for its builds."""
    if not isinstance(payload, dict):
        return ''
    declared = payload.get('ota_github_repo') or payload.get('github_repo') or ''
    if isinstance(declared, str) and declared.strip():
        declared = declared.strip()
        if 'github' in declared:
            return _repo_from_url(declared)
        return declared.strip('/')
    for key in ('download_url', 'apk_url'):
        repo = _repo_from_url(payload.get(key))
        if repo:
            return repo
    return ''


def _fetch_manifest(repo):
    """Read the published manifest. Kept separate so tests can replace it."""
    request = urllib.request.Request(
        MANIFEST_URL.format(repo=repo, branch=MANIFEST_BRANCH),
        headers={'User-Agent': 'CineStream-Website'},
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return json.loads(response.read(65536).decode('utf-8'))


def _as_build(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = re.sub(r'\D', '', value)
        return int(digits) if digits else 0
    return 0


def published_release(payload):
    """``{latest_version, latest_build, apk_url, download_url}``, or {}."""
    repo = update_repo(payload)
    if not repo:
        return {}
    cache_key = 'app_release:%s' % repo
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        manifest = _fetch_manifest(repo)
    except Exception:
        cache.set(cache_key, {}, MANIFEST_FAILURE_TTL)
        return {}
    if not isinstance(manifest, dict):
        cache.set(cache_key, {}, MANIFEST_FAILURE_TTL)
        return {}

    version = str(manifest.get('latest_version') or manifest.get('version') or '').strip()
    build = _as_build(manifest.get('latest_build', manifest.get('build')))
    apk_url = str(manifest.get('download_url') or manifest.get('apk_url') or '').strip()
    if not apk_url and build:
        apk_url = 'https://raw.githubusercontent.com/%s/%s/app-release.apk' % (
            repo, MANIFEST_BRANCH)
    if not version and not build and not apk_url:
        cache.set(cache_key, {}, MANIFEST_FAILURE_TTL)
        return {}

    release = {
        'latest_version': version,
        'latest_build': build,
        'apk_url': apk_url,
        'download_url': apk_url,
    }
    cache.set(cache_key, release, MANIFEST_TTL)
    return release


def with_published_release(payload):
    """Merge the published build over a stored config payload.

    Only an app that already has an update channel is touched, and only when
    the manifest is newer than what the payload advertises: a manifest that is
    older (or unreadable) never hides the version an operator saved.
    """
    if not isinstance(payload, dict):
        return payload
    if not any(key in payload for key in UPDATE_KEYS):
        return payload
    release = published_release(payload)
    if not release:
        return payload

    stored_build = _as_build(payload.get('latest_build'))
    release_build = _as_build(release.get('latest_build'))
    if release_build <= stored_build:
        return payload

    merged = dict(payload)
    merged['latest_build'] = release_build
    if release.get('latest_version'):
        merged['latest_version'] = release['latest_version']
    if release.get('apk_url'):
        merged['apk_url'] = release['apk_url']
        merged['download_url'] = release['apk_url']
    return merged
