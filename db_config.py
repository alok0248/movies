import os

def get_db_config():
    """Return database configuration. Override via environment variables."""
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
