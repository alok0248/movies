import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def get_db_config():
    """Return database configuration with local + optional external DB.

    External DB is loaded from DBConnectionConfig model (admin-entered via UI).
    No .env files needed — everything is managed from the admin dashboard.
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

    # Load external DB from admin-entered DBConnectionConfig model
    config = _load_external_from_model(config)

    return config


def _load_external_from_model(config):
    """Load external DB from DBConnectionConfig model (admin-entered via UI).

    Priority:
    1. Admin enters DB details on admin-dashboard/settings/db-connections/
    2. Sets one as 'Default' and enables routing on admin-dashboard/settings/db-routing/
    3. Django picks up the config here on next request/restart
    """
    if 'django' not in __import__('sys').modules:
        return config

    try:
        from core.models import DBConnectionConfig, DBRoutingConfig
        try:
            routing = DBRoutingConfig.get_config()
        except Exception:
            return config
        if not routing.use_external_db:
            return config

        conn = DBConnectionConfig.objects.filter(is_active=True, is_default=True).first()
        if not conn:
            return config

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

        config['external'] = db_cfg
    except Exception:
        pass

    return config
