"""Offline access is server-driven: the sync payload always carries the shipped
policy so the shape is discoverable, and an operator object wins key by key."""
import json

from django.test import TestCase

from core.models import AppRemoteConfig
from core.views import _remote_config_payload


class OfflinePolicyPayloadTest(TestCase):
    def setUp(self):
        AppRemoteConfig.objects.all().delete()

    def _store(self, payload):
        AppRemoteConfig.objects.create(payload=json.dumps(payload))

    def test_defaults_are_served_without_operator_config(self):
        self._store({})
        offline = _remote_config_payload()['offline']
        self.assertEqual(17, offline['subscription_grace_days'])
        self.assertFalse(offline['allow_guest_offline'])
        self.assertFalse(offline['allow_registered_offline'])

    def test_operator_values_win_per_key(self):
        self._store({'offline': {'subscription_grace_days': 30, 'allow_guest_offline': True}})
        offline = _remote_config_payload()['offline']
        self.assertEqual(30, offline['subscription_grace_days'])
        self.assertTrue(offline['allow_guest_offline'])
        self.assertFalse(offline['allow_registered_offline'])

    def test_a_malformed_policy_falls_back_to_defaults(self):
        self._store({'offline': {'subscription_grace_days': 'soon', 'allow_guest_offline': 'yes'}})
        offline = _remote_config_payload()['offline']
        self.assertEqual(17, offline['subscription_grace_days'])
        self.assertFalse(offline['allow_guest_offline'])

    def test_negative_days_fall_back_to_defaults(self):
        self._store({'offline': {'subscription_grace_days': -5}})
        self.assertEqual(17, _remote_config_payload()['offline']['subscription_grace_days'])

    def test_rejects_a_non_object_offline_value(self):
        self._store({'offline': 'seventeen'})
        offline = _remote_config_payload()['offline']
        self.assertEqual(17, offline['subscription_grace_days'])
        self.assertFalse(offline['allow_guest_offline'])

    def test_other_payload_keys_survive(self):
        self._store({'flags': {'downloads': False}, 'min_supported_build': 600})
        payload = _remote_config_payload()
        self.assertEqual({'downloads': False}, payload['flags'])
        self.assertEqual(600, payload['min_supported_build'])

    def test_no_config_row_serves_nothing(self):
        self.assertEqual({}, _remote_config_payload())
