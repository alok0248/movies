"""The Ad links page the Ads page opens.

It lists every link added so far — Adsterra direct links/smartlinks and Amazon
affiliate products — with the date it was added, its clicks and a button that
turns it on or off.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import AdsterraLink, AmazonAffiliateProduct


class AdLinksPageTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw',
                                              is_staff=True)
        self.client.force_login(self.staff)
        self.link = AdsterraLink.objects.create(
            name='Popunder_1', ad_type='Popunder',
            url='https://link.amazon/B0jcmYlGv', click_count=17)
        self.amazon = AmazonAffiliateProduct.objects.create(
            affiliate_url='https://www.amazon.com/dp/B08XYZ/?tag=yourname-20',
            title='Wireless earbuds', price='$29.99')

    def page_url(self):
        return reverse('ad_links')

    def test_staff_only(self):
        self.client.logout()
        self.assertEqual(302, self.client.get(self.page_url()).status_code)

    def test_the_ads_page_has_the_add_links_button(self):
        response = self.client.get(reverse('ad_list'))
        self.assertContains(response, 'href="%s"' % self.page_url())
        self.assertContains(response, '+ Add links')

    def test_every_added_link_is_listed(self):
        response = self.client.get(self.page_url())
        self.assertEqual(200, response.status_code)

        self.assertContains(response, 'https://link.amazon/B0jcmYlGv')
        self.assertContains(response, 'Popunder_1')
        self.assertContains(response, 'Wireless earbuds')
        self.assertContains(response, 'yourname-20')

    def test_the_added_date_and_clicks_are_shown(self):
        response = self.client.get(self.page_url())
        html = response.content.decode()
        self.assertIn(self.link.created_at.strftime('%Y-%m-%d'), html)
        self.assertIn('>17<', html.replace('\n', '').replace(' ', ''))

    def test_the_totals_are_counted(self):
        AdsterraLink.objects.create(name='Off', url='https://example.invalid/x',
                                    is_active=False)
        response = self.client.get(self.page_url())
        # Counted from the models: the store may already hold seeded rows.
        self.assertEqual(
            AdsterraLink.objects.count() + AmazonAffiliateProduct.objects.count(),
            response.context['link_count'])
        self.assertEqual(
            AdsterraLink.objects.filter(is_active=True).count()
            + AmazonAffiliateProduct.objects.filter(is_active=True).count(),
            response.context['active_count'])

    def test_a_link_with_only_js_code_says_so(self):
        AdsterraLink.objects.create(name='Social bar', code='<script>x</script>')
        response = self.client.get(self.page_url())
        self.assertContains(response, 'JS ad code saved, no link')

    def test_the_empty_state(self):
        AdsterraLink.objects.all().delete()
        AmazonAffiliateProduct.objects.all().delete()
        response = self.client.get(self.page_url())
        self.assertContains(response, 'No links added yet')

    def test_a_button_flips_a_link_and_returns_to_this_page(self):
        response = self.client.post(
            reverse('ad_toggle_any', args=['adsterra', self.link.id]),
            {'next': self.page_url()})

        self.assertEqual(302, response.status_code)
        self.assertEqual(self.page_url(), response['Location'])
        self.link.refresh_from_db()
        self.assertFalse(self.link.is_active)

    def test_a_button_flips_an_amazon_link_too(self):
        self.client.post(reverse('ad_toggle_any', args=['amazon', self.amazon.id]),
                         {'next': self.page_url()})
        self.amazon.refresh_from_db()
        self.assertFalse(self.amazon.is_active)

    def test_a_foreign_next_is_ignored(self):
        response = self.client.post(
            reverse('ad_toggle_any', args=['adsterra', self.link.id]),
            {'next': 'https://evil.example.com/steal'})
        self.assertEqual(302, response.status_code)
        self.assertNotIn('evil.example.com', response['Location'])
        self.assertIn(reverse('ad_list'), response['Location'])
