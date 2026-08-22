from django.core.management.base import BaseCommand
from core.models import AndroidApp


class Command(BaseCommand):
    help = 'Clean old analytics data for all Android apps based on their retention settings'

    def handle(self, *args, **options):
        apps = AndroidApp.objects.all()
        total_deleted = {}
        for app in apps:
            deleted = app.clean_old_analytics_data()
            for key, count in deleted.items():
                total_deleted[key] = total_deleted.get(key, 0) + count
            total_records = sum(deleted.values())
            if total_records > 0:
                self.stdout.write(
                    f'  {app.name}: deleted {total_records} old records (retention: {app.data_retention_days} days)'
                )

        grand_total = sum(total_deleted.values())
        if grand_total > 0:
            self.stdout.write(self.style.SUCCESS(
                f'\nTotal: deleted {grand_total} records across {apps.count()} apps'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('No old records to clean.'))
