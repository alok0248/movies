"""Device and app checks on the Android API traffic.

The checks are evaluated on every request but only enforced when the app has
``strict_client_validation`` switched on, so the live app keeps working until
an operator decides otherwise. These tests cover both halves:

* observe mode — a request that would be refused is let through and stored with
  ``would_block`` set, which is what makes a trial run safe;
* enforce mode — the scenarios the requirement lists: a Google Play install and
  an official APK are allowed; a sideload, a tampered build, an emulator, a
  non-Android client, a browser and a spoofed app identity are refused with the
  documented reason code.

The HMAC tests cover replay, staleness and a wrong digest.
"""
import hashlib
import hmac
import time

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from core.client_validation import MAX_CLOCK_SKEW_SECONDS, REASONS
from core.models import AndroidApp, AppClientValidationLog

APP_SLUG = 'cinestream'
PACKAGE = 'com.aistudio.cinestream.vqdpmr'
CERT = 'AB' * 32
PLAY = 'com.android.vending'
OFFICIAL_APK = 'com.google.android.packageinstaller'
SIGNING_SECRET = 'signing-secret'


class ClientValidationBase(TestCase):
    def setUp(self):
        cache.clear()
        self.app = AndroidApp.objects.create(
            name='CineStream', slug=APP_SLUG,
            access_username='shared', access_password='secret', is_active=True,
            strict_client_validation=True,
            allowed_package_names=PACKAGE,
            allowed_installers='%s\n%s' % (PLAY, OFFICIAL_APK),
            allowed_signing_cert_sha256=CERT,
        )
        import base64
        self.basic = 'Basic ' + base64.b64encode(b'shared:secret').decode()
        self.client = Client()

    def headers(self, **overrides):
        base = {
            'HTTP_AUTHORIZATION': self.basic,
            'HTTP_USER_AGENT': 'okhttp/4.12.0',
            'HTTP_X_ANDROID_PLATFORM': 'android',
            'HTTP_X_ANDROID_PACKAGE': PACKAGE,
            'HTTP_X_ANDROID_INSTALLER': PLAY,
            'HTTP_X_ANDROID_SIGNATURE_SHA256': CERT,
            'HTTP_X_ANDROID_APP': APP_SLUG,
        }
        base.update(overrides)
        return base

    def call(self, **overrides):
        """Hit a real Android endpoint so the whole chain runs for real."""
        return self.client.get('/api/messages/', {'deviceId': 'dev-1'},
                               **self.headers(**overrides))

    def last_log(self):
        return AppClientValidationLog.objects.filter(android_app=self.app).order_by('-created_at').first()


class LegitimateInstallTest(ClientValidationBase):
    def test_a_google_play_install_is_allowed(self):
        response = self.call()
        self.assertEqual(200, response.status_code, response.content[:200])

    def test_an_official_apk_install_is_allowed(self):
        response = self.call(HTTP_X_ANDROID_INSTALLER=OFFICIAL_APK)
        self.assertEqual(200, response.status_code, response.content[:200])

    def test_a_certificate_with_colons_in_a_different_case_still_matches(self):
        colonised = ':'.join(CERT[i:i + 2] for i in range(0, len(CERT), 2)).lower()
        response = self.call(HTTP_X_ANDROID_SIGNATURE_SHA256=colonised)
        self.assertEqual(200, response.status_code, response.content[:200])

    def test_an_allowed_request_is_logged_once_enforcement_is_on(self):
        self.call()
        row = self.last_log()
        self.assertIsNotNone(row)
        self.assertEqual('allow', row.decision)
        self.assertTrue(row.strict)
        self.assertEqual(PACKAGE, row.package_name)
        self.assertEqual(PLAY, row.installer)


