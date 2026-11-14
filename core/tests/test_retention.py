"""Cohort retention and the funnels: the arithmetic has to be checkable.

The numbers drive product decisions (and the roadmap's S8), so these tests pin
the definitions: an account counts in the week it signed up, D-N means active on
that calendar day, rolling means active on or after it, and a day in the future
is never counted as a return.
"""
from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core import retention
from core.models import ApkDownload, PlayHistory, SubscriptionEvent, SyncedUser


class CohortBase(TestCase):
    """Helpers for building accounts with controlled dates."""

    def make_account(self, email, signed_up=None, subscribed=False):
        user = User.objects.create_user(username=email, email=email, password='x')
        profile = SyncedUser.objects.create(user=user, email=email,
                                            is_subscribed=subscribed)
        if signed_up is not None:
            SyncedUser.objects.filter(pk=profile.pk).update(created_at=signed_up)
        return profile

    def play(self, profile, when, completed=False, watched=0, title='Title'):
        return PlayHistory.objects.create(
            user_id=profile.user_id, tmdb_id=1, media_type='movie', title=title,
            completed=completed, duration_seconds=watched, last_played_at=when)

    def monday(self, weeks_ago=0):
        today = timezone.localdate()
        return today - timedelta(days=today.weekday()) - timedelta(days=7 * weeks_ago)


class CohortRetentionTest(CohortBase):
    def test_d1_counts_the_next_calendar_day_only(self):
        monday = self.monday(2)
        profile = self.make_account('a@example.invalid', signed_up=_noon(monday))
        self.play(profile, _noon(monday + timedelta(days=1)))
        rows, totals = retention.cohorts(weeks=4)
        row = [r for r in rows if r['week_start'] == monday][0]
        self.assertEqual(1, row['size'])
        self.assertEqual(1, row['d1'])
        self.assertEqual(100.0, row['d1_pct'])
        self.assertEqual(0, row['d7'])
        self.assertEqual(1, totals['size'])

    def test_a_later_return_does_not_count_as_d1(self):
        monday = self.monday(2)
        profile = self.make_account('b@example.invalid', signed_up=_noon(monday))
        self.play(profile, _noon(monday + timedelta(days=3)))
        rows, _totals = retention.cohorts(weeks=4)
        row = [r for r in rows if r['week_start'] == monday][0]
        self.assertEqual(0, row['d1'])
        # Rolling starts at day 7, so a day-3 return is neither D1 nor rolling.
        self.assertEqual(0, row['d7'])
        self.assertEqual(0, row['rolling'])

    def test_rolling_counts_a_return_on_or_after_day_seven(self):
        monday = self.monday(3)
        profile = self.make_account('c@example.invalid', signed_up=_noon(monday))
        self.play(profile, _noon(monday + timedelta(days=9)))
        rows, _totals = retention.cohorts(weeks=4)
        row = [r for r in rows if r['week_start'] == monday][0]
        self.assertEqual(0, row['d7'], 'day 9 is not exactly day 7')
        self.assertEqual(1, row['rolling'])

    def test_a_future_day_is_not_counted(self):
        # Signed up today: D1 has not happened yet, so it cannot be a return.
        profile = self.make_account('d@example.invalid', signed_up=_noon(timezone.localdate()))
        self.play(profile, timezone.now())
        rows, _totals = retention.cohorts(weeks=4)
        row = rows[0]
        self.assertTrue(row['partial'])
        self.assertEqual(0, row['d1'])
        self.assertEqual(0, row['rolling'])

    def test_activity_before_signup_is_ignored_for_cohorts(self):
        monday = self.monday(2)
        profile = self.make_account('e@example.invalid', signed_up=_noon(monday))
        self.play(profile, _noon(monday - timedelta(days=5)))
        rows, _totals = retention.cohorts(weeks=4)
        row = [r for r in rows if r['week_start'] == monday][0]
        self.assertEqual(0, row['d1'])

    def test_weeks_is_capped(self):
        rows, _totals = retention.cohorts(weeks=999)
        self.assertEqual(retention.MAX_WEEKS, len(rows))


