"""云端部署专用设置；本机开发仍使用 settings.py。"""
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from .deployment_settings import parse_public_address
from .settings import *  # noqa: F403

SECRET_KEY = os.environ.get('FITSTUDIO_SECRET_KEY', '')
if len(SECRET_KEY) < 50 or SECRET_KEY.startswith('django-insecure-'):
    raise ImproperlyConfigured('请提供长度至少为 50 的独立随机密钥')

DOMAIN, HTTPS_ORIGIN = parse_public_address(
    os.environ.get('FITSTUDIO_DOMAIN', ''),
    os.environ.get('FITSTUDIO_HTTPS_PORT', '443'),
)
DEBUG = False
ALLOWED_HOSTS = [DOMAIN]
CSRF_TRUSTED_ORIGINS = [HTTPS_ORIGIN]
DATABASES['default']['NAME'] = Path(os.environ.get('FITSTUDIO_DB_PATH', '/data/fitstudio.sqlite3'))  # noqa: F405
STATIC_ROOT = BASE_DIR / 'staticfiles'  # noqa: F405
MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')  # noqa: F405
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
DEFAULT_EXCEPTION_REPORTER_FILTER = 'config.reporting.PrivateRequestFilter'
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {'private_request': {'()': 'config.reporting.RequestLogFilter'}},
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'filters': ['private_request']},
    },
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}
