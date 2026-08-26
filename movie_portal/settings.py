import os
import json
from pathlib import Path

# Install PyMySQL as MySQLdb for Oracle Cloud MySQL compatibility
try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:
    pass

from db_config import get_db_config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Environment (default to 'dev' if not set)
ENV = os.environ.get('DJANGO_ENV', 'dev')

# Load credentials from JSON file or environment variables
creds_path = BASE_DIR / 'cred' / 'credentials.json'
TMDB_API_KEY = None
CODESPECTERS_API_KEY = os.environ.get('CODESPECTERS_API_KEY', 'YOUR_CODESPECTERS_API_KEY_HERE')
if creds_path.exists():
    with open(creds_path, 'r') as f:
        creds = json.load(f)
        CODESPECTERS_API_KEY = creds.get('CODESPECTERS_API_KEY', CODESPECTERS_API_KEY)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-your-secret-key-here')

# SECURITY WARNING: don't run with debug turned on in production!
# Default to True for dev, False for prod
if ENV == 'prod':
    DEBUG = os.environ.get('DJANGO_DEBUG', 'False').lower() in ('true', '1', 't')
else:
    DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() in ('true', '1', 't')

ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core.apps.CoreConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # 'core.middleware.ContentSecurityPolicyMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'core.middleware.EmailSettingsMiddleware',
    'core.middleware.URLBlockMiddleware',
    'core.middleware.WebsiteVisitorTrackingMiddleware',
    'core.browser_cache.BrowserCacheMiddleware',
]

ROOT_URLCONF = 'movie_portal.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.site_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'movie_portal.wsgi.application'

# Database
# https://docs.djangoproject.com/en/stable/ref/settings/#databases
DATABASES = get_db_config()

# Database Router — routes user data to external DB when enabled
DATABASE_ROUTERS = ['core.db_router.UserDBRouter']

# Password validation
# https://docs.djangoproject.com/en/stable/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/stable/topics/i18n/
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/stable/howto/static-files/
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# File upload size limits (increase for larger APK files)
# Set maximum file size to 500MB (in bytes: 500 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = 524288000
DATA_UPLOAD_MAX_MEMORY_SIZE = 524288000

# Default primary key field type
# https://docs.djangoproject.com/en/stable/ref/settings/#default-auto-field
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Email Configuration
# Console backend: emails print to terminal. Switch to SMTP for production.
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = 'noreply@newmovies.com'
# Uncomment below for SMTP (e.g. Gmail):
# EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
# EMAIL_HOST = 'smtp.gmail.com'
# EMAIL_PORT = 587
# EMAIL_USE_TLS = True
# EMAIL_HOST_USER = 'your@gmail.com'
# EMAIL_HOST_PASSWORD = 'your-app-password'

# API Configuration
TMDB_BASE_URL = 'https://api.themoviedb.org/3'
TMDB_IMAGE_BASE_URL = 'https://image.tmdb.org/t/p/w500'
CODESPECTERS_BASE_URL = 'https://api.codespecters.com/embed'

# Cache Configuration
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
        'TIMEOUT': 3600,  # 1 hour
        'OPTIONS': {
            'MAX_ENTRIES': 1000
        }
    }
}

# Login URL
LOGIN_URL = '/login/'

# Security settings for production (configurable via environment variables)
if ENV == 'prod':
    SECURE_SSL_REDIRECT = False  # Disable for ngrok
    SESSION_COOKIE_SECURE = False  # Disable for ngrok
    CSRF_COOKIE_SECURE = False  # Disable for ngrok
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = os.environ.get('DJANGO_X_FRAME_OPTIONS', 'ALLOWALL')
else:
    X_FRAME_OPTIONS = 'ALLOWALL'

# Trust the proxy (nginx)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# ---------------------------------------------------------------------------
# Performance Optimizations
# ---------------------------------------------------------------------------

# SQLite timeout for concurrent access
DATABASES['default']['OPTIONS'] = {
    'timeout': 20,
}

# In-memory cache (single server)
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
        'TIMEOUT': 300,
        'OPTIONS': {
            'MAX_ENTRIES': 2000,
        },
    }
}

# Session engine — database-backed, cached
SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'

# Template caching
TEMPLATES[0]['OPTIONS']['loaders'][0] = (
    ('django.template.loaders.cached.Loader', TEMPLATES[0]['OPTIONS']['loaders'][0])
    if isinstance(TEMPLATES[0]['OPTIONS']['loaders'][0], str)
    else TEMPLATES[0]['OPTIONS']['loaders'][0]
)

# Static files caching
STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.StaticFilesStorage'

# Reduce query logging in production
if ENV == 'prod':
    LOGGING = {
        'version': 1,
        'disable_existing_loggers': False,
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
            },
        },
        'loggers': {
            'django': {
                'handlers': ['console'],
                'level': 'WARNING',
            },
        },
    }

# Load local settings LAST (if exists) for environment-specific config
try:
    from movie_portal.settings_local import *
except ImportError:
    pass
