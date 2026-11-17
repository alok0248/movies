"""The users-full page: one people table, its filters and its two row pops.

The page used to be eight tabs, then four. It is now two: everyone the app has
seen in one table, and the per-period chart. The verified, signed-out, blocked,
no-account, app-version and gone-quiet lists are filters on that one table, so
these tests hold the property that matters most — a filter changes which rows
are on the page and never which columns.
"""
import json
import pathlib
import re

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from core.admin_table_columns import TAB_SPECS, default_keys, filter_table_columns
from core.models import (AndroidApp, AndroidAppDevice, AndroidAppDeviceVisit,
                         AdminTableLayout, SyncedUser, UserMessage)

User = get_user_model()

# The order the table writes its columns in, and the contract the layout script
# and the picker both rely on: the two row actions are the last two columns.
PEOPLE_KEYS = ['check', 'who', 'status', 'account', 'app', 'ip',
               'subscription', 'activity', 'access', 'messages', 'details']


class Base(TestCase):
    def setUp(self):
        # The merged people list is cached for a minute under the app id, and
        # every test gets id 1 — without this each test reads the last one's
        # rows.
        cache.clear()
        self.app = AndroidApp.objects.create(
            name='CineStream', slug='cinestream', access_username='u',
            access_password='p')
        self.staff = User.objects.create_user('alok', 'alok@example.com', 'x',
                                              is_staff=True)
        self.customer = User.objects.create_user('viewer', 'viewer@example.com', 'x')
        self.client.force_login(self.staff)

    def url(self, **params):
        base = reverse('android_app_users_full', args=[self.app.id])
        query = '&'.join('%s=%s' % (k, v) for k, v in params.items())
        return base + ('?' + query if query else '')

    def headers(self, resp):
        html = resp.content.decode()
        at = html.find('data-tab-cols="')
        self.assertNotEqual(-1, at, 'no table with data-tab-cols on the page')
        head = html[at:].split('</thead>')[0]
        return re.findall(r'<th data-col="([a-z_]+)"', head)

    def island(self, resp, ident):
        match = re.search(r'id="%s"[^>]*>(.*?)</script>' % ident,
                          resp.content.decode(), re.S)
        self.assertIsNotNone(match, 'missing %s island' % ident)
        return json.loads(match.group(1))


