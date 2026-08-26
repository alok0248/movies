import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def get_db_config():
    """Return database configuration with local + optional external DB."""
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

    # Try to load external DB from server_config.py (VM-local only)
    config = _load_external_db(config)

    # Try to load from DBConnectionConfig model (for admin-configured external DB)
    config = _load_external_from_model(config)

    return config


def _load_external_db(config):
    """Load external DB config from server_config.py if it exists."""
    config_path = BASE_DIR / 'server_config.py'
    if config_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location('server_config', str(config_path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, 'DB_CONFIG') and isinstance(mod.DB_CONFIG, dict):
                db_cfg = mod.DB_CONFIG.copy()
                # Remove extra params that Django doesn't understand
                django_keys = {'ENGINE', 'NAME', 'USER', 'PASSWORD', 'HOST', 'PORT', 'OPTIONS', 'CONN_MAX_AGE', 'CONN_HEALTH_CHECKS', 'AUTOCOMMIT', 'ATOMIC_REQUESTS', 'TIME_ZONE'}
                options = {k: v for k, v in db_cfg.items() if k not in django_keys}
                if options:
                    db_cfg['OPTIONS'] = options
                config['external'] = db_cfg
        except Exception:
            pass
    return config


def _load_external_from_model(config):
    """Load external DB from DBConnectionConfig model."""
    # Avoid circular import — only import when Django is ready
    if 'django' not in __import__('sys').modules:
        return config
    try:
        from core.models import DBConnectionConfig, DBRoutingConfig
        routing = DBRoutingConfig.get_config()
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

        config['external'] = db_cfg
    except Exception:
        pass
    return config
