"""The emailed second factor for admin sign-in (core.staff_mfa).

Covers the classifier (which requests are challenged), the code lifecycle
(issue -> verify -> used, expiry, attempt limit), the middleware redirect, and
the /staff-otp/ view including the fail-open path when mail is unavailable.

The suite runs with STAFF_OTP_REQUIRED off (settings.py forces it off under the
test runner), so every test here turns it on with override_settings.
"""
import time
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from core import staff_mfa
from core.models import StaffLoginOTP

MFA_ON = dict(STAFF_OTP_REQUIRED=True, STAFF_OTP_FAIL_OPEN=True,
              STAFF_OTP_TTL_SECONDS=12 * 60 * 60)


def _code_from_mail(mock_send):
    """Pull the 6-digit code out of the most recent send_mail() call."""
    body = mock_send.call_args.args[1]
    for token in body.replace('.', ' ').split():
        if len(token) == 6 and token.isdigit():
            return token
    raise AssertionError('no 6-digit code in the email body: %r' % body)


class _Req:
    """Minimal request stand-in for the pure helpers."""

    def __init__(self, user, path='/admin-dashboard/', session=None):
        self.user = user
        self.path = path
        self.path_info = path
        self.session = session if session is not None else {}


@override_settings(**MFA_ON)
class ChallengeClassifierTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw', is_staff=True)

    def test_a_staff_dashboard_request_is_challenged(self):
        self.assertTrue(staff_mfa.challenge_needed(_Req(self.staff)))

    def test_a_public_page_is_never_challenged(self):
        self.assertFalse(staff_mfa.challenge_needed(_Req(self.staff, path='/movies/')))

    def test_the_otp_page_itself_is_not_challenged(self):
        self.assertFalse(staff_mfa.challenge_needed(_Req(self.staff, path='/staff-otp/')))

    def test_a_non_staff_user_is_never_challenged(self):
        normal = User.objects.create_user('joe', 'joe@example.com', 'pw')
        self.assertFalse(staff_mfa.challenge_needed(_Req(normal)))

    def test_a_staff_row_without_an_email_is_let_through(self):
        boss = User.objects.create_user('no-mail', '', 'pw', is_staff=True)
        self.assertFalse(staff_mfa.challenge_needed(_Req(boss)))

    def test_a_passed_session_is_not_challenged(self):
        req = _Req(self.staff)
        staff_mfa.mark_passed(req)
        self.assertFalse(staff_mfa.challenge_needed(req))

    @override_settings(STAFF_OTP_REQUIRED=False)
    def test_the_switch_off_disables_the_gate(self):
        self.assertFalse(staff_mfa.challenge_needed(_Req(self.staff)))


@override_settings(**MFA_ON)
class CodeLifecycleTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw', is_staff=True)

    @patch('django.core.mail.send_mail')
    def test_a_mailed_code_verifies_once(self, send):
        ok, err = staff_mfa.issue(self.staff)
        self.assertTrue(ok)
        self.assertEqual('', err)
        code = _code_from_mail(send)

        self.assertTrue(staff_mfa.verify(self.staff, code))
        # A used code cannot be replayed.
        self.assertFalse(staff_mfa.verify(self.staff, code))

    @patch('core.auth.send_configured_email', return_value=True)
    def test_the_code_goes_through_the_configured_sender(self, sender):
        # EMAIL_BACKEND is the console backend, so the code must be sent with the
        # site's configured sender (EmailAddress / SiteSettings SMTP), not a
        # bare send_mail that would only print to the log.
        ok, _err = staff_mfa.issue(self.staff)
        self.assertTrue(ok)
        self.assertEqual(['boss@example.com'], sender.call_args.kwargs['recipient_list'])
        self.assertEqual('verification', sender.call_args.kwargs['purpose'])

    @patch('django.core.mail.send_mail')
    def test_a_wrong_code_counts_an_attempt(self, send):
        staff_mfa.issue(self.staff)
        self.assertFalse(staff_mfa.verify(self.staff, '000000'))
        row = StaffLoginOTP.objects.filter(user=self.staff).latest('created_at')
        self.assertEqual(1, row.attempts)

    @patch('django.core.mail.send_mail')
    def test_the_attempt_limit_locks_the_code(self, send):
        staff_mfa.issue(self.staff)
        for _ in range(StaffLoginOTP.MAX_ATTEMPTS):
            staff_mfa.verify(self.staff, '111111')
        row = StaffLoginOTP.objects.filter(user=self.staff).latest('created_at')
        self.assertTrue(row.too_many_attempts)
        code = _code_from_mail(send)
        self.assertFalse(staff_mfa.verify(self.staff, code))

    @patch('django.core.mail.send_mail')
    def test_an_expired_code_is_refused(self, send):
        staff_mfa.issue(self.staff)
        code = _code_from_mail(send)
        StaffLoginOTP.objects.filter(user=self.staff).update(
            created_at=StaffLoginOTP.objects.get(user=self.staff).created_at.replace(year=2000))
        self.assertFalse(staff_mfa.verify(self.staff, code))

    @patch('django.core.mail.send_mail')
    def test_issuing_again_retires_the_old_code(self, send):
        staff_mfa.issue(self.staff)
        first = _code_from_mail(send)
        staff_mfa.issue(self.staff)
        second = _code_from_mail(send)
        self.assertNotEqual(first, second)
        # The newest code works; the earlier one was marked used.
        self.assertTrue(staff_mfa.verify(self.staff, second))

    @patch('django.core.mail.send_mail')
    def test_has_live_code_tracks_the_lifecycle(self, send):
        self.assertFalse(staff_mfa.has_live_code(self.staff))
        staff_mfa.issue(self.staff)
        self.assertTrue(staff_mfa.has_live_code(self.staff))
        staff_mfa.verify(self.staff, _code_from_mail(send))
        self.assertFalse(staff_mfa.has_live_code(self.staff))

    @patch('django.core.mail.send_mail', side_effect=RuntimeError('smtp down'))
    def test_a_mail_failure_is_reported_not_raised(self, send):
        ok, err = staff_mfa.issue(self.staff)
        self.assertFalse(ok)
        self.assertTrue(err)


