import os
import json
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from project root (before anything else reads env vars)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

# Install PyMySQL as MySQLdb for Oracle Cloud MySQL compatibility
try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:
    pass

from db_config import get_db_config

# Environment (default to 'dev' if not set)
ENV = os.environ.get('DJANGO_ENV', 'dev')

# Load credentials from JSON file or environment variables.
# The shared cred\ folder lives next to the project (../cred/website/); the
# in-project copy is kept as a fallback for a standalone checkout.
CRED_DIR = BASE_DIR.parent / 'cred'
creds_path = CRED_DIR / 'website' / 'credentials.json'
if not creds_path.exists():
    creds_path = BASE_DIR / 'cred' / 'credentials.json'
TMDB_API_KEY = os.environ.get('TMDB_API_KEY', None)
CODESPECTERS_API_KEY = os.environ.get('CODESPECTERS_API_KEY', 'YOUR_CODESPECTERS_API_KEY_HERE')
if creds_path.exists():
    with open(creds_path, 'r') as f:
        creds = json.load(f)
        TMDB_API_KEY = creds.get('TMDB_API_KEY', TMDB_API_KEY)
        CODESPECTERS_API_KEY = creds.get('CODESPECTERS_API_KEY', CODESPECTERS_API_KEY)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-your-secret-key-here')

# SECURITY WARNING: don't run with debug turned on in production!
# Default to False everywhere; enable explicitly via DJANGO_DEBUG in development.
DEBUG = os.environ.get('DJANGO_DEBUG', 'False').lower() in ('true', '1', 't')

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
    'core.middleware.ContentSecurityPolicyMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # Counts failed Django-admin sign-ins (needs request.user, so it sits
    # after AuthenticationMiddleware).
    'core.middleware.AdminLoginThrottleMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'core.middleware.EmailSettingsMiddleware',
    'core.middleware.URLBlockMiddleware',
    'core.middleware.WebsiteVisitorTrackingMiddleware',
    'core.browser_cache.BrowserCacheMiddleware',
    'core.error_monitor.ErrorMonitoringMiddleware',
    'core.middleware.RequestLogMiddleware',
    # Last on purpose: it runs *inside* RequestLogMiddleware, so a request from
    # a blocked IP is denied (403) but still recorded in the request log.
    'core.middleware.IPBlockMiddleware',
]

ROOT_URLCONF = 'movie_portal.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
            'core.context_processors.data_health_alert',
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.site_settings',
                'core.context_processors.admin_badges',
                'core.context_processors.settings_wrapper',
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
TIME_ZONE = 'Asia/Kolkata'  # IST — every displayed time is India Standard Time
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

# Google Sign-In — the Web OAuth client ID an ID token's `aud` claim must
# equal for POST /api/user/google-login to accept it. Override via env var to
# rotate the client without a code change.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get(
    'GOOGLE_OAUTH_CLIENT_ID',
    '621729506326-1nqvn8vsk5abvbm0hvd9mm4ijce3dva1.apps.googleusercontent.com',
)
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

# Security settings now live *after* the settings_local import at the bottom of
# this file — see "Security settings" there. They used to sit here, where ENV
# was still the environment default because settings_local (the file that sets
# ENV='prod' on the server) had not been loaded yet, so production silently ran
# with development values.

# Trust the proxy (nginx)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# ---------------------------------------------------------------------------
# Performance Optimizations
# ---------------------------------------------------------------------------

# SQLite tuning: busy timeout. WAL journal mode + synchronous=NORMAL are applied
# per-connection in core/signals.py (connection_created), guarded so non-SQLite
# engines such as MySQL/Oracle are unaffected.
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
# (skip if loaders not configured as expected)

# Static files caching
# Static files caching — ManifestStaticFilesStorage adds content-hash for cache busting
STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'

# Health watch: the TLS certificates nginx serves for this site, checked for
# expiry by `manage.py check_health` against core.health.CERT_WARN_DAYS. The
# default is the chain the deploy hook installs and nginx actually serves
# (line 73 of the site config; deploy/ssl/ssl_deploy_cert.sh), so the alarm is
# on for every deployment and cannot be forgotten in settings_local.py. A
# machine that has no such file — a laptop, a container — reports the check as
# unknown ("no readable certificate at ..."), which is not a failure and does
# not change the exit status. Override the list, or set it to [], in
# settings_local.py to watch different files or switch the check off.
HEALTH_CERT_PATHS = ['/etc/ssl/custom/newmovies.linkpc.net_fullchain.cer']

# Load local settings LAST (if exists) for environment-specific config
try:
    from movie_portal.settings_local import *
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Security settings
# ---------------------------------------------------------------------------
# Evaluated after settings_local, which is what sets ENV='prod' on the server.

def _env_flag(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ('true', '1', 't', 'yes')


# Only this site may frame its own pages (was ALLOWALL). The CSP added by
# core.middleware.ContentSecurityPolicyMiddleware enforces the same rule where
# X-Frame-Options is ignored, and every other directive stays wide open so the
# players, ads and third-party iframes keep working.
X_FRAME_OPTIONS = os.environ.get('DJANGO_X_FRAME_OPTIONS', 'SAMEORIGIN')
SECURE_CONTENT_TYPE_NOSNIFF = True

if ENV == 'prod':
    # nginx forwards X-Forwarded-Proto (proxy_params), and SECURE_PROXY_SSL_HEADER
    # above makes request.is_secure() follow it — so this cannot redirect-loop.
    SECURE_SSL_REDIRECT = _env_flag('DJANGO_SECURE_SSL_REDIRECT', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    CSRF_COOKIE_SAMESITE = 'Lax'
    # HSTS: 180 days, no preload and no includeSubDomains (the host is a
    # dynamic-DNS name, and preloading is irreversible for that long).
    SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_SECURE_HSTS_SECONDS', '15552000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False

# Configure logging after settings_local sets ENV
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'mail_admins': {
            'level': 'ERROR',
            'class': 'django.utils.log.AdminEmailHandler',
            'include_html': False,
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'WARNING',
        },
        'django.request': {
            'handlers': ['console', 'mail_admins'],
            'level': 'ERROR',
            'propagate': True,
        },
        'error_monitor': {
            'handlers': ['console'],
            'level': 'INFO',
        },
    },
}
