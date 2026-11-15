"""Sign-in is refused until the email address has been confirmed.

The rule lives on ``UserProfile.email_verified`` and is read through
``core.email_verification``. These tests pin down each half of the promise:

* a right password on an unconfirmed account never produces a session, and the
  visitor is told to confirm the address rather than shown a blank failure;
* a session that somehow exists for an unconfirmed account reaches nothing but
  the confirmation screen — every protected page answers with that screen;
* confirming the code (typed, linked or sent by the app) is what turns the
  account on, and only then does sign-in succeed.

The install page is checked too, because it is the one public page that has to
stay open while the site-wide sign-in gate is on.
"""
import base64
import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from core.email_verification import (is_email_verified, mark_email_verified,
                                     needs_email_verification)
from core.models import (AndroidApp, EmailVerification, SiteSettings,
                         UserProfile)

PASSWORD = 'pw-123456'


def make_user(username, email, *, active=True, verified=None, staff=False):
    """An account, optionally carrying a profile whose flag says ``verified``."""
    user = User.objects.create_user(username=username, email=email, password=PASSWORD)
    user.is_active = active
    user.is_staff = staff
    user.save(update_fields=['is_active', 'is_staff'])
    if verified is not None:
        UserProfile.objects.create(user=user, email_verified=verified)
    return user


class EmailVerificationRuleTest(TestCase):
    """The single answer to "may this account sign in?"."""

    def test_an_anonymous_visitor_is_never_verified(self):
        self.assertFalse(is_email_verified(None))
        self.assertFalse(needs_email_verification(None))

    def test_staff_are_always_verified(self):
        # The dashboard must open even if the mailbox is gone.
        user = make_user('staffer', 'staff@example.invalid', verified=False, staff=True)
        self.assertTrue(is_email_verified(user))
        self.assertFalse(needs_email_verification(user))

    def test_an_active_account_without_a_profile_is_grandfathered(self):
        # Google sign-in, the app sync and admin-created accounts all arrive
        # active and without a profile row, and all trust the address.
        user = make_user('legacy', 'legacy@example.invalid')
        self.assertTrue(is_email_verified(user))

    def test_an_inactive_account_without_a_profile_is_not_verified(self):
        # What both registration flows produce: created, left switched off.
        user = make_user('pending', 'pending@example.invalid', active=False)
        self.assertFalse(is_email_verified(user))
        self.assertTrue(needs_email_verification(user))

    def test_the_profile_flag_decides_when_a_row_exists(self):
        confirmed = make_user('confirmed', 'confirmed@example.invalid', verified=True)
        self.assertTrue(is_email_verified(confirmed))
        unconfirmed = make_user('unconfirmed', 'unconfirmed@example.invalid',
                                active=True, verified=False)
        self.assertFalse(is_email_verified(unconfirmed))

    def test_marking_verified_creates_the_row_and_stamps_the_time(self):
        user = make_user('marker', 'marker@example.invalid', active=False)
        self.assertFalse(UserProfile.objects.filter(user_id=user.pk).exists())
        mark_email_verified(user)
        profile = UserProfile.objects.get(user_id=user.pk)
        self.assertTrue(profile.email_verified)
        self.assertIsNotNone(profile.email_verified_at)
        self.assertTrue(is_email_verified(user))

    def test_marking_twice_is_harmless(self):
        user = make_user('twice', 'twice@example.invalid', verified=True)
        before = UserProfile.objects.get(user_id=user.pk).email_verified_at
        mark_email_verified(user)
        self.assertEqual(before, UserProfile.objects.get(user_id=user.pk).email_verified_at)


