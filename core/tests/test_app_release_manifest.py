"""The update metadata the config payload carries.

A published build writes app_version.json into the GitHub APK repo. The app
compares its own build against latest_version / latest_build from its config
payload, so that manifest — not a hand-edited copy in the payload — is what
tells every installed build that a newer version exists.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from core import app_release


class RepoDiscoveryTest(TestCase):
    def test_repo_from_raw_apk_url(self):
        payload = {'apk_url': 'https://raw.githubusercontent.com/alok0248/cinestream_apk_beta/main/app-release.apk'}
        self.assertEqual('alok0248/cinestream_apk_beta', app_release.update_repo(payload))

    def test_repo_from_github_download_url(self):
        payload = {'download_url': 'https://github.com/alok0248/cinestream_apk_beta/releases/download/v3.7.024/app.apk'}
        self.assertEqual('alok0248/cinestream_apk_beta', app_release.update_repo(payload))

    def test_declared_repo_wins(self):
        payload = {'ota_github_repo': 'owner/other_repo',
                   'apk_url': 'https://raw.githubusercontent.com/alok0248/cinestream_apk_beta/main/app-release.apk'}
        self.assertEqual('owner/other_repo', app_release.update_repo(payload))

    def test_no_channel(self):
        self.assertEqual('', app_release.update_repo({'api_key': 'x'}))


class PublishedReleaseTest(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def payload(self, build=682, version='v3.4.8.682'):
        return {
            'apk_url': 'https://raw.githubusercontent.com/alok0248/cinestream_apk_beta/main/app-release.apk',
            'latest_version': version,
            'latest_build': build,
            'min_build': 666,
        }

    def test_newer_manifest_replaces_stored_version(self):
        manifest = {'latest_version': 'v3.7.024', 'latest_build': 724,
                    'download_url': 'https://raw.githubusercontent.com/alok0248/cinestream_apk_beta/main/app-release.apk'}
        with mock.patch.object(app_release, '_fetch_manifest', return_value=manifest):
            merged = app_release.with_published_release(self.payload())
        self.assertEqual(724, merged['latest_build'])
        self.assertEqual('v3.7.024', merged['latest_version'])
        self.assertEqual(manifest['download_url'], merged['apk_url'])
        self.assertEqual(666, merged['min_build'])

    def test_older_manifest_never_hides_the_saved_version(self):
        manifest = {'latest_version': 'v3.4.8.600', 'latest_build': 600}
        with mock.patch.object(app_release, '_fetch_manifest', return_value=manifest):
            merged = app_release.with_published_release(self.payload())
        self.assertEqual(682, merged['latest_build'])
        self.assertEqual('v3.4.8.682', merged['latest_version'])

    def test_failed_fetch_serves_the_stored_payload(self):
        with mock.patch.object(app_release, '_fetch_manifest', side_effect=OSError('offline')):
            merged = app_release.with_published_release(self.payload())
        self.assertEqual(682, merged['latest_build'])

    def test_app_without_an_update_channel_is_untouched(self):
        payload = {'api_key': 'x', 'movie_servers': []}
        with mock.patch.object(app_release, '_fetch_manifest') as fetch:
            merged = app_release.with_published_release(payload)
        self.assertEqual(payload, merged)
        fetch.assert_not_called()
