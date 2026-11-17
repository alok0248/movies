"""The Registered & verified tab and the per-admin column layouts.

The users-full page has one tab per table. "Registered & verified" lists the
profiles whose email address has actually been proven, with a searchable,
sortable, paginated table; every tab can have its columns chosen and ordered by
the admin (`core.admin_table_columns`) and the choice is stored per admin, not
per app.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.admin_table_columns import TAB_SPECS, default_keys
from core.models import AndroidApp, AdminTableLayout, SyncedUser

User = get_user_model()


class ColumnLayoutBase(TestCase):
    def setUp(self):
        self.app = AndroidApp.objects.create(
            name='CineStream', slug='cinestream', access_username='u',
            access_password='p')
        self.staff = User.objects.create_user('alok', 'alok@example.com', 'x',
                                              is_staff=True)
        self.other_staff = User.objects.create_user('ops', 'ops@example.com', 'x',
                                                    is_staff=True)
        self.customer = User.objects.create_user('viewer', 'viewer@example.com',
                                                 'x')
        self.client.force_login(self.staff)

    def users_url(self, **params):
        url = reverse('android_app_users_full', args=[self.app.id])
        query = '&'.join('%s=%s' % (k, v) for k, v in params.items())
        return url + ('?' + query if query else '')

    def layout_url(self):
        return reverse('admin_table_layout')

    def island(self, resp, ident):
        """Read one of the page's |json_script islands the way the browser does."""
        import re
        match = re.search(r'id="%s"[^>]*>(.*?)</script>' % ident,
                          resp.content.decode(), re.S)
        self.assertIsNotNone(match, 'missing %s island' % ident)
        return json.loads(match.group(1))


class RegisteredTabTest(ColumnLayoutBase):
    def test_lists_only_verified_addresses(self):
        SyncedUser.objects.create(email='verified@example.com',
                                  display_name='Verified', is_email_verified=True)
        SyncedUser.objects.create(email='unverified@example.com',
                                  display_name='Unverified',
                                  is_email_verified=False)

        resp = self.client.get(self.users_url(tab='registered'))

        self.assertEqual(200, resp.status_code)
        emails = [row['obj'].email for row in resp.context['rows']]
        self.assertEqual(['verified@example.com'], emails)
        self.assertEqual(1, resp.context['counts']['registered'])
        self.assertEqual(2, resp.context['counts']['total'])

    def test_search_narrows_the_registered_list(self):
        SyncedUser.objects.create(email='a@example.com', display_name='A',
                                  is_email_verified=True)
        SyncedUser.objects.create(email='b@example.com', display_name='B',
                                  is_email_verified=True)

        resp = self.client.get(self.users_url(tab='registered', q='a@example'))

        self.assertEqual(['a@example.com'],
                         [row['obj'].email for row in resp.context['rows']])

    def test_a_header_click_sorts_the_whole_table(self):
        for name in ('carol', 'alice', 'bob'):
            SyncedUser.objects.create(email='%s@example.com' % name,
                                      display_name=name, is_email_verified=True)

        resp = self.client.get(self.users_url(tab='registered', sort='email',
                                              dir='asc'))
        ascending = [row['obj'].email for row in resp.context['rows']]
        self.assertEqual(sorted(ascending), ascending)

        resp = self.client.get(self.users_url(tab='registered', sort='email',
                                              dir='desc'))
        descending = [row['obj'].email for row in resp.context['rows']]
        self.assertEqual(sorted(descending, reverse=True), descending)

    def test_sorting_by_a_column_the_tab_does_not_have_is_ignored(self):
        SyncedUser.objects.create(email='x@example.com', is_email_verified=True)
        resp = self.client.get(self.users_url(tab='registered', sort='password',
                                              dir='desc'))
        self.assertEqual(200, resp.status_code)
        self.assertEqual(1, len(resp.context['rows']))

    def test_the_list_is_paginated(self):
        for i in range(30):
            SyncedUser.objects.create(email='user%02d@example.com' % i,
                                      is_email_verified=True)

        first = self.client.get(self.users_url(tab='registered'))
        self.assertEqual(25, len(first.context['rows']))
        self.assertEqual(2, first.context['page_obj'].paginator.num_pages)

        second = self.client.get(self.users_url(tab='registered', page=2))
        self.assertEqual(5, len(second.context['rows']))

    def test_the_tab_is_on_the_page_and_needs_staff(self):
        resp = self.client.get(self.users_url(tab='registered'))
        self.assertContains(resp, 'Registered &amp; verified')

        self.client.force_login(self.customer)
        self.assertEqual(302, self.client.get(self.users_url(tab='registered')).status_code)

    def test_every_column_of_the_tab_is_a_sortable_header(self):
        SyncedUser.objects.create(email='x@example.com', is_email_verified=True)
        resp = self.client.get(self.users_url(tab='registered'))
        columns = resp.context['registered_columns']
        self.assertEqual(default_keys('registered'), [c['key'] for c in columns])
        self.assertTrue(any(c['sortable'] for c in columns))
        self.assertEqual(1, sum(1 for c in columns if c['active']))


