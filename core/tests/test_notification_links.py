"""Links in a notification read as a name, not as a full address.

The admin can attach a link and often writes one into the message text. Both
are shown under a short name (the file or the host) and open the real URL.
"""
from django.test import TestCase

from core.linktext import clean_url, link_label, linkify
from core.models import AppNotification


class LinkLabelTest(TestCase):
    def test_file_name(self):
        self.assertEqual(
            'CineStream_v3.7.024.apk',
            link_label('https://cdn.example.com/files/CineStream_v3.7.024.apk'))

    def test_url_encoded_name_is_decoded(self):
        self.assertEqual(
            'Saavn Music.apk',
            link_label('https://cdn.example.com/android_apks/Saavn%20Music.apk'))

    def test_query_string_is_dropped(self):
        self.assertEqual(
            'movie.apk',
            link_label('https://cdn.example.com/dl/movie.apk?token=abc123'))

    def test_site_root_uses_the_host(self):
        self.assertEqual('example.com', link_label('https://www.example.com/'))

    def test_trailing_punctuation(self):
        self.assertEqual('page', link_label('https://example.com/page.'))
        self.assertEqual('https://example.com/page', clean_url('https://example.com/page).'))

    def test_no_link(self):
        self.assertEqual('', link_label(''))
        self.assertEqual('', link_label(None))


class LinkifyTest(TestCase):
    def test_url_in_the_text_shows_its_name(self):
        html = linkify('Download here: https://cdn.example.com/files/app.apk today')
        self.assertIn('>app.apk</a>', html)
        self.assertIn('href="https://cdn.example.com/files/app.apk"', html)
        self.assertNotIn('>https://cdn.example.com/files/app.apk<', html)

    def test_surrounding_text_is_kept(self):
        html = linkify('Download here: https://cdn.example.com/files/app.apk today')
        self.assertTrue(html.startswith('Download here: '))
        self.assertTrue(html.endswith(' today'))

    def test_html_is_escaped(self):
        html = linkify('<script>alert(1)</script> https://example.com/x')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_links_open_in_a_new_tab(self):
        html = linkify('https://example.com/x')
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_text_without_a_link_is_unchanged(self):
        self.assertEqual('no links here', linkify('no links here'))


class NotificationLinkNameTest(TestCase):
    def test_attached_link_has_a_name(self):
        note = AppNotification(title='New build', link_url='https://cdn.example.com/a/b/app.apk')
        self.assertEqual('app.apk', note.link_name)

    def test_notification_without_a_link(self):
        self.assertEqual('', AppNotification(title='Hello').link_name)
