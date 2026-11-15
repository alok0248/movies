"""The single shared test account.

What matters here is that it is created once and only once: repeated runs must
not add a second account, must not rewrite the password, and must not switch a
pending account on by accident in an environment that is not a development
checkout.
"""
import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.email_verification import is_email_verified
from core.test_user import (ALLOWED_DOMAIN, TestUserConfigError,
                            ensure_test_user, load_credentials,
                            should_approve_locally, validate_email)

EMAIL = 'cinestream.qa.tester@gmail.com'
PASSWORD = 'WVPukYJTs9Xuw5RAVzGtWqUe'


class EmailDomainTest(TestCase):
    def test_a_gmail_address_is_accepted(self):
        self.assertEqual(EMAIL, validate_email(EMAIL))

    def test_the_address_is_normalised_to_lower_case(self):
        self.assertEqual(EMAIL, validate_email('Cinestream.QA.Tester@Gmail.com'))

    def test_other_domains_are_refused(self):
        for address in ['tester@yahoo.com', 'tester@example.com', 'tester@notgmail.com']:
            with self.subTest(address=address):
                with self.assertRaises(TestUserConfigError) as ctx:
                    validate_email(address)
                self.assertIn(ALLOWED_DOMAIN, str(ctx.exception))

    def test_a_malformed_address_is_refused(self):
        for address in ['', 'no-at-sign', '@gmail.com']:
            with self.subTest(address=address):
                with self.assertRaises(TestUserConfigError):
                    validate_email(address)


class CredentialsFileTest(TestCase):
    def setUp(self):
        # Make sure the environment cannot answer before the file is tried.
        patcher = patch.dict(os.environ, {'TEST_USER_EMAIL': '', 'TEST_USER_PASSWORD': ''})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, tmp, payload):
        path = Path(tmp) / '.test_user.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_the_local_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, {'email': EMAIL, 'password': PASSWORD})
            with override_settings(BASE_DIR=tmp):
                self.assertEqual((EMAIL, PASSWORD), load_credentials())

    def test_a_missing_file_says_what_to_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(BASE_DIR=tmp):
                with self.assertRaises(TestUserConfigError) as ctx:
                    load_credentials()
                self.assertIn('.test_user.json', str(ctx.exception))

    def test_a_broken_file_is_reported_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.test_user.json').write_text('{not json', encoding='utf-8')
            with override_settings(BASE_DIR=tmp):
                with self.assertRaises(TestUserConfigError):
                    load_credentials()

    def test_the_environment_overrides_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, {'email': EMAIL, 'password': PASSWORD})
            with override_settings(BASE_DIR=tmp):
                with patch.dict(os.environ, {'TEST_USER_EMAIL': 'other@gmail.com',
                                             'TEST_USER_PASSWORD': 'from-env'}):
                    self.assertEqual(('other@gmail.com', 'from-env'), load_credentials())


class ApprovalRuleTest(TestCase):
    def test_a_debug_checkout_approves_by_itself(self):
        with override_settings(DEBUG=True):
            self.assertTrue(should_approve_locally())

    def test_a_non_debug_environment_does_not(self):
        # What the server looks like: approval has to be asked for explicitly.
        with override_settings(DEBUG=False):
            self.assertFalse(should_approve_locally())

    def test_the_escape_hatch_is_honoured(self):
        with override_settings(DEBUG=False):
            with patch.dict(os.environ, {'TEST_USER_AUTO_APPROVE': '1'}):
                self.assertTrue(should_approve_locally())


