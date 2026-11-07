"""启动引导：把数据库里的配置写入 os.environ。

必须在**任何业务模块被 import 之前**调用，原因有三条：

1. `utils/constants.py` 与 `models/models.py` 在模块级就执行 `load_dotenv()`
   并立刻求值二十多个常量；
2. `main.py` 的 `from utils.constants import ...` 与读取 `API_ID` 等凭据都发生在
   import 阶段，函数体还没执行；
3. 一旦常量被求值成字符串，后面再改 `os.environ` 也不会回头更新它们。

所以顺序只能是：

    bootstrap_runtime_env()   # 这里注入
    from utils.constants import ...   # 之后才能 import

本模块只依赖标准库（os / sqlite3 / logging），不走 SQLAlchemy，
否则会触发 models → constants → dotenv 的 import 链，反而在注入之前把常量固化。

关于优先级：数据库里的值**覆盖**同名环境变量（数据库是配置的唯一真相源）；
数据库里没有的键保留环境变量原值，便于迁移期用 env 临时兜底。
`python-dotenv` 的 `load_dotenv()` 默认 `override=False`，不会把已注入的值冲掉。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join(".", "db", "forward.db")
_TABLE = "app_settings"

# 注入成功的键，便于诊断与测试断言
_injected: Dict[str, str] = {}


def db_path() -> str:
    """配置文件所在的 SQLite 路径。固定相对工作目录，与 models.init_db 保持一致。"""
    return os.environ.get("APP_DB_PATH") or DEFAULT_DB_PATH


def read_app_settings(path: Optional[str] = None) -> Dict[str, str]:
    """只读地取出全部配置；库或表不存在时返回空字典。"""
    target = path or db_path()
    if not os.path.exists(target):
        return {}

    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        logger.debug("无法打开配置库 %s: %s", target, exc)
        return {}

    try:
        rows = conn.execute(
            f"SELECT key, value FROM {_TABLE}"
        ).fetchall()
    except sqlite3.Error:
        # 表还没建（首次启动），不是错误
        return {}
    finally:
        conn.close()

    result: Dict[str, str] = {}
    for key, value in rows:
        if not key:
            continue
        if value is None:
            continue
        text = str(value)
        if text == "":
            # 空值视为「未配置」，交给代码里的默认值处理
            continue
        result[str(key)] = text
    return result


def apply_to_environ(settings: Dict[str, str]) -> int:
    """把配置写进 os.environ，返回实际写入的键数量。"""
    written = 0
    for key, value in settings.items():
        os.environ[key] = value
        _injected[key] = value
        written += 1
    return written


def bootstrap_runtime_env(path: Optional[str] = None) -> int:
    """入口：读库 → 注入环境变量。返回注入的键数量。

    在任何业务模块 import 之前调用一次即可；重复调用是幂等的。
    """
    settings = read_app_settings(path)
    if not settings:
        logger.debug("配置库中没有 app_settings 记录，使用环境变量默认值")
        return 0

    written = apply_to_environ(settings)
    logger.debug("已从配置库注入 %d 项配置到环境变量", written)
    return written


def injected_keys() -> Dict[str, str]:
    """本次进程注入过的键（不含值），供自检与日志使用。"""
    return dict(_injected)