class ColumnLayoutEndpointTest(ColumnLayoutBase):
    def post_layout(self, payload):
        return self.client.post(self.layout_url(), data=json.dumps(payload),
                                content_type='application/json')

    def test_saves_the_order_for_this_admin(self):
        columns = ['email', 'user', 'manage']
        resp = self.post_layout({'tab': 'registered', 'columns': columns})
        self.assertEqual(200, resp.status_code)
        self.assertEqual(columns, resp.json()['columns'])

        saved = AdminTableLayout.objects.get(user=self.staff, tab='registered')
        self.assertEqual(columns, saved.columns)

        page = self.client.get(self.users_url(tab='registered'))
        self.assertEqual(columns, self.island(page, 'colLayoutIsland')['registered'])

    def test_layouts_are_per_admin(self):
        self.post_layout({'tab': 'visits', 'columns': ['ip', 'time']})
        self.client.force_login(self.other_staff)
        page = self.client.get(self.users_url(tab='visits'))
        self.assertEqual(default_keys('visits'),
                         self.island(page, 'colLayoutIsland')['visits'])

    def test_unknown_keys_are_dropped_and_repeats_collapse(self):
        resp = self.post_layout({'tab': 'accounts',
                                 'columns': ['user', 'user', 'not_a_column']})
        self.assertEqual(['user'], resp.json()['columns'])

    def test_an_empty_list_falls_back_to_the_default_order(self):
        resp = self.post_layout({'tab': 'versions', 'columns': []})
        self.assertEqual(default_keys('versions'), resp.json()['columns'])

    def test_reset_restores_the_defaults(self):
        self.post_layout({'tab': 'signedout', 'columns': ['actions']})
        resp = self.post_layout({'tab': 'signedout', 'reset': True})
        self.assertEqual(default_keys('signedout'), resp.json()['columns'])

    def test_bad_requests_are_refused(self):
        self.assertEqual(400, self.post_layout({'tab': 'nope'}).status_code)
        self.assertEqual(400, self.post_layout({'tab': 'visits',
                                                'columns': 'ip'}).status_code)
        resp = self.client.post(self.layout_url(), data='not json',
                                content_type='application/json')
        self.assertEqual(400, resp.status_code)

    def test_requires_staff_and_post(self):
        self.assertEqual(405, self.client.get(self.layout_url()).status_code)
        self.client.force_login(self.customer)
        resp = self.post_layout({'tab': 'visits', 'columns': ['ip']})
        self.assertEqual(302, resp.status_code)


class TableColumnSpecTest(ColumnLayoutBase):
    def test_every_table_renders_its_columns_for_the_layout_script(self):
        for tab in ('accounts', 'registered', 'visits', 'uniquedevices'):
            resp = self.client.get(self.users_url(tab=tab))
            self.assertContains(resp, 'data-tab-cols="%s"' % ','.join(default_keys(tab)))

    def test_every_tab_still_renders(self):
        SyncedUser.objects.create(email='a@example.com', is_email_verified=True,
                                  device_model='Pixel 7', app_version='3.4.8.694')
        for tab in ('accounts', 'registered', 'versions', 'uniquedevices',
                    'inactive', 'signedout', 'visits', 'dailyusers'):
            with self.subTest(tab=tab):
                resp = self.client.get(self.users_url(tab=tab))
                self.assertEqual(200, resp.status_code, tab)
                self.assertEqual(tab, resp.context['active_tab'])

    def test_the_layout_islands_are_objects_the_page_script_can_read(self):
        """json_script must serialise the dicts themselves; a pre-dumped JSON
        string would arrive double encoded and the script could not use it."""
        resp = self.client.get(self.users_url(tab='accounts'))
        specs = self.island(resp, 'colSpecsIsland')
        layout = self.island(resp, 'colLayoutIsland')
        for tab in TAB_SPECS:
            self.assertIsInstance(specs[tab], list, tab)
            self.assertIsInstance(layout[tab], list, tab)
            self.assertEqual(default_keys(tab), layout[tab], tab)
        self.assertEqual('accounts', self.island(resp, 'activeTabIsland'))

    def test_the_pencil_button_is_on_the_page(self):
        resp = self.client.get(self.users_url(tab='accounts'))
        self.assertContains(resp, 'id="colCfgBtn"')
        self.assertContains(resp, 'id="colCfgOverlay"')

    def test_each_table_markup_has_one_header_per_declared_column(self):
        """The layout script maps cells to columns by position, so the header
        count and the spec length must agree for every tab."""
        import pathlib
        import re
        templates = {
            'accounts': '_users_full_accounts.html',
            'registered': '_users_full_registered.html',
            'versions': '_users_full_versions.html',
            'uniquedevices': '_users_full_uniquedevices.html',
            'inactive': '_users_full_inactive.html',
            'signedout': '_users_full_signedout.html',
            'visits': '_users_full_visits.html',
            'dailyusers': '_users_full_dailyusers.html',
        }
        base = pathlib.Path(__file__).resolve().parents[2] / 'templates' / 'core'
        for tab, name in templates.items():
            text = (base / name).read_text(encoding='utf-8')
            self.assertIn('data-tab-cols', text, tab)
            if tab == 'registered':
                # This table builds its headers from the column list, so the
                # markup has one <th> inside a loop instead of one per column.
                self.assertIn('{% for c in registered_columns %}', text)
                continue
            head = text.split('data-tab-cols')[1].split('</thead>')[0]
            # '<th' would also match '<thead', so require a tag boundary.
            self.assertEqual(len(default_keys(tab)),
                             len(re.findall(r'<th[\s>]', head)), tab)

    def test_specs_are_unique_per_tab(self):
        for tab, specs in TAB_SPECS.items():
            keys = [c['key'] for c in specs]
            self.assertEqual(len(keys), len(set(keys)), tab)
            self.assertTrue(keys, tab)