class PeopleTableTest(Base):
    def test_the_page_has_two_tabs(self):
        resp = self.client.get(self.url(tab='people'))
        tabs = re.findall(r'class="fbtn tabbtn[^"]*" data-tab="([a-z]+)"',
                          resp.content.decode())
        self.assertEqual(['people', 'dailyusers'], tabs)

    def test_every_column_is_rendered_once_in_the_documented_order(self):
        resp = self.client.get(self.url(tab='people'))
        self.assertEqual(PEOPLE_KEYS, self.headers(resp))
        self.assertEqual(PEOPLE_KEYS, [c['key'] for c in resp.context['people_columns']])

    def test_the_two_row_actions_are_the_last_columns(self):
        self.assertEqual(['messages', 'details'], PEOPLE_KEYS[-2:])

    def test_a_filter_changes_rows_without_changing_columns(self):
        SyncedUser.objects.create(email='yes@example.com', display_name='Yes',
                                  is_email_verified=True, device_id='d-yes')
        SyncedUser.objects.create(email='no@example.com', display_name='No',
                                  is_email_verified=False, device_id='d-no')
        all_headers = None
        for view in ('all', 'online', 'verified', 'signedout', 'blocked',
                     'subscribed', 'free', 'noacc'):
            with self.subTest(view=view):
                resp = self.client.get(self.url(tab='people', f=view))
                self.assertEqual(200, resp.status_code)
                headers = self.headers(resp)
                self.assertEqual(PEOPLE_KEYS, headers)
                if all_headers is None:
                    all_headers = headers
                self.assertEqual(all_headers, headers)

        resp = self.client.get(self.url(tab='people', f='verified'))
        self.assertEqual(['yes@example.com'],
                         [r['email'] for r in resp.context['people_rows']])

    def test_the_old_tabs_all_land_on_the_one_table(self):
        for old in ('accounts', 'registered', 'signedout', 'versions',
                    'devices', 'inactive', 'uniquedevices', 'visits'):
            with self.subTest(old=old):
                resp = self.client.get(self.url(tab=old))
                self.assertEqual(200, resp.status_code, old)
                self.assertEqual('people', resp.context['active_tab'])
        self.assertEqual('verified', self.client.get(
            self.url(tab='registered')).context['view'])

    def test_every_filter_is_offered_on_the_one_table(self):
        html = self.client.get(self.url(tab='people')).content.decode()
        for marker in ('f=verified', 'f=signedout', 'f=blocked', 'f=noacc',
                       'name="nv"', 'name="sil"', 'name="version"', 'name="app"'):
            self.assertIn(marker, html)

    def test_the_silent_and_version_filters_narrow_the_table(self):
        SyncedUser.objects.create(email='old@example.com', display_name='Old',
                                  device_id='d-old', app_version='3.0.0')
        SyncedUser.objects.create(email='new@example.com', display_name='New',
                                  device_id='d-new', app_version='4.0.0')
        resp = self.client.get(self.url(tab='people', version='4.0.0'))
        self.assertEqual(['new@example.com'],
                         [r['email'] for r in resp.context['people_rows']])
        resp = self.client.get(self.url(tab='people', version='4.0.0', f='verified'))
        # No verified rows on that version, but the columns are still there.
        self.assertEqual([], [r['email'] for r in resp.context['people_rows']])
        self.assertEqual(PEOPLE_KEYS, self.headers(resp))

    def test_a_header_click_sorts_the_merged_rows(self):
        for name in ('carol', 'alice', 'bob'):
            SyncedUser.objects.create(email='%s@example.com' % name,
                                      display_name=name, device_id='d-' + name)
        resp = self.client.get(self.url(tab='people', sort='name', dir='asc'))
        self.assertEqual(['alice', 'bob', 'carol'],
                         [r['display_name'] for r in resp.context['people_rows']])
        resp = self.client.get(self.url(tab='people', sort='name', dir='desc'))
        self.assertEqual(['carol', 'bob', 'alice'],
                         [r['display_name'] for r in resp.context['people_rows']])

    def test_the_list_is_paginated(self):
        for i in range(30):
            SyncedUser.objects.create(email='u%02d@example.com' % i,
                                      device_id='d%02d' % i)
        resp = self.client.get(self.url(tab='people'))
        self.assertEqual(25, len(resp.context['people_rows']))
        self.assertEqual(2, resp.context['page_obj'].paginator.num_pages)

    def test_partial_returns_only_the_panel(self):
        resp = self.client.get(self.url(tab='people', partial=1))
        self.assertNotIn('<html', resp.content.decode())
        self.assertIn('data-tab-cols', resp.content.decode())

    def test_requires_staff(self):
        self.client.force_login(self.customer)
        self.assertEqual(302, self.client.get(self.url(tab='people')).status_code)


