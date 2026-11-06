"""调用主进程内部桥的客户端（子进程侧）。

主进程在 127.0.0.1:${BRIDGE_PORT} 上提供只读接口，
这里负责带上共享 token 去请求，并在不可达时安静降级
（控制台显示「主进程不可用」而不是报错整页 500）。
"""
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
_PORT = os.getenv("BRIDGE_PORT", "9805")
_BASE = f"http://{_HOST}:{_PORT}"


def _token() -> Optional[str]:
    return os.getenv("BRIDGE_TOKEN")


def _new_client(timeout: float) -> httpx.AsyncClient:
    """构造客户端。

    某些环境的 NO_PROXY 值（如 `[::1]`）会让 httpx 解析崩在 URL 构造阶段，
    因此降级为 trust_env=False 重试一次。
    """
    try:
        return httpx.AsyncClient(timeout=timeout)
    except Exception:
        return httpx.AsyncClient(timeout=timeout, trust_env=False)


async def _get(path: str, timeout: float = 3.0,
               params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    token = _token()
    if not token:
        return None

    headers = {"X-Bridge-Token": token}
    try:
        async with _new_client(timeout) as client:
            resp = await client.get(f"{_BASE}{path}", headers=headers, params=params)
            if resp.status_code != 200:
                logger.debug("桥接口 %s 返回 %s", path, resp.status_code)
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("调用桥接口失败 %s: %s", path, exc)
        return None


async def get_status(timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """Telegram 连接状态；主进程不可达时返回 None。"""
    return await _get("/internal/status", timeout=timeout)


async def get_dialogs(limit: int = 200, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """用户账号可见的会话列表。"""
    return await _get("/internal/dialogs", timeout=timeout, params={"limit": limit})


def is_available() -> bool:
    """是否具备调用条件（有 token 即认为主进程已启动过桥）。"""
    return bool(_token())
