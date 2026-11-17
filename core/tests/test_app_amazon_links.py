import base64
import json

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from core import views
from core.models import AmazonAffiliateProduct, AndroidApp

URL_A = 'https://www.amazon.com/dp/B08XYZ/?tag=headhuntrr0c-21'
URL_B = 'https://www.amazon.com/dp/B09ABC/?tag=headhuntrr0c-21'


class AmazonPayloadTestCase(TestCase):
    """The links saved on the Amazon page are what the app config serves."""

    def setUp(self):
        cache.clear()
        self.payload = {
            'login_required': 0,
            'sync': 300,
            'ads1': {'click': 50, 'amazon1': {'tag': 'headhuntrr0c-21', 'url': []}},
        }
        self.app = AndroidApp.objects.create(
            name='Cinestream', slug='cinestream',
            access_username='app-user', access_password='app-pass',
            json_payload=self.payload,
        )

    def _auth(self):
        return 'Basic ' + base64.b64encode(b'app-user:app-pass').decode()

    def test_saved_links_land_in_the_amazon_block(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A, order=1)

        served = views._attach_amazon_links(self.app.json_payload)

        self.assertEqual([URL_A], served['ads1']['amazon1']['url'])
        # The tag and the rest of the block are left as they were.
        self.assertEqual('headhuntrr0c-21', served['ads1']['amazon1']['tag'])
        self.assertEqual(50, served['ads1']['click'])

    def test_links_come_out_in_the_saved_order(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A, order=5)
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_B, order=2)

        self.assertEqual([URL_B, URL_A], views._amazon_affiliate_urls())

    def test_disabled_links_are_not_served(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A, is_active=False)
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_B)

        self.assertEqual([URL_B], views._amazon_affiliate_urls())

    def test_a_link_saved_twice_is_served_once(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A)
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A.upper())

        self.assertEqual([URL_A], views._amazon_affiliate_urls())

    def test_a_payload_without_the_amazon_block_is_untouched(self):
        payload = {'ads1': {'click': 50}}
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A)

        self.assertEqual(payload, views._attach_amazon_links(payload))

    def test_the_stored_payload_is_not_modified(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A)

        views._attach_amazon_links(self.app.json_payload)

        self.assertEqual([], self.app.json_payload['ads1']['amazon1']['url'])

    def test_the_app_config_endpoint_serves_the_saved_links(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A, order=1)
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_B, order=2,
                                              is_active=False)

        response = self.client.get(
            reverse('android_app_endpoint', args=['cinestream']),
            HTTP_AUTHORIZATION=self._auth(),
        )

        self.assertEqual(200, response.status_code)
        data = json.loads(response.content)
        self.assertEqual([URL_A], data['ads1']['amazon1']['url'])
        self.assertEqual('headhuntrr0c-21', data['ads1']['amazon1']['tag'])

    def test_a_new_link_is_served_without_waiting_for_the_cache(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A, order=1)
        first = self.client.get(reverse('android_app_endpoint', args=['cinestream']),
                                HTTP_AUTHORIZATION=self._auth())
        self.assertEqual([URL_A], json.loads(first.content)['ads1']['amazon1']['url'])

        # Cached already, so this would miss the second link without the signal.
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_B, order=2)

        second = self.client.get(reverse('android_app_endpoint', args=['cinestream']),
                                 HTTP_AUTHORIZATION=self._auth())
        self.assertEqual([URL_A, URL_B],
                         json.loads(second.content)['ads1']['amazon1']['url'])
