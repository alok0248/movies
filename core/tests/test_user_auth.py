"""S1: another person's email must not be readable or writable with the shared
app credential alone, while token-less builds keep working until the cutoff."""
import base64
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from core.auth import create_user_token_pair
from core.models import AndroidApp


class PerUserCredentialTest(TestCase):
    def setUp(self):
        self.app = AndroidApp.objects.create(
            name='CineStream Test', slug='cinestream-test',
            access_username='shared', access_password='secret', is_active=True)
        self.user_a = User.objects.create_user(username='a@example.com', email='a@example.com')
        self.user_b = User.objects.create_user(username='b@example.com', email='b@example.com')
        self.token_a = create_user_token_pair(self.user_a)['token']
        self.token_b = create_user_token_pair(self.user_b)['token']
        self.basic = 'Basic ' + base64.b64encode(b'shared:secret').decode()
        self.client = Client()

    # ------------------------------------------------------------------ helpers
    def _headers(self, token=None):
        headers = {
            'HTTP_AUTHORIZATION': self.basic,
            'HTTP_USER_AGENT': 'CineStream/3.4.8',
            'HTTP_HOST': 'testserver',
        }
        if token:
            headers['HTTP_X_USER_TOKEN'] = token
        return headers

    def _notifications(self, email, token=None):
        return self.client.get('/api/notifications/', {'email': email}, **self._headers(token))

    def _message_post(self, email, token=None, body='hello'):
        payload = json.dumps({'email': email, 'body': body, 'deviceId': 'dev-1'})
        return self.client.post('/api/messages/', payload,
                                content_type='application/json', **self._headers(token))

    def _sync(self, email, token=None):
        payload = json.dumps({'email': email, 'userData': {}})
        return self.client.post('/api/user/sync/', payload,
                                content_type='application/json', **self._headers(token))

    # -------------------------------------------------------------------- tests
    def test_token_may_read_its_own_address(self):
        response = self._notifications('a@example.com', self.token_a)
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()['status'], 'ok')

    def test_token_cannot_read_another_address(self):
        response = self._notifications('b@example.com', self.token_a)
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertEqual(response.json()['code'], 'email_mismatch')
        self.assertNotIn('notifications', response.json())

    def test_token_cannot_write_another_address(self):
        response = self._message_post('b@example.com', self.token_a)
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertEqual(response.json()['code'], 'email_mismatch')

    def test_token_cannot_sync_another_address(self):
        response = self._sync('b@example.com', self.token_a)
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertEqual(response.json()['code'], 'email_mismatch')

    def test_token_reads_and_writes_own_account(self):
        self.assertEqual(self._message_post('a@example.com', self.token_a).status_code, 200)
        sync = self._sync('a@example.com', self.token_a)
        self.assertEqual(sync.status_code, 200, sync.content[:200])
        for key in ('status', 'subscription', 'userData', 'remoteConfig'):
            self.assertIn(key, sync.json())

    def test_garbage_token_is_rejected_not_ignored(self):
        response = self._notifications('a@example.com', 'not-a-jwt')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['code'], 'token_invalid')

    def test_legacy_install_without_token_still_works(self):
        """The transition: existing builds send only the shared credential."""
        self.assertIsNone(self.app.legacy_user_auth_until)
        response = self._notifications('a@example.com')
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(self._sync('a@example.com').status_code, 200)

    def test_tokenless_request_refused_after_cutoff(self):
        self.app.legacy_user_auth_until = timezone.localdate() - timedelta(days=1)
        self.app.save(update_fields=['legacy_user_auth_until'])
        response = self._notifications('a@example.com')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['code'], 'user_token_required')
        # A build that does send its token is unaffected by the cutoff.
        self.assertEqual(self._notifications('a@example.com', self.token_a).status_code, 200)

    def test_cutoff_day_itself_still_works(self):
        self.app.legacy_user_auth_until = timezone.localdate()
        self.app.save(update_fields=['legacy_user_auth_until'])
        self.assertEqual(self._notifications('a@example.com').status_code, 200)

    def test_guest_device_messages_keep_working(self):
        """A guest has no account and therefore no token; device scope is safe."""
        response = self.client.get('/api/messages/', {'deviceId': 'dev-guest'},
                                   **self._headers())
        self.assertEqual(response.status_code, 200, response.content[:200])