class RefusedClientTest(ClientValidationBase):
    """Every scenario in the requirement, refused with its own reason code."""

    def _assert_refused(self, code, **overrides):
        response = self.call(**overrides)
        self.assertEqual(REASONS[code][0], response.status_code, response.content[:200])
        self.assertEqual(code, response.json()['code'])
        return response

    def test_a_sideloaded_unofficial_apk_is_refused(self):
        self._assert_refused('installer_not_allowed',
                             HTTP_X_ANDROID_INSTALLER='com.example.sideload')

    def test_a_tampered_build_is_refused(self):
        self._assert_refused('tamper_detected', HTTP_X_ANDROID_TAMPERED='1')

    def test_a_build_signed_by_a_different_certificate_is_refused(self):
        self._assert_refused('signature_mismatch',
                             HTTP_X_ANDROID_SIGNATURE_SHA256='CD' * 32)

    def test_a_missing_signature_header_is_refused(self):
        self._assert_refused('missing_signature_header', HTTP_X_ANDROID_SIGNATURE_SHA256='')

    def test_an_emulator_is_refused(self):
        self._assert_refused('emulator_detected', HTTP_X_ANDROID_EMULATOR='1')

    def test_a_debuggable_build_is_refused(self):
        self._assert_refused('debug_build', HTTP_X_ANDROID_DEBUG='1')

    def test_a_non_android_platform_is_refused(self):
        self._assert_refused('platform_not_android', HTTP_X_ANDROID_PLATFORM='ios')

    def test_missing_platform_header_is_refused(self):
        self._assert_refused('missing_platform_header', HTTP_X_ANDROID_PLATFORM='')

    def test_a_browser_replaying_the_api_is_refused(self):
        self._assert_refused('browser_client', HTTP_ORIGIN='https://newmovies.linkpc.net')

    def test_a_request_claiming_another_app_is_refused(self):
        # The credentials belong to cinestream; the request says otherwise.
        self._assert_refused('app_identity_mismatch', HTTP_X_ANDROID_APP='someone-else')

    def test_an_unlisted_package_is_refused(self):
        self._assert_refused('package_mismatch', HTTP_X_ANDROID_PACKAGE='com.clone.app')

    def test_a_refusal_is_written_to_the_log_with_its_reason(self):
        self._assert_refused('emulator_detected', HTTP_X_ANDROID_EMULATOR='1')
        row = self.last_log()
        self.assertEqual('reject', row.decision)
        self.assertEqual('emulator_detected', row.reason_code)
        self.assertTrue(row.reason)
        self.assertTrue(row.would_block)


class ObserveModeTest(ClientValidationBase):
    """With enforcement off nothing is refused, but the log still records it."""

    def setUp(self):
        super().setUp()
        self.app.strict_client_validation = False
        self.app.save(update_fields=['strict_client_validation'])

    def test_a_request_that_would_be_refused_is_let_through(self):
        response = self.call(HTTP_X_ANDROID_INSTALLER='com.example.sideload')
        self.assertEqual(200, response.status_code, response.content[:200])

    def test_the_would_be_refusal_is_recorded(self):
        self.call(HTTP_X_ANDROID_INSTALLER='com.example.sideload')
        row = self.last_log()
        self.assertIsNotNone(row)
        self.assertEqual('allow', row.decision)
        self.assertTrue(row.would_block)
        self.assertFalse(row.strict)
        self.assertEqual('installer_not_allowed', row.reason_code)

    def test_a_clean_request_is_not_logged_in_observe_mode(self):
        self.call()
        self.assertIsNone(self.last_log())

    def test_the_default_for_a_new_app_is_observe_only(self):
        other = AndroidApp.objects.create(
            name='Other', slug='other', access_username='o', access_password='p')
        self.assertFalse(other.strict_client_validation)


