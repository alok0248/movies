from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # Import signals to connect them!
        import core.signals
        # Register the admin-configured external DB. The app registry is
        # ready here, so the config models can be queried — settings.py
        # import time is too early (AppRegistryNotReady).
        from db_config import activate_external_db
        activate_external_db()
