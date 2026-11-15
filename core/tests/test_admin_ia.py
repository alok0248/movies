"""Admin information architecture: consolidated settings URLs and the shell.

The 13 legacy one-panel settings pages were collapsed into the four hubs, so
each old URL must now redirect to the matching hub tab (never 404). The shell
gained a command palette; a render test keeps it in place.
"""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from core.models import ErrorLog


class LegacySettingsRedirectTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw', is_staff=True)
        self.client = Client()
        self.client.force_login(self.staff)

    def test_each_legacy_url_redirects_to_its_hub_tab(self):
        cases = {
            '/admin-dashboard/settings/general/': '/admin-dashboard/settings/site/?tab=general',
            '/admin-dashboard/settings/branding/': '/admin-dashboard/settings/site/?tab=branding',
            '/admin-dashboard/settings/display/': '/admin-dashboard/settings/site/?tab=display',
            '/admin-dashboard/settings/footer/': '/admin-dashboard/settings/site/?tab=footer',
            '/admin-dashboard/settings/data-source/': '/admin-dashboard/settings/data/?tab=source',
            '/admin-dashboard/settings/tmdb-db/': '/admin-dashboard/settings/data/?tab=tmdb',
            '/admin-dashboard/settings/data-source-config/': '/admin-dashboard/settings/data/?tab=keys',
            '/admin-dashboard/settings/data-source-usage-stats/': '/admin-dashboard/settings/data/?tab=usage',
            '/admin-dashboard/settings/db-connections/': '/admin-dashboard/settings/data/?tab=db',
            '/admin-dashboard/settings/db-routing/': '/admin-dashboard/settings/data/?tab=routing',
            '/admin-dashboard/settings/player/': '/admin-dashboard/settings/delivery/?tab=player',
            '/admin-dashboard/settings/email/': '/admin-dashboard/settings/delivery/?tab=email',
            '/admin-dashboard/settings/security-protection/': '/admin-dashboard/settings/security/?tab=protection',
        }
        for old, expected in cases.items():
            with self.subTest(old=old):
                response = self.client.get(old)
                self.assertEqual(302, response.status_code)
                self.assertEqual(expected, response['Location'])

    def test_the_retired_settings_page_lands_on_the_settings_home(self):
        response = self.client.get('/admin-dashboard/edit-settings/')
        self.assertEqual(302, response.status_code)
        self.assertEqual('/admin-dashboard/settings/overview/', response['Location'])


class AdminShellTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw', is_staff=True)
        self.client = Client()
        self.client.force_login(self.staff)

    def test_the_dashboard_renders_with_the_command_palette(self):
        response = self.client.get('/admin-dashboard/')
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'id="adminPalette"')

    def test_the_sidebar_exposes_every_hub(self):
        response = self.client.get('/admin-dashboard/')
        for hub in ('/admin-dashboard/settings/site/', '/admin-dashboard/settings/data/',
                    '/admin-dashboard/settings/delivery/', '/admin-dashboard/settings/security/'):
            with self.subTest(hub=hub):
                self.assertContains(response, hub)

    def test_a_hub_tab_renders(self):
        for url in ('/admin-dashboard/settings/security/?tab=protection',
                    '/admin-dashboard/settings/site/?tab=general',
                    '/admin-dashboard/settings/data/?tab=keys',
                    '/admin-dashboard/settings/delivery/?tab=player'):
            with self.subTest(url=url):
                self.assertEqual(200, self.client.get(url).status_code)


class HealthViewTest(TestCase):
    def test_health_serialises_an_error_row_instead_of_crashing(self):
        # recent_errors() yields ErrorLog rows; treating one as a dict used to
        # raise AttributeError and turn /api/health into a 500 whenever any
        # error had ever been logged.
        ErrorLog.objects.create(kind='api', status_code=500, message='boom')
        response = self.client.get('/api/health')
        self.assertIn(response.status_code, (200, 503))
        payload = response.json()
        self.assertGreaterEqual(payload['api_error_count'], 1)
        self.assertEqual('boom', payload['last_api_error']['message'])