class FirstPlayTest(CohortBase):
    def test_first_play_is_the_earliest_one_and_completion_is_measured_on_it(self):
        monday = self.monday(2)
        profile = self.make_account('f@example.invalid', signed_up=_noon(monday))
        self.play(profile, _noon(monday), completed=False, title='Opened')
        self.play(profile, _noon(monday + timedelta(days=2)), completed=True, title='Finished')
        stats = retention.first_play_stats()
        self.assertEqual(1, stats['players'])
        self.assertEqual(0, stats['finished_first'],
                         'the *first* play was not finished, even though a later one was')
        self.assertEqual(1, stats['never_finished_first'])

    def test_a_finished_first_play_counts(self):
        profile = self.make_account('g@example.invalid', signed_up=_noon(self.monday(1)))
        self.play(profile, timezone.now(), completed=True, watched=1200, title='Done')
        stats = retention.first_play_stats()
        self.assertEqual(1, stats['finished_first'])
        self.assertEqual(100.0, stats['finished_first_pct'])
        self.assertEqual('Done', stats['top_first'][0][0])

    def test_plays_without_watch_time_are_flagged(self):
        profile = self.make_account('h@example.invalid', signed_up=_noon(self.monday(1)))
        self.play(profile, timezone.now(), watched=0)
        stats = retention.first_play_stats()
        self.assertEqual(1, stats['started_only'])
        self.assertEqual(100.0, stats['started_only_pct'])

    def test_recent_window_counts_only_plays_inside_it(self):
        profile = self.make_account('i@example.invalid', signed_up=_noon(self.monday(4)))
        self.play(profile, timezone.now() - timedelta(days=40), completed=True, title='Old')
        self.play(profile, timezone.now() - timedelta(days=2), completed=True, title='New')
        stats = retention.first_play_stats(days=30)
        self.assertEqual(1, stats['recent_plays'])
        self.assertEqual(1, stats['recent_completed'])


class FunnelTest(CohortBase):
    def test_install_funnel_counts_each_step_from_the_download_record(self):
        ApkDownload.objects.create(app_slug='cinestream', opened_app=False)
        ApkDownload.objects.create(app_slug='cinestream', opened_app=True,
                                   device_user_id='device-1')
        ApkDownload.objects.create(app_slug='cinestream', opened_app=True)
        funnel = retention.install_funnel()
        by_label = {s['label']: s['value'] for s in funnel['steps']}
        self.assertEqual(3, by_label['APK downloads'])
        self.assertEqual(2, by_label['App opened'])
        self.assertEqual(1, by_label['Reached the API'])

    def test_subscription_funnel_walks_from_accounts_to_paying(self):
        played = self.make_account('j@example.invalid', signed_up=_noon(self.monday(2)))
        self.play(played, timezone.now())
        self.make_account('k@example.invalid', signed_up=_noon(self.monday(2)))
        self.make_account('l@example.invalid', signed_up=_noon(self.monday(2)),
                          subscribed=True)
        SubscriptionEvent.objects.create(email='k@example.invalid', action='90',
                                         is_subscribed=True, new_plan='Standard')
        funnel = retention.subscription_funnel()
        by_label = {s['label']: s['value'] for s in funnel['steps']}
        self.assertEqual(3, by_label['Accounts'])
        self.assertEqual(1, by_label['Watched something'])
        self.assertEqual(2, by_label['Ever subscribed'],
                         'one through the event log, one through the profile flag')
        self.assertEqual(1, by_label['Subscribed today'])


class CohortsPageTest(CohortBase):
    def test_staff_can_open_the_page(self):
        User.objects.create_superuser(username='admin', email='admin@example.invalid',
                                      password='pw')
        self.client.force_login(User.objects.get(username='admin'))
        response = self.client.get('/admin-dashboard/analytics/cohorts/',
                                   HTTP_HOST='newmovies.linkpc.net',
                                   HTTP_X_FORWARDED_PROTO='https')
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'Cohorts')
        self.assertContains(response, 'Retention by signup week')
        self.assertEqual('private, no-store', response['Cache-Control'])

    def test_anonymous_visitors_are_sent_to_the_login_page(self):
        response = self.client.get('/admin-dashboard/analytics/cohorts/',
                                   HTTP_HOST='newmovies.linkpc.net',
                                   HTTP_X_FORWARDED_PROTO='https')
        self.assertEqual(302, response.status_code)
        self.assertIn('/login/', response['Location'])
        self.assertIn('cohorts', response['Location'])

    def test_the_weeks_parameter_garbage_does_not_break_the_page(self):
        User.objects.create_superuser(username='admin2', email='a2@example.invalid',
                                      password='pw')
        self.client.force_login(User.objects.get(username='admin2'))
        response = self.client.get('/admin-dashboard/analytics/cohorts/?weeks=abc',
                                   HTTP_HOST='newmovies.linkpc.net',
                                   HTTP_X_FORWARDED_PROTO='https')
        self.assertEqual(200, response.status_code)


class ReportTest(CohortBase):
    def test_build_report_has_every_section_the_template_renders(self):
        report = retention.build_report(weeks=4)
        for key in ('cohorts', 'cohort_totals', 'first_play', 'install',
                    'subscriptions', 'user_db', 'generated_at'):
            self.assertIn(key, report)


def _noon(day):
    """Midday on ``day``, aware, so a play lands on the intended calendar day."""
    return timezone.make_aware(datetime.combine(day, time(hour=12)),
                              timezone.get_current_timezone())
