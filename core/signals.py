from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """Create related objects when a new user is created."""
    pass


def _configure_sqlite_connection(sender, connection, **kwargs):
    """Enable WAL journal mode and relaxed synchronous mode on SQLite.

    WAL allows readers to proceed while a write is in flight, which removes
    the "database is locked" stalls that occur when multiple gunicorn
    workers/threads share one SQLite file. Guarded so non-SQLite engines
    (MySQL, Oracle, PostgreSQL) are left untouched.
    """
    if connection.vendor != 'sqlite':
        return
    cursor = connection.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA synchronous=NORMAL;')
    cursor.execute('PRAGMA busy_timeout=20000;')
    cursor.close()


from django.db.backends.signals import connection_created

connection_created.connect(_configure_sqlite_connection, dispatch_uid='core_sqlite_wal')
