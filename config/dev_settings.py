"""仅供本隔离副本本地验证；绝不读取生产数据库或生产环境文件。"""
import ipaddress
import os
from copy import deepcopy

from .settings import *  # noqa: F403

_directory = BASE_DIR / 'work'
_directory.mkdir(exist_ok=True)
DATABASES = deepcopy(DATABASES)
DATABASES['default']['NAME'] = _directory / 'portal-dev.sqlite3'
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '[::1]']
# iPhone 实机验证时只额外允许明确指定的局域网 IP，不接受通配符。
_phone_host = os.environ.get('FITSTUDIO_DEV_HOST', '').strip()
if _phone_host:
    _address = ipaddress.ip_address(_phone_host)
    if not _address.is_private or _address.is_unspecified or _address.is_multicast:
        raise ValueError('FITSTUDIO_DEV_HOST 必须为可信局域网中的具体 IP。')
    ALLOWED_HOSTS.append(_phone_host)
