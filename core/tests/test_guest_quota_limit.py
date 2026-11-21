"""The guest allowance comes from the app's no_login_play setting.

The app edit page writes it into AndroidApp.json_payload; the two guest
endpoints read it from the app row that authenticated the call. These tests
cover the number reaching playback: default 10, an admin value, and a change
made after a device has already used plays.
"""
import base64
import json

from django.test import TestCase
from django.urls import reverse

from core.guest_access import limit_for_app
from core.models import AndroidApp, GuestPlayQuota


class GuestQuotaBase(TestCase):
    def setUp(self):
        self.app = AndroidApp.objects.create(
            name='CineStream', slug='cinestream', access_username='u',
            access_password='p', json_payload={'login_required': 0})
        token = base64.b64encode(b'u:p').decode()
        self.auth = 'Basic ' + token

    def quota(self, device='device-1', email=''):
        return self.client.get(
            reverse('api_guest_quota'),
            {'deviceId': device, 'email': email},
            HTTP_AUTHORIZATION=self.auth)

    def play(self, device='device-1', email=''):
        return self.client.post(
            reverse('api_guest_play'),
            data=json.dumps({'deviceId': device, 'email': email, 'titleId': '1'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth)


class GuestQuotaLimitTest(GuestQuotaBase):
    def test_without_the_key_ten_plays_are_allowed(self):
        self.assertEqual(10, limit_for_app(self.app))
        self.assertEqual(10, self.quota().json()['limit'])
        self.assertEqual(10, self.quota().json()['remaining'])

    def test_the_saved_value_is_the_allowance(self):
        self.app.json_payload['no_login_play'] = 2
        self.app.save()

        self.assertEqual(2, self.quota().json()['limit'])
        self.assertEqual(200, self.play().status_code)
        self.assertEqual(200, self.play().status_code)
        response = self.play()
        self.assertEqual(403, response.status_code)
        self.assertEqual('limit_reached', response.json()['status'])
        self.assertEqual(0, response.json()['remaining'])

    def test_a_string_or_assignment_value_is_read(self):
        self.app.json_payload['no_login_play'] = '3'
        self.app.save()
        self.assertEqual(3, limit_for_app(self.app))

        self.app.json_payload['no_login_play'] = 'no_login_play=4'
        self.app.save()
        self.assertEqual(4, limit_for_app(self.app))

    def test_raising_the_limit_keeps_used_plays(self):
        self.app.json_payload['no_login_play'] = 2
        self.app.save()
        self.play()
        self.play()

        self.app.json_payload['no_login_play'] = 5
        self.app.save()

        payload = self.quota().json()
        self.assertEqual(5, payload['limit'])
        self.assertEqual(2, payload['used'])
        self.assertEqual(3, payload['remaining'])
        self.assertFalse(payload['blocked'])

    def test_zero_needs_an_account_straight_away(self):
        self.app.json_payload['no_login_play'] = 0
        self.app.save()

        response = self.play()
        self.assertEqual(403, response.status_code)
        self.assertEqual(0, response.json()['limit'])

    def test_a_verified_account_stays_unlimited(self):
        from core.models import SyncedUser
        self.app.json_payload['no_login_play'] = 0
        self.app.save()
        SyncedUser.objects.create(email='member@example.com', is_email_verified=True)

        payload = self.quota(email='member@example.com').json()
        self.assertTrue(payload['unlimited'])
        self.assertIsNone(payload['limit'])
        self.assertEqual(200, self.play(email='member@example.com').status_code)
