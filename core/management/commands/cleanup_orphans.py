"""Delete orphan rows whose cross-DB FK no longer points to an existing User.

Because PlayHistory, WatchList, EmailSendLog, UserSession, and other user-data
models live on the external MySQL database with db_constraint=False, Django's
CASCADE cannot reach them when a User is deleted on the default SQLite database.
Over time these orphan rows accumulate and can crash admin views that dereference
h.user, log.sent_by, etc.

Run periodically (cron / Task Scheduler / celery beat):

    python manage.py cleanup_orphans          # dry-run (default)
    python manage.py cleanup_orphans --delete  # actually delete

Or schedule it nightly via the server_admin.bat admin menu.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Find and optionally delete rows that reference a User ID '
        'which no longer exists in the auth_user table.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete', action='store_true', default=False,
            help='Actually delete orphan rows (default is dry-run)',
        )
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Max orphan rows to delete per model (0 = unlimited)',
        )

    def handle(self, *args, **options):
        dry_run = not options['delete']
        limit = options['limit']

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — no rows will be deleted. '
                'Use --delete to actually remove orphans.\n'
            ))

        # Collect all valid user IDs once
        valid_ids = set(User.objects.values_list('id', flat=True))
        self.stdout.write(f'Valid User IDs in auth_user: {len(valid_ids)}\n')

        # Each entry: (Model, fk_field_name, extra_label)
        # The user-data models that live on the external DB and are declared
        # db_constraint=False — those are the ones CASCADE cannot reach, so
        # they are the ones that actually accumulate orphans.
        models_to_check = [
            ('playhistory', 'user', 'PlayHistory'),
            ('watchlist', 'user', 'WatchList'),
            ('usersession', 'user', 'UserSession'),
            ('useractivity', 'user', 'UserActivity'),
            ('emailverification', 'user', 'EmailVerification'),
            ('passwordresetotp', 'user', 'PasswordResetOTP'),
            ('accountdeletionotp', 'user', 'AccountDeletionOTP'),
            ('userclouddata', 'user', 'UserCloudData'),
            ('userprofile', 'user', 'UserProfile'),
            ('userheartbeat', 'user', 'UserHeartbeat'),
            ('emailsendlog', 'sent_by', 'EmailSendLog (sent_by)'),
            ('emailmessage', 'sent_by', 'EmailMessage (sent_by)'),
        ]

        # SyncedUser.user is SET_NULL, so an orphan is a row whose user link
        # was already nulled rather than one pointing at a missing ID. Those
        # are found by email instead, below.
        unlink_models = [
            ('websitevisitor', 'user', 'WebsiteVisitor (unlink, keep traffic row)'),
        ]

        from django.apps import apps

        total_deleted = 0
        for model_name, fk_field, label in models_to_check:
            try:
                Model = apps.get_model('core', model_name)
            except LookupError:
                self.stdout.write(f'  SKIP  {label} — model not found')
                continue

            # Get all distinct user IDs referenced by this model
            ref_ids = set(
                Model.objects.exclude(**{f'{fk_field}_id__isnull': True})
                             .values_list(f'{fk_field}_id', flat=True)
                             .distinct()
            )

            orphan_ids = ref_ids - valid_ids
            if not orphan_ids:
                self.stdout.write(f'  OK    {label} — no orphans')
                continue

            qs = Model.objects.filter(**{f'{fk_field}_id__in': orphan_ids})
            count = qs.count()

            if limit and count > limit:
                qs = qs[:limit]
                count = limit

            self.stdout.write(self.style.WARNING(
                f'  FOUND {label} — {count} orphan row(s) '
                f'(user IDs: {", ".join(str(i) for i in sorted(orphan_ids)[:10])}'
                f'{"..." if len(orphan_ids) > 10 else ""})'
            ))

            if not dry_run:
                deleted, _ = qs.delete()
                total_deleted += deleted
                self.stdout.write(self.style.SUCCESS(
                    f'  DELETED {deleted} {label} rows'
                ))

        # Traffic rows that merely pointed at a deleted account are unlinked
        # rather than deleted — the visit still happened.
        for model_name, fk_field, label in unlink_models:
            try:
                Model = apps.get_model('core', model_name)
            except LookupError:
                self.stdout.write(f'  SKIP  {label} — model not found')
                continue

            ref_ids = set(
                Model.objects.exclude(**{f'{fk_field}_id__isnull': True})
                             .values_list(f'{fk_field}_id', flat=True)
                             .distinct()
            )
            orphan_ids = ref_ids - valid_ids
            if not orphan_ids:
                self.stdout.write(f'  OK    {label} — no orphans')
                continue

            count = Model.objects.filter(**{f'{fk_field}_id__in': orphan_ids}).count()
            self.stdout.write(self.style.WARNING(
                f'  FOUND {label} — {count} row(s) to unlink'
            ))
            if not dry_run:
                updated = Model.objects.filter(**{f'{fk_field}_id__in': orphan_ids}).update(
                    **{fk_field: None})
                total_deleted += updated
                self.stdout.write(self.style.SUCCESS(
                    f'  UNLINKED {updated} {label} rows'
                ))

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'Dry run complete. Re-run with --delete to remove orphans.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Total deleted: {total_deleted} orphan rows'
            ))
