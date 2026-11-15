"""Who may open the Android app download page (/get-app/).

Two switches on SiteSettings decide it — public access and the login
requirement. The promised behaviour:

* public on            -> anyone, signed in or not
* public off, login on -> only signed-in visitors; everyone else is sent to the
                          sign-in screen
* both off             -> nobody, and the page says so
* both on              -> public wins, so turning the login switch on never
                          takes the page away from people who already had it

A change must bite on the next request, without a restart, and a posted value
that is not a real on/off state must be refused instead of guessed at.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from core.download_access import (CLOSED, LOGIN, PUBLIC, InvalidToggle,
                                  download_access_state, parse_toggle)
from core.models import SiteSettings


def set_access(*, public, login):
    """Store the two switches the way the admin page does, then clear the cache."""
    site = SiteSettings.get_settings()
    site.app_download_public_access = public
    site.app_download_login_required = login
    site.save()
    cache.delete(SiteSettings._DOWNLOAD_ACCESS_CACHE_KEY)


class ToggleParsingTest(TestCase):
    """Input validation: only a real enable/disable state is stored."""

    def test_the_accepted_states(self):
        for raw, expected in [('on', True), ('1', True), ('true', True),
                              ('off', False), ('0', False), ('', False),
                              (None, False), (' ON ', True)]:
            with self.subTest(raw=raw):
                self.assertEqual(expected, parse_toggle(raw))

    def test_anything_else_is_refused(self):
        for raw in ['maybe', '2', 'yes-please', 'null', 'checked=False']:
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidToggle):
                    parse_toggle(raw)


class DownloadPageAccessTest(TestCase):
    def setUp(self):
        # Start from a known state for every test: the database row and the
        # five-second decision cache both.
        cache.clear()
        self.site = SiteSettings.get_settings()
        self.site.app_download_public_access = True
        self.site.app_download_login_required = False
        self.site.save()
        cache.delete(SiteSettings._DOWNLOAD_ACCESS_CACHE_KEY)
        self.anonymous = Client()
        self.fan = User.objects.create_user('fan', 'fan@example.invalid', 'pw-123456')
        self.signed_in = Client()
        self.signed_in.force_login(self.fan)

    # ------------------------------------------------------------------ states
    def test_public_lets_everyone_in(self):
        self.assertEqual(PUBLIC, download_access_state())
        self.assertEqual(200, self.anonymous.get('/get-app/').status_code)
        self.assertEqual(200, self.signed_in.get('/get-app/').status_code)

    def test_login_required_blocks_a_signed_out_visitor(self):
        set_access(public=False, login=True)
        self.assertEqual(LOGIN, download_access_state())
        response = self.anonymous.get('/get-app/')
        self.assertEqual(302, response.status_code)
        self.assertIn('login_required=true', response['Location'])
        self.assertIn('next=%2Fget-app%2F', response['Location'])

    def test_login_required_admits_a_signed_in_visitor(self):
        set_access(public=False, login=True)
        response = self.signed_in.get('/get-app/')
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'Sign-in required')

    def test_both_switches_off_closes_the_page_to_everyone(self):
        set_access(public=False, login=False)
        self.assertEqual(CLOSED, download_access_state())
        for who, client in [('anonymous', self.anonymous), ('signed in', self.signed_in)]:
            with self.subTest(who=who):
                response = client.get('/get-app/')
                self.assertEqual(403, response.status_code)
                self.assertContains(response, 'download page is closed', status_code=403)

    def test_both_switches_on_lets_public_access_win(self):
        set_access(public=True, login=True)
        self.assertEqual(PUBLIC, download_access_state())
        self.assertEqual(200, self.anonymous.get('/get-app/').status_code)

    def test_the_page_tells_the_visitor_it_is_public(self):
        response = self.anonymous.get('/get-app/')
        self.assertContains(response, 'Public page')

    # ------------------------------------------------------------------ timing
    def test_a_change_bites_on_the_next_request(self):
        # Cached for a few seconds per worker, so this proves the save clears
        # that cache rather than the switch merely being stored.
        self.assertEqual(200, self.anonymous.get('/get-app/').status_code)
        set_access(public=False, login=False)
        self.assertEqual(403, self.anonymous.get('/get-app/').status_code)
        set_access(public=True, login=False)
        self.assertEqual(200, self.anonymous.get('/get-app/').status_code)

    # ------------------------------------------------------------------ storage
    def test_the_values_are_stored(self):
        set_access(public=False, login=True)
        row = SiteSettings.objects.get(pk=1)
        self.assertFalse(row.app_download_public_access)
        self.assertTrue(row.app_download_login_required)


class DownloadAccessAdminTest(TestCase):
    """The control panel on the APK Downloads page."""

    def setUp(self):
        cache.clear()
        # The row has to exist before the test can compare before/after values.
        SiteSettings.get_settings()
        self.staff = User.objects.create_superuser('boss', 'boss@example.invalid', 'pw-123456')
        self.client = Client()
        self.client.force_login(self.staff)
        self.url = reverse('android_app_downloads')

    def _post(self, public=None, login=None):
        payload = {}
        if public is not None:
            payload['app_download_public_access'] = public
        if login is not None:
            payload['app_download_login_required'] = login
        return self.client.post(self.url, payload, follow=True)

    def test_the_panel_shows_the_current_status(self):
        response = self.client.get(self.url)
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'Download page access')
        self.assertContains(response, 'Current:')

    def test_the_switches_are_saved_and_take_effect(self):
        self._post(public='off', login='on')
        row = SiteSettings.objects.get(pk=1)
        self.assertFalse(row.app_download_public_access)
        self.assertTrue(row.app_download_login_required)
        self.assertEqual(302, Client().get('/get-app/').status_code)

    def test_an_invalid_value_is_refused_and_nothing_changes(self):
        before_public = SiteSettings.objects.get(pk=1).app_download_public_access
        before_login = SiteSettings.objects.get(pk=1).app_download_login_required
        response = self._post(public='sometimes', login='on')
        self.assertContains(response, 'only accept an on/off state')
        row = SiteSettings.objects.get(pk=1)
        self.assertEqual(before_public, row.app_download_public_access)
        self.assertEqual(before_login, row.app_download_login_required)

    def test_a_non_staff_visitor_cannot_reach_the_panel(self):
        self.client.logout()
        response = Client().get(self.url)
        self.assertIn(response.status_code, (301, 302))