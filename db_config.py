import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def get_db_config():
    """Return database configuration with local + optional external DB.

    External DB is registered at app-ready time (see activate_external_db):
    querying the DBConnectionConfig model during settings import raises
    AppRegistryNotReady, so it cannot be done here.
    """
    db_engine = os.environ.get('DB_ENGINE', 'django.db.backends.sqlite3')
    db_name = os.environ.get('DB_NAME', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db.sqlite3'))

    config = {
        'default': {
            'ENGINE': db_engine,
            'NAME': db_name,
        }
    }

    # Only add these for non-SQLite databases
    if db_engine != 'django.db.backends.sqlite3':
        config['default']['USER'] = os.environ.get('DB_USER', '')
        config['default']['PASSWORD'] = os.environ.get('DB_PASSWORD', '')
        config['default']['HOST'] = os.environ.get('DB_HOST', 'localhost')
        config['default']['PORT'] = os.environ.get('DB_PORT', '5432')

    return config


def activate_external_db():
    """Register the admin-configured external DB into settings.DATABASES.

    Must be called after the app registry is ready (from AppConfig.ready),
    because it queries the DBRoutingConfig / DBConnectionConfig models —
    doing so during settings.py import raises AppRegistryNotReady, which
    is why the old _load_external_from_model() path silently never worked.
    """
    from django.conf import settings
    from core.models import DBConnectionConfig, DBRoutingConfig

    try:
        routing = DBRoutingConfig.get_config()
        if not routing.use_external_db:
            return
        conn = DBConnectionConfig.objects.filter(is_active=True, is_default=True).first()
        if not conn:
            return
    except Exception:
        return

    engine_map = {
        'mysql': 'django.db.backends.mysql',
        'oracle': 'django.db.backends.oracle',
        'postgresql': 'django.db.backends.postgresql',
        'mssql': 'django.db.backends.mssql',
        'sqlite': 'django.db.backends.sqlite3',
    }
    engine = engine_map.get(conn.db_type, 'django.db.backends.mysql')

    db_cfg = {
        'ENGINE': engine,
        'NAME': conn.database_name,
        'USER': conn.username,
        'PASSWORD': conn.password,
        'HOST': conn.host,
        'PORT': str(conn.port),
        # Django fills these defaults for every alias present at startup.
        # This alias is registered later (AppConfig.ready), so Django's
        # configure_settings() never saw it — provide the same defaults
        # here or request handlers (ATOMIC_REQUESTS etc.) break.
        'ATOMIC_REQUESTS': False,
        'AUTOCOMMIT': True,
        'CONN_MAX_AGE': 0,
        'CONN_HEALTH_CHECKS': False,
        'TIME_ZONE': None,
        'TEST': {},
    }
    if conn.extra_params:
        db_cfg['OPTIONS'] = conn.extra_params
    elif conn.db_type == 'mysql':
        db_cfg['OPTIONS'] = {
            'charset': 'utf8mb4',
            'connect_timeout': 10,
            'read_timeout': 30,
            'write_timeout': 30,
        }

    settings.DATABASES['external'] = db_cfg
