"""The user list's two new row actions: the Message popup and Login as.

Message shows the device's conversation with the admin beside its watch
history, and sends the next answer; Login as mints a session that renders the
website as that user from a URL, so it needs no cookie (it works in an
incognito window) and never touches the admin's own session.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (AndroidApp, AppNotification, AppNotificationRecipient,
                         PlayHistory, SyncedUser, UserMessage)
from core.preview_middleware import session_store

User = get_user_model()


class UserChatBase(TestCase):
    def setUp(self):
        self.app = AndroidApp.objects.create(
            name='CineStream', slug='cinestream', access_username='u',
            access_password='p')
        self.staff = User.objects.create_user('alok', 'alok@example.com', 'x',
                                              is_staff=True)
        self.customer = User.objects.create_user('viewer', 'viewer@example.com',
                                                 'x')
        self.profile = SyncedUser.objects.create(
            user=self.customer, email='viewer@example.com', display_name='Viewer',
            device_id='device-1', app_version='3.4.8', build_number=682)
        self.client.force_login(self.staff)

    def chat_url(self, user_id=None):
        return reverse('android_app_user_chat',
                       args=[self.app.id, user_id or self.profile.id])

    def login_as_url(self, user_id=None):
        return reverse('android_app_login_as',
                       args=[self.app.id, user_id or self.profile.id])


class ChatPanelTest(UserChatBase):
    def test_staff_only(self):
        self.client.logout()
        resp = self.client.get(self.chat_url())
        self.assertEqual(302, resp.status_code)
        self.client.force_login(self.customer)
        self.assertEqual(302, self.client.get(self.chat_url()).status_code)

    def test_panel_shows_the_thread_and_the_watch_history(self):
        message = UserMessage.objects.create(
            app=self.app, email=self.profile.email, device_id='device-1',
            body='Please add subtitles to this title')
        message.reply_body = 'Subtitles are on the way.'
        message.replied_at = timezone.now()
        message.save()
        PlayHistory.objects.create(user=self.customer, tmdb_id=11,
                                   media_type='movie', title='Sample Movie',
                                   duration_seconds=600,
                                   total_duration_seconds=1200)
        resp = self.client.get(self.chat_url())
        self.assertEqual(200, resp.status_code)
        self.assertContains(resp, 'Please add subtitles to this title')
        self.assertContains(resp, 'Subtitles are on the way.')
        self.assertContains(resp, 'Sample Movie')

    def test_guest_messages_of_the_same_device_join_the_thread(self):
        UserMessage.objects.create(app=self.app, email='', device_id='device-1',
                                   body='Guest question', kind=UserMessage.KIND_SUGGESTION)
        resp = self.client.get(self.chat_url())
        self.assertContains(resp, 'Guest question')

    def test_sending_answers_the_waiting_message(self):
        message = UserMessage.objects.create(
            app=self.app, email=self.profile.email, device_id='device-1',
            body='Where is my download?')
        resp = self.client.post(self.chat_url(), {'body': 'Re-download it now.'},
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(200, resp.status_code)
        message.refresh_from_db()
        self.assertEqual('Re-download it now.', message.reply_body)
        self.assertIsNotNone(message.replied_at)
        self.assertEqual('alok', message.replied_by)
        # The answer also travels the way every other message does.
        note = AppNotification.objects.filter(
            recipients__email=self.profile.email).first()
        self.assertIsNotNone(note)
        self.assertContains(resp, 'Re-download it now.')

    def test_sending_without_a_thread_creates_an_inbox_message(self):
        resp = self.client.post(self.chat_url(), {'body': 'Your plan expires soon.'},
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(200, resp.status_code)
        note = AppNotification.objects.get(title='Message from support')
        self.assertEqual('Your plan expires soon.', note.body)
        self.assertTrue(AppNotificationRecipient.objects.filter(
            notification=note, email=self.profile.email).exists())
        self.assertContains(resp, 'Your plan expires soon.')

    def test_a_user_without_a_website_account_can_still_be_messaged(self):
        app_only = SyncedUser.objects.create(email='apponly@example.com',
                                             display_name='App only',
                                             device_id='device-2')
        resp = self.client.get(self.chat_url(app_only.id))
        self.assertEqual(200, resp.status_code)
        self.assertContains(resp, 'No watch history synced')
        resp = self.client.post(self.chat_url(app_only.id),
                                {'body': 'Welcome aboard.'},
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(200, resp.status_code)
        self.assertTrue(AppNotificationRecipient.objects.filter(
            email='apponly@example.com').exists())

    def test_an_empty_message_is_refused(self):
        resp = self.client.post(self.chat_url(), {'body': '   '},
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertContains(resp, 'Write a message first.')

    def test_a_post_without_xhr_returns_to_the_list(self):
        resp = self.client.post(self.chat_url(), {'body': 'Hi there.'})
        self.assertEqual(302, resp.status_code)
        self.assertIn(reverse('android_app_users_full', args=[self.app.id]),
                      resp['Location'])


class LoginAsPreviewTest(UserChatBase):
    def test_requires_post_and_staff(self):
        self.assertEqual(405, self.client.get(self.login_as_url()).status_code)
        self.client.force_login(self.customer)
        self.assertEqual(302, self.client.post(self.login_as_url()).status_code)

    def test_mints_a_preview_session_for_the_user(self):
        resp = self.client.post(self.login_as_url(),
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(200, resp.status_code)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['url'].startswith('/preview/'))
        key = data['url'].split('/')[2]
        session = session_store()(session_key=key)
        session.load()
        self.assertEqual(str(self.customer.pk), session['_auth_user_id'])
        self.assertEqual('alok', session['preview_by'])
        self.assertEqual(self.profile.email, session['preview_email'])

    def test_refused_without_a_website_account(self):
        app_only = SyncedUser.objects.create(email='apponly@example.com',
                                             device_id='device-2')
        resp = self.client.post(self.login_as_url(app_only.id))
        self.assertEqual(400, resp.status_code)
        self.assertFalse(resp.json()['ok'])

    def test_refused_for_an_administrator_account(self):
        admin_profile = SyncedUser.objects.create(user=self.staff,
                                                  email='alok2@example.com',
                                                  device_id='device-3')
        resp = self.client.post(self.login_as_url(admin_profile.id))
        self.assertEqual(400, resp.status_code)

    def test_preview_renders_the_site_without_touching_the_admin_session(self):
        key = self.client.post(self.login_as_url()).json()['url'].split('/')[2]
        admin_id = self.client.session['_auth_user_id']
        resp = self.client.get('/preview/%s/' % key)
        self.assertEqual(200, resp.status_code)
        # Links stay inside the preview window, and the banner offers the way out.
        self.assertContains(resp, '/preview/%s/preview-exit/' % key)
        self.assertContains(resp, 'Previewing the site as')
        # The admin's own session is exactly as it was.
        self.assertEqual(admin_id, self.client.session['_auth_user_id'])
        self.assertEqual(str(self.staff.pk), self.client.session['_auth_user_id'])
        self.assertEqual(200, self.client.get(
            reverse('android_app_users_full', args=[self.app.id])).status_code)

    def test_an_unknown_or_plain_session_key_is_not_a_preview(self):
        self.assertEqual(404, self.client.get('/preview/%s/' % ('0' * 32)).status_code)
        # A real sign-in session is not replayable through the URL.
        plain = str(self.client.session.session_key)
        self.assertEqual(404, self.client.get('/preview/%s/' % plain).status_code)

    def test_exit_closes_the_preview_and_returns_to_the_list(self):
        key = self.client.post(self.login_as_url()).json()['url'].split('/')[2]
        resp = self.client.get('/preview/%s/preview-exit/' % key)
        self.assertEqual(302, resp.status_code)
        self.assertIn(reverse('android_app_users_full', args=[self.app.id]),
                      resp['Location'])
        self.assertEqual(404, self.client.get('/preview/%s/' % key).status_code)

    def test_exit_is_refused_outside_a_preview(self):
        self.assertEqual(403, self.client.get('/preview-exit/').status_code)
