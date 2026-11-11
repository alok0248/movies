"""Bring ``SyncedUser.is_email_verified`` in line with the login account.

Every synced profile currently reads as unverified, while playback gating uses
that flag. The authoritative signal already exists: the Django account's
``is_active``, which the verification flow sets. This rewrites the flag from it
(reporting first), and can be scheduled so the two cannot drift again.
"""
from django.core.management.base import BaseCommand

from core.models import SyncedUser


class Command(BaseCommand):
    help = 'Set is_email_verified from the linked login account (is_active).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Save the changes.')

    def handle(self, *args, **options):
        changed = 0
        for profile in SyncedUser.objects.filter(is_email_verified=False).iterator():
            if not profile.user_id:
                continue
            active = getattr(profile.user, 'is_active', False)
            if not active:
                continue
            changed += 1
            self.stdout.write('  VERIFY %s (login is active)' % profile.email)
            if options['apply']:
                profile.is_email_verified = True
                profile.save(update_fields=['is_email_verified'])

        verb = 'verified' if options['apply'] else 'would verify'
        self.stdout.write(self.style.SUCCESS('%s %d profile(s).%s' % (
            verb, changed, '' if options['apply'] else ' Re-run with --apply to save.')))
