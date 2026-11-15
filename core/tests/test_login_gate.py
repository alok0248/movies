"""The site-wide "sign in to browse" switch.

Off by default, so nothing changes until an admin asks for it. When it is on,
every page must bounce an anonymous visitor to the sign-in prompt — exactly
once. The prompt is a modal on the home page opened by ``login_required`` in
the query string, so a request carrying that flag has to pass; otherwise the
gate and /login/ redirect to each other forever and nobody can sign in.
"""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from core.models import SiteSettings


class LoginRequiredMiddlewareTest(TestCase):
    def setUp(self):
        self.settings_row = SiteSettings.get_settings()
        self.settings_row.require_login_to_browse = True
        self.settings_row.save()
        self.client = Client()

    def tearDown(self):
        self.settings_row.require_login_to_browse = False
        self.settings_row.save()

    def test_a_page_redirects_to_the_sign_in_prompt_once(self):
        response = self.client.get('/movies/', follow=True)
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.redirect_chain))
        self.assertIn('login_required=true', response.redirect_chain[0][0])

    def test_the_prompt_itself_is_served(self):
        response = self.client.get('/?login_required=true&next=/movies/')
        self.assertEqual(200, response.status_code)

    def test_the_sign_in_and_admin_routes_stay_open(self):
        # /login/ is itself a redirect to the prompt and /admin/ has its own
        # sign-in; both must settle on a page instead of bouncing forever.
        for path in ['/login/', '/admin/']:
            with self.subTest(path=path):
                response = self.client.get(path, follow=True)
                self.assertEqual(200, response.status_code)
                self.assertLessEqual(len(response.redirect_chain), 2)

    def test_the_app_api_answers_instead_of_the_gate(self):
        # 401 (no email given) proves the request reached the endpoint.
        self.assertEqual(401, self.client.get('/api/messages/').status_code)

    def test_a_signed_in_visitor_browses_normally(self):
        User.objects.create_user('gate-user', 'gate@example.invalid', 'pw-123456')
        self.client.login(username='gate-user', password='pw-123456')
        response = self.client.get('/movies/')
        self.assertEqual(200, response.status_code)

    def test_turning_the_switch_off_takes_effect_at_once(self):
        # The gate keeps a short cache of its own; a save has to clear it or
        # the page would keep asking for a sign-in after the admin turned it off.
        self.settings_row.require_login_to_browse = False
        self.settings_row.save()
        self.assertEqual(200, self.client.get('/movies/').status_code)

    def test_the_other_gates_own_screens_stay_reachable(self):
        # The human gate sends a locked visitor to /verify/; if this gate then
        # bounced /verify/ back to the sign-in prompt the two would deadlock and
        # nobody could ever pass either. Each such screen must not be answered
        # with the sign-in prompt.
        for path in ('/verify/', '/security-status/', '/staff-otp/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotIn('login_required=true', response.get('Location', ''))
        self.assertEqual(200, self.client.get('/security-status/').status_code)

    def test_the_prompt_says_why_a_login_is_needed(self):
        # Once on the page under the navbar, once inside the prompt itself,
        # because the prompt is a modal that covers the page.
        response = self.client.get('/?login_required=true&next=/movies/')
        self.assertContains(response, 'bot extraction activity', count=2)

    def test_a_signed_in_visitor_is_not_told_why(self):
        User.objects.create_user('gate-user', 'gate@example.invalid', 'pw-123456')
        self.client.login(username='gate-user', password='pw-123456')
        response = self.client.get('/movies/')
        self.assertNotContains(response, 'bot extraction activity')


class LoginRequiredOffTest(TestCase):
    def test_pages_stay_public_while_the_switch_is_off(self):
        self.assertFalse(SiteSettings.get_settings().require_login_to_browse)
        self.assertEqual(200, Client().get('/movies/').status_code)
