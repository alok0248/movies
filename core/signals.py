from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.contrib.auth.models import User

from .models import AmazonAffiliateProduct


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """Create related objects when a new user is created."""
    pass


def _sync_amazon_links(sender, **kwargs):
    """Keep every app's Amazon block and the cached configs in step.

    A link added, switched off or deleted on the Amazon links page is written
    into the app payloads straight away and the cached configs are dropped, so
    the app and the admin page never disagree about the list.
    """
    try:
        from . import amazon_links

        amazon_links.sync_app_payloads()
    except Exception:
        pass


post_save.connect(_sync_amazon_links, sender=AmazonAffiliateProduct,
                  dispatch_uid='core_amazon_link_saved')
post_delete.connect(_sync_amazon_links, sender=AmazonAffiliateProduct,
                    dispatch_uid='core_amazon_link_deleted')


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


# Addresses are converted to a location as they are written, never while an admin
# page is rendering: every core model that carries an address gets one post_save
# receiver, and core/geoip.ensure() decides what still needs an ipwho.is call. A
# new model with an address field is picked up by adding its field name here.
ADDRESS_FIELDS = ('ip', 'ip_address', 'last_ip_address', 'client_ip')

# model -> the field holding its address, so the receiver is a dict lookup.
_ADDRESS_FIELD = {}


def _address_field(model):
    """The name of the field on this model that holds an address, if any."""
    from django.core.exceptions import FieldDoesNotExist

    for name in ADDRESS_FIELDS:
        try:
            model._meta.get_field(name)
        except FieldDoesNotExist:
            continue
        return name
    return ''


def _note_address(sender, instance, **kwargs):
    """Hand a saved row's address to the shared IP -> location path."""
    field = _ADDRESS_FIELD.get(sender)
    if not field:
        return
    try:
        from . import geoip

        geoip.ensure(getattr(instance, field, '') or '')
    except Exception:
        pass


def connect_address_watchers():
    """Watch every core model that stores an address (called from ready())."""
    from django.apps import apps

    for model in apps.get_app_config('core').get_models():
        if model._meta.object_name == 'IPLocation':
            continue  # that is the cache of answers, not a source of addresses
        field = _address_field(model)
        if not field:
            continue
        _ADDRESS_FIELD[model] = field
        post_save.connect(
            _note_address, sender=model,
            dispatch_uid='core_address_watch_%s' % model._meta.label_lower,
        )
