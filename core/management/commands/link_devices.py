"""Link install records to the account device ids that own them.

The app sends one id to sync/heartbeat and another to the install/visit
endpoint, so ``AndroidAppDevice.account_key`` is what joins them. Visits set it
from now on; this command backfills the rows that predate that, using the id
itself or the visit IP (only when every heartbeat from that IP belongs to one
account).
"""
from django.core.management.base import BaseCommand

from core.models import AndroidAppDevice, AndroidAppDeviceVisit
from core.views import _resolve_account_key


class Command(BaseCommand):
    help = 'Link Android install records to account device ids (by id, else by visit IP).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Save the links (default: report only).')
        parser.add_argument('--app', type=int, dest='app_id',
                            help='Only this Android app id.')

    def handle(self, *args, **options):
        queryset = AndroidAppDevice.objects.filter(account_key='')
        if options.get('app_id'):
            queryset = queryset.filter(android_app_id=options['app_id'])

        total = linked = 0
        for device in queryset.iterator():
            total += 1
            # The newest few addresses that install used, newest first: the last
            # one may predate the account's current address by weeks.
            addresses = list(AndroidAppDeviceVisit.objects.filter(device=device)
                             .order_by('-visited_at')
                             .values_list('ip_address', flat=True).distinct()[:5])
            key, used_ip = '', None
            for address in addresses:
                key = _resolve_account_key(device.user_id, address)
                if key and key != device.user_id:
                    used_ip = address
                    break
            if not key or key == device.user_id:
                continue
            linked += 1
            self.stdout.write('  LINK  %s (app %s) -> %s  via %s' % (
                device.user_id[:18], device.android_app_id, key[:18],
                'ip ' + str(used_ip) if used_ip else 'id'))
            if options['apply']:
                device.account_key = key
                device.save(update_fields=['account_key'])

        verb = 'linked' if options['apply'] else 'would link'
        self.stdout.write(self.style.SUCCESS(
            '%s %d of %d unlinked install record(s).%s' % (
                verb, linked, total,
                '' if options['apply'] else ' Re-run with --apply to save.')))
