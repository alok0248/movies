"""The app edit page's behaviour switches.

The Android app reads login_required, subscription, without_server and sync
from the top of its config response. They live in AndroidApp.json_payload, and
the edit page shows them as sliders plus a seconds box, so the four values can
be flipped without hand-editing the JSON.
"""
import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import AndroidApp


class AppSwitchTestBase(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw',
                                              is_staff=True)
        self.client.force_login(self.staff)
        self.app = AndroidApp.objects.create(
            name='CineStream', slug='cinestream', access_username='u',
            access_password='p', data_retention_days=30, log_retention_days=30,
            json_payload={
                'api_key': 'ce9d3dcbdb958906922ca6fbead78b95',
                'login_required': 0,
                'sync': 300,
                'subscription': 0,
                'without_server': 0,
                'iptv_links': ['https://iptv-org.github.io/iptv/index.m3u'],
                'ads1': {'click': 50, 'amazon1': {'tag': 'headhuntrr005-21'}},
            })

    def edit_url(self):
        return reverse('android_app_edit', args=[self.app.id])

    def post(self, **overrides):
        payload = overrides.pop('payload', None)
        data = {
            'name': self.app.name,
            'slug': self.app.slug,
            'allowed_endpoint': self.app.allowed_endpoint,
            'allowed_build_id': self.app.allowed_build_id,
            'access_username': self.app.access_username,
            'access_password': self.app.access_password,
            'data_retention_days': self.app.data_retention_days,
            'log_retention_days': self.app.log_retention_days,
            'is_active': 'on',
            'json_payload_input': json.dumps(payload if payload is not None
                                             else self.app.json_payload),
            'sync': self.app.json_payload.get('sync', 300),
        }
        data.update(overrides)
        return self.client.post(self.edit_url(), data)

    def reload(self):
        self.app.refresh_from_db()
        return self.app.json_payload


class AppSwitchPageTest(AppSwitchTestBase):
    def test_the_page_shows_a_slider_for_each_and_the_timer(self):
        response = self.client.get(self.edit_url())
        self.assertEqual(200, response.status_code)
        form = response.context['form']
        self.assertEqual(0, form['login_required'].value() and 1)
        self.assertEqual(0, form['subscription'].value() and 1)
        self.assertEqual(0, form['without_server'].value() and 1)
        self.assertEqual(300, form['sync'].value())
        self.assertContains(response, 'name="login_required"')
        self.assertContains(response, 'name="subscription"')
        self.assertContains(response, 'name="without_server"')
        self.assertContains(response, 'name="sync"')
        self.assertContains(response, 'name="no_login_play"')

    def test_the_plays_before_login_box_defaults_to_ten(self):
        form = self.client.get(self.edit_url()).context['form']
        self.assertEqual(10, form['no_login_play'].value())

    def test_switched_on_values_are_shown_as_on(self):
        self.app.json_payload.update({'login_required': 1, 'subscription': 1,
                                      'without_server': 1, 'sync': 600})
        self.app.save()
        form = self.client.get(self.edit_url()).context['form']
        self.assertTrue(form['login_required'].value())
        self.assertTrue(form['subscription'].value())
        self.assertTrue(form['without_server'].value())
        self.assertEqual(600, form['sync'].value())

    def test_staff_only(self):
        self.client.logout()
        response = self.client.get(self.edit_url())
        self.assertEqual(302, response.status_code)


class AppSwitchSaveTest(AppSwitchTestBase):
    def test_the_sliders_write_the_config_keys(self):
        response = self.post(login_required='on', without_server='on',
                             subscription='', sync=600)
        self.assertEqual(302, response.status_code)

        payload = self.reload()
        self.assertEqual(1, payload['login_required'])
        self.assertEqual(1, payload['without_server'])
        self.assertEqual(0, payload['subscription'])
        self.assertEqual(600, payload['sync'])
        self.assertEqual(10, payload['no_login_play'])

    def test_the_plays_box_writes_the_config_key(self):
        self.post(no_login_play=25)
        self.assertEqual(25, self.reload()['no_login_play'])

    def test_a_blank_plays_box_keeps_the_saved_value(self):
        self.app.json_payload['no_login_play'] = 4
        self.app.save()
        self.post(no_login_play='')
        self.assertEqual(4, self.reload()['no_login_play'])

    def test_every_other_key_in_the_payload_is_kept(self):
        self.post(login_required='on', sync=120)

        payload = self.reload()
        self.assertEqual('ce9d3dcbdb958906922ca6fbead78b95', payload['api_key'])
        self.assertEqual(['https://iptv-org.github.io/iptv/index.m3u'],
                         payload['iptv_links'])
        self.assertEqual('headhuntrr005-21', payload['ads1']['amazon1']['tag'])
        self.assertEqual(50, payload['ads1']['click'])

    def test_a_blank_timer_keeps_the_saved_one(self):
        self.app.json_payload['sync'] = 240
        self.app.save()
        self.post(sync='', subscription='on')

        payload = self.reload()
        self.assertEqual(240, payload['sync'])
        self.assertEqual(1, payload['subscription'])

    def test_saving_drops_the_endpoint_cache_so_the_app_sees_the_change(self):
        from django.core.cache import cache
        key = 'android_endpoint_payload_%s' % self.app.slug
        cache.set(key, {'login_required': 0}, 300)

        self.post(login_required='on')

        self.assertIsNone(cache.get(key))

    def test_a_timer_below_the_minimum_is_refused(self):
        self.post(sync=5)

        self.assertEqual(300, self.reload()['sync'])

    def test_a_timer_above_a_day_is_refused(self):
        self.post(sync=999999)

        self.assertEqual(300, self.reload()['sync'])

    def test_a_negative_play_count_is_refused(self):
        self.app.json_payload['no_login_play'] = 10
        self.app.save()
        response = self.post(no_login_play=-3)

        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'Ensure this value is greater than or equal to 0')
        self.assertEqual(10, self.reload()['no_login_play'])

    def test_an_array_payload_cannot_use_the_switches(self):
        response = self.post(payload=[], login_required='on')

        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'The payload must be a JSON object')
        # Nothing is saved, so the app keeps its object payload as it was.
        self.assertEqual(0, self.reload()['login_required'])
        self.assertEqual(300, self.reload()['sync'])

    def test_broken_json_is_still_reported(self):
        response = self.client.post(self.edit_url(), {
            'name': self.app.name, 'slug': self.app.slug,
            'access_username': 'u', 'access_password': 'p',
            'data_retention_days': 30, 'log_retention_days': 30,
            'json_payload_input': '{oops', 'sync': 300,
        })
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'Invalid JSON')
        self.assertEqual(300, self.reload()['sync'])
