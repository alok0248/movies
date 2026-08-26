"""
Database Router: Routes user-related models to external DB when enabled.
Website config stays on local SQLite always.
"""


# Models that should go to the external database (when enabled)
USER_MODELS = {
    'synceduser',
    'usersession',
    'playhistory',
    'userclouddata',
    'emailverification',
    'emailsendlog',
    'useractivity',
    'watchlist',
    'websitevisitor',
    'websitevisitorvisit',
    'subscriber',
}


def _use_external():
    """Check if external DB routing is enabled."""
    try:
        from .models import DBRoutingConfig
        cfg = DBRoutingConfig.get_config()
        return cfg.use_external_db and cfg.external_db_ready
    except Exception:
        return False


def _get_read_db():
    """Return 'external' or 'default' for reads."""
    return 'external' if _use_external() else 'default'


def _get_write_db():
    """Return 'external' or 'default' for writes."""
    cfg = None
    try:
        from .models import DBRoutingConfig
        cfg = DBRoutingConfig.get_config()
    except Exception:
        pass
    if cfg and cfg.use_external_db and cfg.external_db_ready:
        return 'external'
    return 'default'


def _is_user_model(model):
    return model._meta.model_name.lower() in USER_MODELS


class UserDBRouter:
    """Route user data to external DB, everything else stays on default."""

    def db_for_read(self, model, **hints):
        if _is_user_model(model):
            return _get_read_db()
        return 'default'

    def db_for_write(self, model, **hints):
        if _is_user_model(model):
            return _get_write_db()
        return 'default'

    def allow_relation(self, obj1, obj2, **hints):
        # Allow relations if both are user models or both are config models
        db_set = {'default', 'external'}
        if _is_user_model(obj1) and _is_user_model(obj2):
            return True
        if not _is_user_model(obj1) and not _is_user_model(obj2):
            return True
        # Allow cross-db relations between auth.User and user models
        if obj1._meta.model_name == 'user' or obj2._meta.model_name == 'user':
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Allow all migrations to run on both databases.
        The router only controls read/write queries, not migrations.
        This ensures all tables exist on both DBs so FK constraints work.
        """
        if db == 'external':
            return True
        elif db == 'default':
            return True
        return True
