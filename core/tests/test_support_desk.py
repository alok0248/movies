"""S23: the support desk around the app's message channel.

What matters here is the behaviour a user can feel: the first message is
acknowledged immediately, an answer asks one question and only once, a rating
cannot be rewritten by a second click, and the console shows who is waiting too
long. The canned replies have to be editable without a deploy.
"""
import base64
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from core import support
from core.auth import create_user_token_pair
from core.models import (AndroidApp, AppNotification, AppNotificationRecipient,
                         CannedReply, SupportTicket, SyncedUser, UserMessage)


class SupportDeskBase(TestCase):
    def setUp(self):
        self.app = AndroidApp.objects.create(
            name='CineStream Desk', slug='cinestream-desk',
            access_username='shared', access_password='secret', is_active=True)
        self.user = User.objects.create_user(username='asker@example.invalid',
                                             email='asker@example.invalid')
        self.token = create_user_token_pair(self.user)['token']
        SyncedUser.objects.create(user=self.user, email='asker@example.invalid')
        self.basic = 'Basic ' + base64.b64encode(b'shared:secret').decode()
        self.client = Client()

    def headers(self, token=None):
        headers = {'HTTP_AUTHORIZATION': self.basic,
                   'HTTP_USER_AGENT': 'CineStream/3.4.8',
                   'HTTP_HOST': 'testserver'}
        if token:
            headers['HTTP_X_USER_TOKEN'] = token
        return headers

    def app_message(self, body='Where is my subscription?', email='asker@example.invalid'):
        payload = json.dumps({'email': email, 'body': body, 'deviceId': 'dev-1'})
        return self.client.post('/api/messages/', payload,
                                content_type='application/json',
                                **self.headers(self.token))

    def staff_client(self):
        admin = User.objects.create_superuser(username='desk-op',
                                              email='desk@example.invalid',
                                              password='pw')
        client = Client(SERVER_NAME='newmovies.linkpc.net')
        client.force_login(admin)
        return client


class AutoAcknowledgeTest(SupportDeskBase):
    def test_the_first_message_is_acknowledged_immediately(self):
        response = self.app_message()
        self.assertEqual(200, response.status_code, response.content[:200])
        note = AppNotification.objects.filter(title=support.ACK_TITLE).first()
        self.assertIsNotNone(note, 'the first message should be acknowledged')
        self.assertIn('within %d hours' % support.SLA_HOURS, note.body)
        self.assertTrue(AppNotificationRecipient.objects.filter(
            notification=note, email='asker@example.invalid').exists(),
            'the acknowledgement has to reach the sender’s inbox')
        ticket = SupportTicket.objects.get()
        self.assertEqual('asker@example.invalid', ticket.email)
        self.assertIsNotNone(ticket.acked_at)

    def test_the_second_message_does_not_get_a_second_acknowledgement(self):
        self.app_message()
        self.app_message('Any news?')
        self.assertEqual(1, AppNotification.objects.filter(title=support.ACK_TITLE).count())

    def test_a_guest_gets_no_acknowledgement(self):
        payload = json.dumps({'deviceId': 'dev-guest', 'body': 'no account yet'})
        response = self.client.post('/api/messages/', payload,
                                    content_type='application/json',
                                    **self.headers())
        self.assertEqual(200, response.status_code, response.content[:200])
        self.assertEqual(0, AppNotification.objects.filter(title=support.ACK_TITLE).count())
        self.assertEqual(0, SupportTicket.objects.count())

    def test_a_failure_in_acknowledging_still_stores_the_message(self):
        """The app's message is what matters; the ack must never break it."""
        from unittest import mock
        with mock.patch.object(support, 'ticket_for', side_effect=RuntimeError('boom')):
            response = self.app_message('does this survive?')
        self.assertEqual(200, response.status_code, response.content[:200])
        self.assertTrue(UserMessage.objects.filter(body='does this survive?').exists())


class CannedReplyTest(SupportDeskBase):
    def test_a_canned_reply_can_be_saved_and_deleted_from_the_console(self):
        client = self.staff_client()
        response = client.post('/admin-dashboard/user-messages/', {
            'canned_action': 'add', 'canned_title': 'How to change plan',
            'canned_body': 'Open the app, tap Profile, then Change plan.'})
        self.assertEqual(302, response.status_code)
        self.assertEqual(1, CannedReply.objects.count())

        row = CannedReply.objects.get()
        response = client.post('/admin-dashboard/user-messages/', {
            'canned_action': 'delete', 'canned_id': row.id})
        self.assertEqual(302, response.status_code)
        self.assertEqual(0, CannedReply.objects.count())

    def test_an_empty_canned_reply_is_refused(self):
        client = self.staff_client()
        client.post('/admin-dashboard/user-messages/', {
            'canned_action': 'add', 'canned_title': '', 'canned_body': ''})
        self.assertEqual(0, CannedReply.objects.count())

    def test_using_a_canned_answer_counts_it(self):
        row = CannedReply.objects.create(title='Plan', body='Tap Profile → Change plan.')
        self.app_message('How do I change my plan?')
        message = UserMessage.objects.get()
        support.bump_canned_use('Tap Profile → Change plan.')
        row.refresh_from_db()
        self.assertEqual(1, row.uses)

    def test_the_console_lists_the_canned_replies(self):
        CannedReply.objects.create(title='Playback issue', body='Try re-installing the app.')
        client = self.staff_client()
        response = client.get('/admin-dashboard/user-messages/')
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'Canned replies')
        self.assertContains(response, 'Playback issue')