class WebLoginTest(TestCase):
    """The sign-in form must refuse an unconfirmed address, with a clear reason."""

    def setUp(self):
        self.client = Client()

    def _login(self, identifier):
        return self.client.post('/ajax/login/', {
            'username': identifier, 'password': PASSWORD,
        })

    def test_an_unconfirmed_account_with_the_right_password_is_refused(self):
        user = make_user('newbie', 'newbie@example.invalid', active=False)
        data = self._login('newbie').json()
        self.assertFalse(data['success'])
        self.assertTrue(data['requires_verification'])
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertFalse(is_email_verified(user))

    def test_the_refusal_tells_the_visitor_to_verify_the_email(self):
        make_user('newbie2', 'newbie2@example.invalid', active=False)
        data = self._login('newbie2').json()
        self.assertEqual('verify_email', data['message'])
        self.assertIn('not been verified', data['detail'])
        self.assertEqual('newbie2@example.invalid', data['email'])

    def test_an_active_but_unconfirmed_account_is_refused_too(self):
        # An administrator switched the account on by hand without the address
        # ever being confirmed; the rule still holds.
        make_user('handson', 'handson@example.invalid', active=True, verified=False)
        data = self._login('handson').json()
        self.assertTrue(data['requires_verification'])
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_a_confirmed_account_signs_in(self):
        make_user('good', 'good@example.invalid', active=True, verified=True)
        response = self._login('good')
        self.assertTrue(response.json()['success'])
        self.assertIn('_auth_user_id', self.client.session)

    def test_an_account_without_a_profile_row_still_signs_in(self):
        make_user('plain', 'plain@example.invalid', active=True)
        self.assertTrue(self._login('plain').json()['success'])

    def test_a_wrong_password_is_a_plain_failure(self):
        make_user('wrongpw', 'wrongpw@example.invalid', active=True, verified=True)
        data = self.client.post('/ajax/login/', {
            'username': 'wrongpw', 'password': 'not-the-password',
        }).json()
        self.assertFalse(data['success'])
        self.assertNotIn('requires_verification', data)


class ProtectedRoutesTest(TestCase):
    """An unconfirmed session reaches nothing but the confirmation screen."""

    def setUp(self):
        self.client = Client()

    def _sign_in_unconfirmed(self):
        user = make_user('halfway', 'halfway@example.invalid', active=True, verified=False)
        self.client.force_login(user)
        return user

    def test_every_protected_page_shows_the_confirmation_screen(self):
        self._sign_in_unconfirmed()
        for path in ['/', '/movies/', '/about/', '/privacy/', '/contact/']:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(403, response.status_code)
                self.assertContains(response, 'Verify your email to continue',
                                    status_code=403)

    def test_the_screen_offers_the_code_box_and_a_resend(self):
        self._sign_in_unconfirmed()
        response = self.client.get('/movies/')
        self.assertContains(response, 'submitVerifyGate', status_code=403)
        self.assertContains(response, 'resendVerifyGate', status_code=403)
        self.assertContains(response, 'halfway@example.invalid', status_code=403)

    def test_the_machine_surfaces_still_answer(self):
        self._sign_in_unconfirmed()
        # The resend endpoint answers on a GET instead of showing the screen,
        # which proves /ajax/ is still reachable.
        response = self.client.get('/ajax/resend-verification/')
        self.assertEqual(200, response.status_code)
        self.assertEqual('Method not allowed', response.json()['message'])

    def test_a_confirmed_account_browses_normally(self):
        self.client.force_login(
            make_user('settled', 'settled@example.invalid', active=True, verified=True))
        self.assertEqual(200, self.client.get('/movies/').status_code)

    def test_staff_never_see_the_confirmation_screen(self):
        self.client.force_login(
            make_user('adminuser', 'admin@example.invalid', verified=False, staff=True))
        self.assertEqual(200, self.client.get('/movies/').status_code)

    def test_a_signed_out_visitor_is_not_sent_to_the_screen(self):
        # The gate is off by default, so an anonymous visitor browses as before.
        self.assertFalse(SiteSettings.get_settings().require_login_to_browse)
        self.assertEqual(200, Client().get('/movies/').status_code)


