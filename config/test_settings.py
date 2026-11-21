"""真实文件 SQLite 测试配置：每次生成全新测试库，绝不复用项目数据库。"""
from copy import deepcopy
from uuid import uuid4

from .settings import *  # noqa: F403

DATABASES = deepcopy(DATABASES)
_test_directory = BASE_DIR / "work"
_test_directory.mkdir(exist_ok=True)
DATABASES["default"]["NAME"] = _test_directory / "unused-test-base.sqlite3"
DATABASES["default"]["TEST"] = {
    "NAME": _test_directory / f"tests-{uuid4().hex}.sqlite3"
}

FITSTUDIO_QUICK_COURSE_TYPE_ID = 1