class MessagesColumnTest(Base):
    def setUp(self):
        super().setUp()
        self.profile = SyncedUser.objects.create(
            email='chat@example.com', display_name='Chatty', device_id='dev-chat')

    def test_an_unread_message_shows_a_red_number_on_the_row(self):
        UserMessage.objects.create(app=self.app, email='chat@example.com',
                                   device_id='dev-chat', body='please help')
        resp = self.client.get(self.url(tab='people'))
        html = resp.content.decode()
        self.assertContains(resp, 'badge bg-danger')
        self.assertIn('please help', html)
        self.assertEqual(1, resp.context['people_rows'][0]['unread'])

    def test_a_read_message_keeps_its_text_but_loses_the_number(self):
        UserMessage.objects.create(app=self.app, email='chat@example.com',
                                   body='seen already', is_read=True)
        resp = self.client.get(self.url(tab='people'))
        self.assertIn('seen already', resp.content.decode())
        self.assertEqual(0, resp.context['people_rows'][0]['unread'])

    def test_the_popup_opens_for_an_account_and_marks_the_thread_read(self):
        UserMessage.objects.create(app=self.app, email='chat@example.com',
                                   body='hello')
        resp = self.client.get(
            reverse('android_app_user_chat', args=[self.app.id, self.profile.pk]))
        self.assertEqual(200, resp.status_code)
        self.assertContains(resp, 'hello')
        self.assertTrue(UserMessage.objects.get().is_read)

    def test_the_popup_opens_for_a_device_with_no_account(self):
        UserMessage.objects.create(app=self.app, device_id='guest-dev',
                                   body='from a guest')
        url = (reverse('android_app_user_chat', args=[self.app.id, 0])
               + '?key=guest-dev')
        resp = self.client.get(url)
        self.assertEqual(200, resp.status_code)
        self.assertContains(resp, 'from a guest')

    def test_the_details_popup_shows_the_requests_that_device_made(self):
        device = AndroidAppDevice.objects.create(
            android_app=self.app, user_id='dev-chat', device_model='Pixel 7',
            total_visits=3)
        AndroidAppDeviceVisit.objects.create(
            android_app=self.app, device=device, device_model='Pixel 7',
            os_version='14', ip_address='1.2.3.4', full_request='GET /api/ HTTP/1.1')
        resp = self.client.get(reverse('android_app_user_details', args=[self.app.id])
                               + '?key=dev-chat')
        self.assertEqual(200, resp.status_code)
        self.assertContains(resp, 'GET /api/ HTTP/1.1')
        self.assertContains(resp, 'Pixel 7')

    def test_the_details_popup_opens_for_an_unknown_device(self):
        resp = self.client.get(reverse('android_app_user_details', args=[self.app.id])
                               + '?key=nothing-here')
        self.assertEqual(200, resp.status_code)

    def test_sending_an_answer_records_it_on_the_row(self):
        message = UserMessage.objects.create(app=self.app, email='chat@example.com',
                                             body='a question')
        url = reverse('android_app_user_chat', args=[self.app.id, self.profile.pk])
        resp = self.client.post(url, {'body': 'the answer'},
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(200, resp.status_code)
        message.refresh_from_db()
        self.assertEqual('the answer', message.reply_body)
        self.assertIsNotNone(message.replied_at)


class ColumnLayoutTest(Base):
    def post_layout(self, payload):
        return self.client.post(reverse('admin_table_layout'),
                                data=json.dumps(payload),
                                content_type='application/json')

    def test_the_layout_is_saved_per_admin_and_per_table(self):
        columns = ['who', 'messages', 'details']
        resp = self.post_layout({'tab': 'people', 'columns': columns})
        self.assertEqual(columns, resp.json()['columns'])
        self.assertEqual(columns, AdminTableLayout.objects.get(
            user=self.staff, tab='people').columns)
        page = self.client.get(self.url(tab='people'))
        self.assertEqual(columns, self.island(page, 'colLayoutIsland')['people'])

    def test_unknown_keys_are_dropped_and_repeats_collapse(self):
        resp = self.post_layout({'tab': 'people',
                                 'columns': ['who', 'who', 'not_a_column']})
        self.assertEqual(['who'], resp.json()['columns'])

    def test_reset_restores_the_defaults(self):
        self.post_layout({'tab': 'people', 'columns': ['who']})
        resp = self.post_layout({'tab': 'people', 'reset': True})
        self.assertEqual(default_keys('people'), resp.json()['columns'])

    def test_bad_requests_are_refused(self):
        self.assertEqual(400, self.post_layout({'tab': 'nope'}).status_code)
        self.assertEqual(400, self.post_layout(
            {'tab': 'people', 'columns': 'who'}).status_code)

    def test_a_hidden_column_is_not_sent_to_the_browser(self):
        SyncedUser.objects.create(email='a@example.com', display_name='Ada',
                                  device_id='d-a')
        self.post_layout({'tab': 'people',
                          'columns': ['who', 'messages', 'details']})
        resp = self.client.get(self.url(tab='people'))
        self.assertEqual(['who', 'messages', 'details'], self.headers(resp))
        self.assertNotIn('data-col="status"', resp.content.decode())

    def test_the_empty_message_spans_only_the_visible_columns(self):
        self.post_layout({'tab': 'people', 'columns': ['who', 'details']})
        resp = self.client.get(self.url(tab='people'))
        self.assertContains(resp, 'colspan="2"')

    def test_the_default_layout_ships_every_column(self):
        resp = self.client.get(self.url(tab='people'))
        head = resp.content.decode().split('</thead>')[0]
        labels = [re.sub(r'<[^>]+>', '', cell).strip()
                  for cell in re.findall(r'<th[^>]*>(.*?)</th>', head, re.S)]
        self.assertEqual([c['label'] for c in TAB_SPECS['people']], labels)
        self.assertContains(
            resp, 'data-tab-cols="%s"' % ','.join(default_keys('people')))


class ColumnFilterUnitTest(TestCase):
    TABLE = ('<table class="t" data-tab-cols="a,b,c"><thead><tr>'
             '<th>A</th><th>B</th><th>C</th></tr></thead><tbody>'
             '<tr><td>1</td><td>2</td><td>3</td></tr></tbody></table>')

    def test_the_default_order_is_returned_untouched(self):
        self.assertEqual(
            self.TABLE,
            filter_table_columns(self.TABLE, ['a', 'b', 'c'], ['a', 'b', 'c']))

    def test_cells_without_data_col_are_matched_by_position(self):
        out = filter_table_columns(self.TABLE, ['c', 'a'], ['a', 'b', 'c'])
        self.assertIn('<th>C</th><th>A</th>', out)
        self.assertNotIn('<td>2</td>', out)

    def test_data_col_names_the_column_even_out_of_markup_order(self):
        table = ('<table data-tab-cols="a,b"><thead><tr>'
                 '<th data-col="b">B</th><th data-col="a">A</th></tr></thead>'
                 '<tbody><tr><td data-col="b">2</td><td data-col="a">1</td>'
                 '</tr></tbody></table>')
        out = filter_table_columns(table, ['a'], ['a', 'b'])
        self.assertIn('<th data-col="a">A</th>', out)
        self.assertNotIn('<th data-col="b">B</th>', out)

    def test_a_spanning_row_keeps_its_message_and_narrows_the_span(self):
        table = ('<table data-tab-cols="a,b,c"><tbody><tr>'
                 '<td colspan="3">Nothing here</td></tr></tbody></table>')
        out = filter_table_columns(table, ['a', 'b'], ['a', 'b', 'c'])
        self.assertIn('colspan="2"', out)
        self.assertIn('Nothing here', out)

    def test_no_layout_returns_the_input(self):
        self.assertEqual(self.TABLE, filter_table_columns(self.TABLE, [], ['a']))

    def test_unclosed_markup_never_raises(self):
        broken = '<table data-tab-cols="a"><tr><td>oops'
        self.assertIn('data-tab-cols', filter_table_columns(broken, ['a'], ['a', 'b']))


class SpecTest(TestCase):
    def test_the_people_spec_is_the_documented_order(self):
        self.assertEqual(PEOPLE_KEYS, [c['key'] for c in TAB_SPECS['people']])

    def test_specs_are_unique_per_table(self):
        for tab, specs in TAB_SPECS.items():
            keys = [c['key'] for c in specs]
            self.assertEqual(len(keys), len(set(keys)), tab)
            self.assertTrue(keys, tab)

    def test_the_markup_has_one_header_per_declared_column(self):
        base = pathlib.Path(__file__).resolve().parents[2] / 'templates' / 'core'
        text = (base / '_users_full_people.html').read_text(encoding='utf-8')
        self.assertIn('{% for c in people_columns %}', text)
        self.assertIn('data-tab-cols', text)
        for key in PEOPLE_KEYS:
            self.assertIn('data-col="%s"' % key, text)
        self.assertEqual(PEOPLE_KEYS, re.findall(r'data-col="([a-z_]+)"', text))