@override_settings(**MFA_ON)
class SessionPassTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw', is_staff=True)

    def test_passed_survives_then_expires(self):
        req = _Req(self.staff)
        staff_mfa.mark_passed(req)
        self.assertTrue(staff_mfa.passed(req))
        req.session[staff_mfa.SESSION_PASSED_KEY] = time.time() - (MFA_ON['STAFF_OTP_TTL_SECONDS'] + 5)
        self.assertFalse(staff_mfa.passed(req))

    def test_clear_removes_the_mark(self):
        req = _Req(self.staff)
        staff_mfa.mark_passed(req)
        staff_mfa.clear(req)
        self.assertFalse(staff_mfa.passed(req))


@override_settings(**MFA_ON)
class MiddlewareAndViewTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw', is_staff=True)
        self.client = Client()

    def test_the_dashboard_redirects_to_the_code_screen(self):
        self.client.force_login(self.staff)
        response = self.client.get('/admin-dashboard/')
        self.assertEqual(302, response.status_code)
        self.assertIn('/staff-otp/', response['Location'])

    def test_a_non_staff_user_is_not_sent_to_the_code_screen(self):
        joe = User.objects.create_user('joe', 'joe@example.com', 'pw')
        self.client.force_login(joe)
        response = self.client.get('/movies/')
        self.assertNotIn('/staff-otp/', response.get('Location', ''))

    @patch('django.core.mail.send_mail')
    def test_the_screen_mails_a_code_and_verifies_it(self, send):
        self.client.force_login(self.staff)
        page = self.client.get('/staff-otp/?next=/admin-dashboard/')
        self.assertEqual(200, page.status_code)
        self.assertContains(page, 'Administrator verification')
        code = _code_from_mail(send)

        response = self.client.post('/staff-otp/',
                                    {'code': code, 'next': '/admin-dashboard/'})
        self.assertEqual(302, response.status_code)
        self.assertEqual('/admin-dashboard/', response['Location'])
        # The mark is on the session, so the dashboard now opens.
        self.assertEqual(200, self.client.get('/admin-dashboard/').status_code)

    @patch('django.core.mail.send_mail')
    def test_a_wrong_code_re_renders_with_a_message(self, send):
        self.client.force_login(self.staff)
        self.client.get('/staff-otp/?next=/admin-dashboard/')
        response = self.client.post('/staff-otp/',
                                    {'code': '999999', 'next': '/admin-dashboard/'})
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'incorrect or has expired')

    @patch('django.core.mail.send_mail')
    def test_resend_issues_a_fresh_code(self, send):
        self.client.force_login(self.staff)
        self.client.get('/staff-otp/?next=/admin-dashboard/')
        first = _code_from_mail(send)
        response = self.client.post('/staff-otp/',
                                    {'action': 'resend', 'next': '/admin-dashboard/'})
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, send.call_count)
        second = _code_from_mail(send)
        self.assertNotEqual(first, second)

    @patch('django.core.mail.send_mail', side_effect=RuntimeError('smtp down'))
    def test_mail_outage_lets_the_admin_through(self, send):
        self.client.force_login(self.staff)
        response = self.client.get('/staff-otp/?next=/admin-dashboard/')
        self.assertEqual(302, response.status_code)
        self.assertEqual('/admin-dashboard/', response['Location'])
        self.assertEqual(200, self.client.get('/admin-dashboard/').status_code)

    @override_settings(STAFF_OTP_FAIL_OPEN=False)
    @patch('django.core.mail.send_mail', side_effect=RuntimeError('smtp down'))
    def test_mail_outage_blocks_when_fail_open_is_off(self, send):
        self.client.force_login(self.staff)
        response = self.client.get('/staff-otp/?next=/admin-dashboard/')
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'Could not send a code')