class RequestSignatureTest(ClientValidationBase):
    """The optional HMAC layer: freshness, replay and digest."""

    def setUp(self):
        super().setUp()
        self.app.require_request_signature = True
        self.app.request_signing_secret = SIGNING_SECRET
        self.app.save(update_fields=['require_request_signature', 'request_signing_secret'])

    def _signature(self, stamp, nonce, method='GET', path='/api/messages/', body=b''):
        canonical = '\n'.join([method, path, str(stamp), nonce,
                               hashlib.sha256(body).hexdigest()])
        return hmac.new(SIGNING_SECRET.encode(), canonical.encode(),
                        hashlib.sha256).hexdigest()

    def _signed(self, stamp=None, nonce='nonce-0001', signature=None):
        stamp = int(time.time()) if stamp is None else stamp
        signature = signature or self._signature(stamp, nonce)
        return {
            'HTTP_X_ANDROID_SIGNATURE': signature,
            'HTTP_X_ANDROID_TIMESTAMP': str(stamp),
            'HTTP_X_ANDROID_NONCE': nonce,
        }

    def test_a_correctly_signed_request_is_allowed(self):
        response = self.call(**self._signed())
        self.assertEqual(200, response.status_code, response.content[:200])

    def test_an_unsigned_request_is_refused(self):
        response = self.call()
        self.assertEqual(401, response.status_code)
        self.assertEqual('missing_request_signature', response.json()['code'])

    def test_a_wrong_digest_is_refused(self):
        response = self.call(**self._signed(signature='00' * 32))
        self.assertEqual(401, response.status_code)
        self.assertEqual('invalid_request_signature', response.json()['code'])

    def test_a_stale_request_is_refused(self):
        old = int(time.time()) - MAX_CLOCK_SKEW_SECONDS - 60
        response = self.call(**self._signed(stamp=old))
        self.assertEqual(401, response.status_code)
        self.assertEqual('stale_request', response.json()['code'])

    def test_a_replayed_nonce_is_refused(self):
        headers = self._signed(nonce='replay-me')
        self.assertEqual(200, self.call(**headers).status_code)
        response = self.call(**headers)
        self.assertEqual(401, response.status_code)
        self.assertEqual('replayed_request', response.json()['code'])

    def test_signing_without_a_secret_is_refused_rather_than_trusted(self):
        self.app.request_signing_secret = ''
        self.app.save(update_fields=['request_signing_secret'])
        response = self.call(**self._signed())
        self.assertEqual(500, response.status_code)
        self.assertEqual('signature_not_configured', response.json()['code'])


class AppSettingsValidationTest(TestCase):
    """The admin form refuses a fingerprint that could never match."""

    def setUp(self):
        self.staff = User.objects.create_superuser('boss', 'boss@example.invalid', 'pw-123456')
        self.app = AndroidApp.objects.create(
            name='CineStream', slug=APP_SLUG, access_username='u', access_password='p')
        self.client = Client()
        self.client.force_login(self.staff)

    def _post(self, **extra):
        payload = {
            'section': 'app',
            'data_retention_days': '30',
            'log_retention_days': '30',
            'sync_min_interval_seconds': '45',
            'sync_max_per_minute': '15',
            'sync_ip_max_per_minute': '60',
            'is_active': 'on',
            'log_collection_enabled': 'on',
        }
        payload.update(extra)
        return self.client.post(reverse('android_app_settings', args=[self.app.id]),
                                payload, follow=True)

    def test_an_invalid_fingerprint_is_refused_and_nothing_is_saved(self):
        response = self._post(allowed_signing_cert_sha256='not-a-fingerprint',
                              strict_client_validation='on')
        self.assertContains(response, 'not valid SHA-256 hex')
        self.app.refresh_from_db()
        self.assertEqual('', self.app.allowed_signing_cert_sha256)
        self.assertFalse(self.app.strict_client_validation)

    def test_valid_device_checks_are_saved(self):
        self._post(
            strict_client_validation='on',
            require_android_platform='on',
            block_emulators='on',
            block_debug_builds='on',
            allowed_package_names=PACKAGE,
            allowed_installers='%s\n%s' % (PLAY, OFFICIAL_APK),
            allowed_signing_cert_sha256=':'.join(CERT[i:i + 2] for i in range(0, len(CERT), 2)),
        )
        self.app.refresh_from_db()
        self.assertTrue(self.app.strict_client_validation)
        self.assertTrue(self.app.require_android_platform)
        self.assertTrue(self.app.block_emulators)
        self.assertTrue(self.app.block_debug_builds)
        self.assertEqual([PACKAGE], self.app.allowed_package_list)
        self.assertEqual([PLAY, OFFICIAL_APK], self.app.allowed_installer_list)
        self.assertEqual([CERT], self.app.allowed_signature_list)

    def test_a_blank_secret_box_keeps_the_stored_secret(self):
        self.app.request_signing_secret = 'keep-me'
        self.app.save(update_fields=['request_signing_secret'])
        self._post(request_signing_secret='')
        self.app.refresh_from_db()
        self.assertEqual('keep-me', self.app.request_signing_secret)