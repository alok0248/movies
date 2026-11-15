"""Create the one shared test account, if it is missing.

    python manage.py ensure_test_user             # create if missing, then report
    python manage.py ensure_test_user --check      # report only; exit 1 when absent
    python manage.py ensure_test_user --approve    # also switch the account on

Safe to run as often as you like: an account that already exists is reported and
left exactly as it is — never duplicated, and its password never rewritten. See
``core/test_user.py`` for the credentials file and the approval rules.
"""
import sys

from django.core.management.base import BaseCommand, CommandError

from core.test_user import (TestUserConfigError, credentials_path,
                            ensure_test_user, load_credentials, validate_email)


class Command(BaseCommand):
    help = ('Create the single shared test account if it is missing. Never creates '
            'a second one and never changes an existing password.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--check', action='store_true',
            help='Only report whether the account exists. Creates nothing; exits 1 if it is missing.')
        parser.add_argument(
            '--approve', dest='approve', action='store_true', default=None,
            help='Also switch the account on (active, email confirmed).')
        parser.add_argument(
            '--no-approve', dest='approve', action='store_false', default=None,
            help='Leave the account pending even in a development checkout.')
        parser.add_argument('--email', default=None,
                            help='Use this address instead of the one in the credentials file.')
        parser.add_argument('--password', default=None,
                            help='Use this password instead of the one in the credentials file.')

    def handle(self, *args, **options):
        email = self._address(options)

        if options['check']:
            return self._check(email)

        from django.contrib.auth.models import User

        # A password is only needed to create the account. When it already
        # exists the file is not read at all, so `--check` and a repeat run
        # work on a machine that has no credentials file.
        password = options['password']
        if password is None and not User.objects.filter(email__iexact=email).exists():
            try:
                _file_email, password = load_credentials()
            except TestUserConfigError as exc:
                raise CommandError(str(exc))

        try:
            status = ensure_test_user(
                approve=options['approve'],
                email=email,
                password=password or '',
            )
        except TestUserConfigError as exc:
            raise CommandError(str(exc))

        if status.created:
            self.stdout.write(self.style.SUCCESS(
                'Created the test account %s (username %s).' % (status.email, status.username)))
        else:
            self.stdout.write('Test account %s already existed — nothing changed.' % status.email)

        if status.approved:
            if status.already_approved:
                self.stdout.write('It was already switched on.')
            else:
                self.stdout.write(self.style.SUCCESS(
                    'Switched on: active and email confirmed, so it can sign in.'))
        else:
            self.stdout.write(self.style.WARNING(
                'Left pending: an administrator has to approve it before it can sign in.'))
            self.stdout.write('  On the server: Admin dashboard -> Users, or '
                              're-run with --approve.')
            self.stdout.write('  Locally: run with --approve (a DEBUG checkout does '
                              'this by itself).')
        self.stdout.write('  Credentials file: %s' % credentials_path())

    def _address(self, options):
        """The address to use: ``--email``, else the credentials file/environment."""
        email = options['email']
        try:
            if email is None:
                email, _password = load_credentials()
            return validate_email(email)
        except TestUserConfigError as exc:
            raise CommandError(str(exc))

    def _check(self, email):
        """Report only. Exits 1 when the account is missing, so a test script can gate on it."""
        from django.contrib.auth.models import User

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            self.stdout.write(self.style.ERROR(
                'MISSING: no account for %s. Run: python manage.py ensure_test_user' % email))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS(
            'FOUND: %s (username %s, %s).'
            % (email, user.username, 'active' if user.is_active else 'pending approval')))