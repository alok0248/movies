"""
Management command: drop_cross_db_fks

Drops foreign-key constraints on the 'external' database that reference
tables in a different database (cross-DB FKs). This is necessary when
models like SyncedUser, EmailVerification, etc. are routed to an external
MySQL database while auth.User lives in the default SQLite database.

Run once after deploying to a fresh MySQL instance:
    python manage.py drop_cross_db_fks
"""
from django.core.management.base import BaseCommand
from django.db import connections


# Tables that Django routes to the 'external' database but whose FKs to
# auth_user (or other default-DB tables) can't be enforced across databases.
# Add new cross-DB FKs here as they appear.
CROSS_DB_TABLES = [
    'core_synceduser',
    'core_emailverification',
    'core_userclouddata',
    'core_websitevisitorvisit',
    'core_websitevisitorsession',
    'core_adimpression',
    'core_emailmessage',
]


class Command(BaseCommand):
    help = 'Drop FK constraints on cross-DB tables in the external database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would be dropped without actually dropping anything',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        try:
            conn = connections['external']
        except Exception as e:
            self.stderr.write(self.style.ERROR(
                f'Cannot connect to external database: {e}'
            ))
            return

        # Find all FK constraints on our cross-DB tables
        cursor = conn.cursor()
        placeholders = ', '.join(['%s'] * len(CROSS_DB_TABLES))
        cursor.execute(
            f"SELECT CONSTRAINT_NAME, TABLE_NAME "
            f"FROM information_schema.KEY_COLUMN_USAGE "
            f"WHERE TABLE_SCHEMA = %s "
            f"AND REFERENCED_TABLE_NAME IS NOT NULL "
            f"AND TABLE_NAME IN ({placeholders})",
            [conn.settings_dict['NAME']] + CROSS_DB_TABLES,
        )
        fks = cursor.fetchall()

        if not fks:
            self.stdout.write(self.style.SUCCESS(
                'No cross-DB FK constraints found — nothing to drop.'
            ))
            return

        self.stdout.write(f'Found {len(fks)} FK constraint(s):')
        dropped = 0
        for cname, tname in fks:
            sql = f'ALTER TABLE `{tname}` DROP FOREIGN KEY `{cname}`'
            self.stdout.write(f'  {cname}  ({tname})')
            if not dry_run:
                try:
                    cursor.execute(sql)
                    dropped += 1
                    self.stdout.write(self.style.SUCCESS(f'    -> Dropped'))
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f'    -> FAILED: {e}'))
            else:
                self.stdout.write(f'    -> Would drop (dry run)')

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'Dry run complete. {len(fks)} constraint(s) would be dropped. '
                f'Run without --dry-run to apply.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Dropped {dropped}/{len(fks)} FK constraints.'
            ))
