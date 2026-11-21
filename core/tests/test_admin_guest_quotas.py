"""The guest preview panel on the admin dashboard.

It answers two questions at a glance: which devices have used up their free
titles (so they need an account) and what each device last watched.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import GuestPlayQuota, SyncedUser


class AdminGuestQuotaPanelTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw',
                                              is_staff=True)
        self.client.force_login(self.staff)

    def dashboard(self):
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(200, response.status_code)
        return response.content.decode()

    def test_blocked_device_is_listed_with_its_numbers(self):
        GuestPlayQuota.objects.create(
            device_id='device-blocked', plays_used=10, limit=10,
            last_title='Dune: Part Two', last_play_at=timezone.now())
        GuestPlayQuota.objects.create(
            device_id='device-fresh', plays_used=2, limit=10)

        html = self.dashboard()

        self.assertIn('Guest free previews', html)
        self.assertIn('device-blocked', html)
        self.assertIn('device-fresh', html)
        self.assertIn('Dune: Part Two', html)
        self.assertIn('blocked', html)
        # Blocked devices are listed first so they are the ones you see.
        self.assertLess(html.index('device-blocked'), html.index('device-fresh'))

    def test_panel_reports_the_totals(self):
        GuestPlayQuota.objects.create(device_id='a', plays_used=10, limit=10)
        GuestPlayQuota.objects.create(device_id='b', plays_used=4, limit=10)

        html = self.dashboard()

        self.assertIn('2 devices tracked', html)
        self.assertIn('1 blocked', html)
        self.assertIn('14 plays used', html)

    def test_empty_panel_says_so(self):
        self.assertIn('No guest plays recorded yet.', self.dashboard())

    def test_last_email_and_account_state_are_shown(self):
        SyncedUser.objects.create(email='member@example.com', is_email_verified=True)
        SyncedUser.objects.create(email='typo@example.com', is_email_verified=False)
        GuestPlayQuota.objects.create(
            device_id='device-member', plays_used=10, limit=10,
            last_email='member@example.com')
        GuestPlayQuota.objects.create(
            device_id='device-typo', plays_used=10, limit=10,
            last_email='typo@example.com')
        GuestPlayQuota.objects.create(
            device_id='device-guest', plays_used=4, limit=10)

        html = self.dashboard()

        self.assertIn('member@example.com', html)
        self.assertIn('verified', html)
        self.assertIn('typo@example.com', html)
        self.assertIn('unverified', html)
        self.assertIn('guest, no email', html)
        # The verified member and the unverified row both have an account.
        self.assertIn('2 with an account', html)

    def test_an_address_without_a_row_is_marked_no_account(self):
        GuestPlayQuota.objects.create(
            device_id='device-stranger', plays_used=10, limit=10,
            last_email='stranger@example.com')

        self.assertIn('no account', self.dashboard())
