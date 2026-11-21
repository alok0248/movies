"""The guest preview panel on the admin dashboard.

It answers two questions at a glance: which devices have used up their free
titles (so they need an account) and what each device last watched.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import GuestPlayQuota


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