class VerificationCompletesTest(TestCase):
    """Confirming the code is what turns the account on."""

    def setUp(self):
        self.client = Client()

    def test_the_registration_code_confirms_and_then_sign_in_works(self):
        response = self.client.post('/ajax/register/', {
            'username': 'freshuser',
            'email': 'freshuser@gmail.com',
            'password': PASSWORD,
            'confirm_password': PASSWORD,
        })
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['requires_verification'])

        user = User.objects.get(email='freshuser@gmail.com')
        self.assertFalse(user.is_active)
        self.assertFalse(is_email_verified(user))

        ev = EmailVerification.objects.filter(user=user).latest('created_at')
        verified = self.client.post('/ajax/verify-email-otp/', {
            'email': 'freshuser@gmail.com', 'otp': ev.otp,
        }).json()
        self.assertTrue(verified['success'])

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(is_email_verified(user))

        sign_in = self.client.post('/ajax/login/', {
            'username': 'freshuser', 'password': PASSWORD,
        }).json()
        self.assertTrue(sign_in['success'])
        self.assertIn('_auth_user_id', self.client.session)

    def test_the_emailed_link_confirms_the_account(self):
        user = make_user('linkuser', 'linkuser@gmail.com', active=False)
        ev = EmailVerification.objects.create(
            user=user, token='link-token-123', otp='000000')
        response = self.client.get('/ajax/verify-email/', {'token': ev.token})
        self.assertEqual(200, response.status_code)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(is_email_verified(user))

    def test_the_app_verify_endpoint_confirms_the_account(self):
        user = make_user('appuser', 'appuser@example.com', active=False)
        ev = EmailVerification.objects.create(
            user=user, token='app-token', otp='123456')
        AndroidApp.objects.create(
            name='CineStream', slug='cinestream', is_active=True,
            access_username='shared', access_password='secret')
        basic = 'Basic ' + base64.b64encode(b'shared:secret').decode()
        response = self.client.post(
            '/api/user/verify-email/',
            json.dumps({'email': 'appuser@example.com', 'otp': ev.otp}),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic,
        )
        self.assertEqual(200, response.status_code, response.content[:200])
        self.assertEqual('success', response.json()['status'])
        self.assertTrue(is_email_verified(user))

    def test_the_app_login_refuses_an_unconfirmed_account(self):
        make_user('applogin', 'applogin@example.com', active=True, verified=False)
        AndroidApp.objects.create(
            name='CineStream', slug='cinestream', is_active=True,
            access_username='shared', access_password='secret')
        basic = 'Basic ' + base64.b64encode(b'shared:secret').decode()
        response = self.client.post(
            '/api/user/login/',
            json.dumps({'email': 'applogin@example.com', 'password': PASSWORD}),
            content_type='application/json',
            HTTP_AUTHORIZATION=basic,
        )
        self.assertEqual(403, response.status_code, response.content[:200])
        self.assertTrue(response.json()['requiresVerification'])


class InstallPageTest(TestCase):
    """The Google Play page is public, including while the site gate is on."""

    def setUp(self):
        self.client = Client()

    def test_the_install_page_is_served_without_signing_in(self):
        response = self.client.get('/get-app/')
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'play.google.com/store/apps/details?id=com.aistudio.cinestream.vqdpmr')

    def test_the_install_page_stays_open_while_the_browse_gate_is_on(self):
        settings_row = SiteSettings.get_settings()
        settings_row.require_login_to_browse = True
        settings_row.save()
        try:
            # A normal page bounces an anonymous visitor...
            self.assertEqual(302, self.client.get('/movies/').status_code)
            # ...but the install page is served straight away.
            self.assertEqual(200, self.client.get('/get-app/').status_code)
        finally:
            settings_row.require_login_to_browse = False
            settings_row.save()

    def test_the_install_page_has_the_download_button(self):
        response = self.client.get('/get-app/')
        self.assertContains(response, 'Get it on')
        self.assertContains(response, 'Google Play')