class CsatTest(SupportDeskBase):
    def _answer(self, client, text='We extended your plan by 30 days.'):
        self.app_message()
        message = UserMessage.objects.get()
        return client.post('/admin-dashboard/user-messages/', {
            'message_id': message.id, 'reply': text}), message

    def test_the_answer_asks_the_question_once_and_carries_a_link(self):
        client = self.staff_client()
        response, message = self._answer(client)
        self.assertEqual(302, response.status_code)
        message.refresh_from_db()
        self.assertIn(support.RATING_QUESTION, message.reply_body)
        ticket = SupportTicket.objects.get()
        self.assertIn('/support/rate/%s/1/' % ticket.token, message.reply_body)
        self.assertIn('/support/rate/%s/0/' % ticket.token, message.reply_body)
        self.assertIsNotNone(ticket.csat_asked_at)

        # A second answer on the same thread does not ask again: the question
        # goes out once, with the first human reply.
        asked_at = ticket.csat_asked_at
        client.post('/admin-dashboard/user-messages/', {
            'message_id': message.id, 'reply': 'Anything else?'})
        message.refresh_from_db()
        ticket.refresh_from_db()
        self.assertNotIn(support.RATING_QUESTION, message.reply_body)
        self.assertEqual(asked_at, ticket.csat_asked_at)

    def test_the_link_records_the_answer(self):
        client = self.staff_client()
        self._answer(client)
        ticket = SupportTicket.objects.get()
        response = self.client.get('/support/rate/%s/1/' % ticket.token,
                                   HTTP_HOST='newmovies.linkpc.net',
                                   HTTP_X_FORWARDED_PROTO='https')
        self.assertEqual(200, response.status_code)
        ticket.refresh_from_db()
        self.assertEqual(SupportTicket.RATING_HELPFUL, ticket.rating)
        self.assertIsNotNone(ticket.rated_at)
        self.assertContains(response, 'Glad that helped')

    def test_a_second_click_does_not_rewrite_the_answer(self):
        client = self.staff_client()
        self._answer(client)
        ticket = SupportTicket.objects.get()
        self.client.get('/support/rate/%s/1/' % ticket.token,
                        HTTP_HOST='newmovies.linkpc.net', HTTP_X_FORWARDED_PROTO='https')
        self.client.get('/support/rate/%s/0/' % ticket.token,
                        HTTP_HOST='newmovies.linkpc.net', HTTP_X_FORWARDED_PROTO='https')
        ticket.refresh_from_db()
        self.assertEqual(SupportTicket.RATING_HELPFUL, ticket.rating)

    def test_an_unknown_or_nonsense_link_is_a_404(self):
        for path in ('/support/rate/deadbeef/1/', '/support/rate/deadbeef/7/'):
            response = self.client.get(path, HTTP_HOST='newmovies.linkpc.net',
                                       HTTP_X_FORWARDED_PROTO='https')
            self.assertEqual(404, response.status_code, path)

    def test_the_console_shows_the_rating_on_the_thread(self):
        client = self.staff_client()
        self._answer(client)
        ticket = SupportTicket.objects.get()
        self.client.get('/support/rate/%s/1/' % ticket.token,
                        HTTP_HOST='newmovies.linkpc.net', HTTP_X_FORWARDED_PROTO='https')
        response = client.get('/admin-dashboard/user-messages/')
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'helpful')
        self.assertContains(response, 'Found it helpful')


class SlaCounterTest(SupportDeskBase):
    def _stats(self):
        return support.stats()

    def test_waiting_and_overdue_are_counted_against_the_target(self):
        self.app_message('old question')
        old = UserMessage.objects.get()
        UserMessage.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(hours=support.SLA_HOURS + 3))
        self.app_message('fresh question')

        desk = self._stats()
        self.assertEqual(2, desk['waiting'])
        self.assertEqual(1, desk['overdue'])
        self.assertGreater(desk['oldest_wait_hours'], support.SLA_HOURS)
        self.assertEqual(support.SLA_HOURS, desk['sla_hours'])

    def test_an_answered_message_is_not_waiting(self):
        client = self.staff_client()
        self.app_message()
        message = UserMessage.objects.get()
        client.post('/admin-dashboard/user-messages/',
                    {'message_id': message.id, 'reply': 'Answered.'})
        desk = self._stats()
        self.assertEqual(0, desk['waiting'])
        self.assertEqual(0, desk['overdue'])
        self.assertGreaterEqual(desk['median_first_reply_hours'], 0)

    def test_the_sidebar_badge_reports_waiting_messages(self):
        from django.core.cache import cache
        self.app_message('waiting please')
        cache.delete('admin_badge_waiting_messages')
        cache.delete('admin_badge_overdue_messages')
        client = self.staff_client()
        response = client.get('/admin-dashboard/user-messages/')
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.context['admin_badges']['waiting_messages'])
