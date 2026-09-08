"""Delete expired email-verification and password-reset OTP rows.

Run periodically (e.g. via cron / Task Scheduler) so old codes don't
accumulate: every resend or new reset request leaves the previous code
behind, and expired rows would otherwise build up forever.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Delete expired EmailVerification and PasswordResetOTP rows'

    def handle(self, *args, **options):
        from core.models import EmailVerification, PasswordResetOTP

        # Both codes are valid for 10 minutes (see EmailVerification.is_expired
        # and PasswordResetOTP.is_expired), so one cutoff covers both.
        cutoff = timezone.now() - timedelta(minutes=10)

        deleted_ev, _ = EmailVerification.objects.filter(created_at__lt=cutoff).delete()
        deleted_otp, _ = PasswordResetOTP.objects.filter(created_at__lt=cutoff).delete()

        total = deleted_ev + deleted_otp
        if total:
            self.stdout.write(self.style.SUCCESS(
                f'Deleted {total} expired OTP records '
                f'({deleted_ev} email verifications, {deleted_otp} password resets)'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('No expired OTP records to delete.'))