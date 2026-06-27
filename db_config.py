import csv
import os
import sys

def get_db_config():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    csv_file = os.path.join(base_dir, 'db_config.csv')
    
    # Default SQLite databases as guaranteed fallbacks
    databases = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.path.join(base_dir, 'db.sqlite3'),
            'OPTIONS': {
                'timeout': 30,  # 30 seconds timeout to prevent locks
            }
        },
        'postgres_db': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.path.join(base_dir, 'movies_fallback.sqlite3'),
            'OPTIONS': {
                'timeout': 30,
            }
        },
        'oracle_db': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.path.join(base_dir, 'series_fallback.sqlite3'),
            'OPTIONS': {
                'timeout': 30,
            }
        },
        'mssql_db': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.path.join(base_dir, 'channels_fallback.sqlite3'),
            'OPTIONS': {
                'timeout': 30,
            }
        }
    }
    
    # Check for a 'force_sqlite' flag (useful for testing)
    if os.environ.get('FORCE_SQLITE') == '1':
        return databases

    if not os.path.exists(csv_file):
        return databases

    try:
        with open(csv_file, mode='r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get('name')
                engine = row.get('engine')
                db_name = row.get('db_name')
                
                if not name or not engine or not db_name:
                    continue

                # Check if the engine is likely to be available
                # This prevents Django from crashing if the driver isn't installed
                can_use_engine = True
                if 'postgresql' in engine and 'psycopg2' not in sys.modules:
                    try: import psycopg2
                    except ImportError: can_use_engine = False
                elif 'oracle' in engine and 'oracledb' not in sys.modules:
                    try: import oracledb
                    except ImportError: can_use_engine = False
                elif 'mssql' in engine and 'pyodbc' not in sys.modules:
                    try: import pyodbc
                    except ImportError: can_use_engine = False

                if not can_use_engine:
                    print(f"Warning: Engine {engine} for {name} not available. Using SQLite fallback.")
                    continue

                db_entry = {
                    'ENGINE': engine,
                    'NAME': db_name,
                }
                
                if row.get('user'): db_entry['USER'] = row['user']
                if row.get('password'): db_entry['PASSWORD'] = row['password']
                if row.get('host'): db_entry['HOST'] = row['host']
                if row.get('port'): db_entry['PORT'] = row['port']
                
                if 'mssql' in engine.lower():
                    db_entry['OPTIONS'] = {'driver': 'ODBC Driver 17 for SQL Server'}

                databases[name] = db_entry
    except Exception as e:
        print(f"Error reading db_config.csv: {e}")
            
    return databases