class EnsureTestUserTest(TestCase):
    def test_it_creates_the_account_once(self):
        status = ensure_test_user(approve=True, email=EMAIL, password=PASSWORD)
        self.assertTrue(status.created)
        self.assertFalse(status.existed)
        self.assertEqual(1, User.objects.filter(email__iexact=EMAIL).count())
        user = User.objects.get(email__iexact=EMAIL)
        self.assertTrue(user.check_password(PASSWORD))

    def test_a_second_run_changes_nothing(self):
        first = ensure_test_user(approve=True, email=EMAIL, password=PASSWORD)
        second = ensure_test_user(approve=True, email=EMAIL, password=PASSWORD)
        self.assertFalse(second.created)
        self.assertTrue(second.existed)
        self.assertEqual(1, User.objects.filter(email__iexact=EMAIL).count())
        self.assertEqual(first.username, second.username)

    def test_a_second_run_never_rewrites_the_password(self):
        ensure_test_user(approve=True, email=EMAIL, password=PASSWORD)
        user = User.objects.get(email__iexact=EMAIL)
        user.set_password('a-password-set-by-hand')
        user.save()
        ensure_test_user(approve=True, email=EMAIL, password='a-different-password')
        user.refresh_from_db()
        self.assertTrue(user.check_password('a-password-set-by-hand'))

    def test_the_password_from_the_file_is_not_needed_once_the_account_exists(self):
        ensure_test_user(approve=True, email=EMAIL, password=PASSWORD)
        # No credentials supplied at all: the lookup alone must succeed.
        status = ensure_test_user(approve=False, email=EMAIL, password='')
        self.assertTrue(status.existed)
        self.assertEqual(1, User.objects.filter(email__iexact=EMAIL).count())

    def test_approval_switches_the_account_on(self):
        ensure_test_user(approve=True, email=EMAIL, password=PASSWORD)
        user = User.objects.get(email__iexact=EMAIL)
        self.assertTrue(user.is_active)
        self.assertTrue(is_email_verified(user))

    def test_withholding_approval_leaves_it_pending(self):
        ensure_test_user(approve=False, email=EMAIL, password=PASSWORD)
        user = User.objects.get(email__iexact=EMAIL)
        self.assertFalse(user.is_active)
        self.assertFalse(is_email_verified(user))

    def test_approval_follows_the_environment_when_it_is_not_forced(self):
        with override_settings(DEBUG=True):
            ensure_test_user(email=EMAIL, password=PASSWORD)
            self.assertTrue(User.objects.get(email__iexact=EMAIL).is_active)

    def test_a_production_like_environment_leaves_it_pending(self):
        with override_settings(DEBUG=False):
            ensure_test_user(email=EMAIL, password=PASSWORD)
            self.assertFalse(User.objects.get(email__iexact=EMAIL).is_active)

    def test_a_clashing_username_does_not_stop_the_account_being_created(self):
        User.objects.create_user(username=EMAIL.split('@')[0], email='someone@example.invalid')
        status = ensure_test_user(approve=True, email=EMAIL, password=PASSWORD)
        self.assertTrue(status.created)
        self.assertNotEqual(EMAIL.split('@')[0], status.username)
        self.assertEqual(1, User.objects.filter(email__iexact=EMAIL).count())

    def test_a_foreign_domain_is_refused_before_anything_is_written(self):
        with self.assertRaises(TestUserConfigError):
            ensure_test_user(approve=True, email='tester@yahoo.com', password=PASSWORD)
        self.assertEqual(0, User.objects.filter(email__iexact='tester@yahoo.com').count())

    def test_a_missing_password_is_refused(self):
        with self.assertRaises(TestUserConfigError):
            ensure_test_user(approve=True, email=EMAIL, password='')


class EnsureTestUserCommandTest(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command('ensure_test_user', *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_check_reports_a_missing_account_and_fails(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run('--check', '--email', EMAIL)
        self.assertEqual(1, ctx.exception.code)
        self.assertEqual(0, User.objects.filter(email__iexact=EMAIL).count())

    def test_check_reports_an_existing_account(self):
        ensure_test_user(approve=True, email=EMAIL, password=PASSWORD)
        output = self._run('--check', '--email', EMAIL)
        self.assertIn('FOUND', output)

    def test_the_command_creates_and_approves(self):
        output = self._run('--email', EMAIL, '--password', PASSWORD, '--approve')
        self.assertIn('Created the test account', output)
        self.assertTrue(User.objects.get(email__iexact=EMAIL).is_active)

    def test_running_the_command_twice_does_not_duplicate(self):
        self._run('--email', EMAIL, '--password', PASSWORD, '--approve')
        output = self._run('--email', EMAIL, '--password', PASSWORD, '--approve')
        self.assertIn('already existed', output)
        self.assertEqual(1, User.objects.filter(email__iexact=EMAIL).count())

    def test_without_approval_it_says_who_has_to_approve(self):
        output = self._run('--email', EMAIL, '--password', PASSWORD, '--no-approve')
        self.assertIn('pending', output)
        self.assertIn('administrator', output)
        self.assertFalse(User.objects.get(email__iexact=EMAIL).is_active)

    def test_a_foreign_domain_is_a_clean_command_error(self):
        with self.assertRaises(CommandError):
            self._run('--email', 'tester@yahoo.com', '--password', PASSWORD)