"""The Amazon affiliate links page.

It exists so a list of affiliate links can be pasted in whole instead of one
field at a time: one link per line, an optional title either side of a pipe,
duplicates skipped, and anything that is not a full http(s) URL reported back.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import AmazonAffiliateProduct

URL_A = 'https://www.amazon.com/dp/B08XYZ/?tag=headhuntrr005-21'
URL_B = 'https://www.amazon.in/dp/B09ABC/?tag=headhuntrr005-21'
URL_C = 'https://www.amazon.com/dp/B07DEF/?tag=headhuntrr005-21'


class AmazonLinksPageTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', 'boss@example.com', 'pw',
                                              is_staff=True)
        self.client.force_login(self.staff)

    def page_url(self):
        return reverse('admin_amazon_links')

    def bulk(self, text):
        return self.client.post(self.page_url(), {'action': 'bulk', 'links': text})

    def single(self, **fields):
        data = {'action': 'single'}
        data.update(fields)
        return self.client.post(self.page_url(), data)

    def test_staff_only(self):
        self.client.logout()
        self.assertEqual(302, self.client.get(self.page_url()).status_code)

    def test_the_page_lists_the_saved_links(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A, title='Earbuds')
        response = self.client.get(self.page_url())
        self.assertEqual(200, response.status_code)
        self.assertContains(response, URL_A)
        self.assertContains(response, 'Earbuds')
        self.assertContains(response, 'Add these links')

    def test_the_empty_state(self):
        response = self.client.get(self.page_url())
        self.assertContains(response, 'No Amazon links yet')

    def test_several_links_are_added_in_one_go(self):
        response = self.bulk('\n'.join([
            URL_A,
            'Wireless earbuds | ' + URL_B,
            URL_C + ' | Power bank',
            '',
            '# a comment line',
        ]))
        self.assertEqual(302, response.status_code)

        saved = {p.affiliate_url: p for p in AmazonAffiliateProduct.objects.all()}
        self.assertEqual({URL_A, URL_B, URL_C}, set(saved))
        self.assertEqual('', saved[URL_A].title)
        self.assertEqual('Wireless earbuds', saved[URL_B].title)
        self.assertEqual('Power bank', saved[URL_C].title)
        self.assertTrue(all(p.is_active for p in saved.values()))

    def test_the_new_links_come_after_the_ones_already_there(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A, order=7)
        self.bulk(URL_B + '\n' + URL_C)
        self.assertEqual(8, AmazonAffiliateProduct.objects.get(affiliate_url=URL_B).order)
        self.assertEqual(9, AmazonAffiliateProduct.objects.get(affiliate_url=URL_C).order)

    def test_a_link_already_saved_is_skipped(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A)
        self.bulk('\n'.join([URL_A, URL_B]))
        # The duplicate adds nothing; the second link does.
        self.assertEqual(2, AmazonAffiliateProduct.objects.count())
        self.assertEqual(URL_B, AmazonAffiliateProduct.objects.order_by('order')
                         .last().affiliate_url)
        self.assertContains(self.client.get(self.page_url()), 'already saved, skipped',
                            status_code=200)

    def test_a_line_that_is_not_a_link_is_reported_and_not_saved(self):
        response = self.bulk('\n'.join([URL_A, 'not a link', 'ftp://example.com/x']))
        self.assertEqual(302, response.status_code)
        self.assertEqual([URL_A],
                         list(AmazonAffiliateProduct.objects.values_list(
                             'affiliate_url', flat=True)))
        page = self.client.get(self.page_url())
        self.assertContains(page, 'not a full http(s) link')

    def test_an_empty_box_says_so(self):
        response = self.bulk('   \n\n')
        self.assertEqual(302, response.status_code)
        self.assertEqual(0, AmazonAffiliateProduct.objects.count())
        self.assertContains(self.client.get(self.page_url()), 'Nothing to add')

    def test_one_link_can_be_added_from_the_fields(self):
        response = self.single(affiliate_url=URL_A, title='Earbuds',
                               image_url='https://m.media-amazon.com/x.jpg',
                               price='$29.99')
        self.assertEqual(302, response.status_code)

        product = AmazonAffiliateProduct.objects.get()
        self.assertEqual(URL_A, product.affiliate_url)
        self.assertEqual('Earbuds', product.title)
        self.assertEqual('$29.99', product.price)
        self.assertEqual('https://m.media-amazon.com/x.jpg', product.image_url)

    def test_a_duplicate_single_link_is_refused(self):
        AmazonAffiliateProduct.objects.create(affiliate_url=URL_A)
        self.single(affiliate_url=URL_A)
        self.assertEqual(1, AmazonAffiliateProduct.objects.count())
        self.assertContains(self.client.get(self.page_url()), 'already saved')

    def test_a_single_link_must_be_a_full_url(self):
        self.single(affiliate_url='www.amazon.com/dp/B08XYZ')
        self.assertEqual(0, AmazonAffiliateProduct.objects.count())
        self.assertContains(self.client.get(self.page_url()),
                            'Enter a full http(s) Amazon affiliate link')

    def test_a_link_can_be_disabled_and_enabled_from_the_page(self):
        product = AmazonAffiliateProduct.objects.create(affiliate_url=URL_A)
        response = self.client.post(
            reverse('ad_toggle_any', args=['amazon', product.id]),
            {'next': self.page_url()})
        self.assertEqual(self.page_url(), response['Location'])
        product.refresh_from_db()
        self.assertFalse(product.is_active)

        self.client.post(reverse('ad_toggle_any', args=['amazon', product.id]),
                         {'next': self.page_url()})
        product.refresh_from_db()
        self.assertTrue(product.is_active)

    def test_a_link_can_be_deleted(self):
        product = AmazonAffiliateProduct.objects.create(affiliate_url=URL_A)
        response = self.client.post(self.page_url(),
                                    {'action': 'delete', 'id': product.id})
        self.assertEqual(302, response.status_code)
        self.assertEqual(0, AmazonAffiliateProduct.objects.count())

    def test_the_buttons_are_on_the_ads_page_and_the_app_edit_page(self):
        self.assertContains(self.client.get(reverse('ad_list')),
                            'href="%s"' % self.page_url())

        from core.models import AndroidApp
        app = AndroidApp.objects.create(name='CineStream', slug='cinestream',
                                        access_username='u', access_password='p')
        self.assertContains(self.client.get(reverse('android_app_edit', args=[app.id])),
                            'href="%s"' % self.page_url())
