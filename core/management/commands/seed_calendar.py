"""
Management command to seed calendar data for the current month window.

Seeds CalendarMonthCache for current month ± 2 months (5 months total).
Run: python manage.py seed_calendar
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
import datetime
import calendar as cal_mod

from core.models import CalendarMonthCache


class Command(BaseCommand):
    help = 'Seed calendar data for current month ± 2 months into CalendarMonthCache'

    def add_arguments(self, parser):
        parser.add_argument('--months', type=int, default=2, help='Months on each side (default: 2)')

    def handle(self, *args, **options):
        from core.views import get_calendar_month_data

        months = options['months']
        today = datetime.date.today()
        base_month = today.year * 12 + today.month - 1

        seeded = 0
        skipped = 0

        for offset in range(-months, months + 1):
            target = base_month + offset
            year = target // 12
            month = (target % 12) + 1

            exists = CalendarMonthCache.objects.filter(year=year, month=month).exists()
            if exists:
                skipped += 1
                self.stdout.write(f'  [SKIP] {cal_mod.month_name[month]} {year} -- already cached')
                continue

            self.stdout.write(f'  [SYNC] {cal_mod.month_name[month]} {year} -- fetching from TMDB...')
            try:
                data = get_calendar_month_data(year, month)
                movie_count = len(data.get('movies', []))
                series_count = len(data.get('series', []))
                self.stdout.write(self.style.SUCCESS(f'  [ OK ] {cal_mod.month_name[month]} {year} -- {movie_count} movies, {series_count} series'))
                seeded += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  [ERR] {cal_mod.month_name[month]} {year} -- error: {e}'))

        self.stdout.write(self.style.SUCCESS(
            f'\nDone! Seeded: {seeded}, Skipped: {skipped}'
        ))
