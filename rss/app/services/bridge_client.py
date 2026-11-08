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


async def _post(path: str, payload: Dict[str, Any], timeout: float = 20.0) -> Optional[Dict[str, Any]]:
    """POST 到桥。

    与 `_get` 不同，这里在非 200 时也要把响应体带回来——
    配置向导需要把「验证码不正确」这类具体原因显示给用户。
    """
    token = _token()
    if not token:
        return None

    headers = {"X-Bridge-Token": token}
    try:
        async with _new_client(timeout) as client:
            resp = await client.post(f"{_BASE}{path}", headers=headers, json=payload)
            try:
                data = resp.json()
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault("ok", resp.status_code == 200)
            return data
    except Exception as exc:
        logger.debug("调用桥接口失败 %s: %s", path, exc)
        return None


async def get_status(timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """Telegram 连接状态；主进程不可达时返回 None。"""
    return await _get("/internal/status", timeout=timeout)


async def get_dialogs(limit: int = 200, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """用户账号可见的会话列表。"""
    return await _get("/internal/dialogs", timeout=timeout, params={"limit": limit})


async def get_setup_status() -> Optional[Dict[str, Any]]:
    """向导轮询：登录阶段 + 凭据是否齐全。"""
    return await _get("/internal/setup/status", timeout=5.0)


async def send_code(api_id: str, api_hash: str, phone: str) -> Optional[Dict[str, Any]]:
    """保存凭据并发送 Telegram 登录验证码。"""
    return await _post("/internal/setup/send-code", {
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": phone,
    }, timeout=40.0)


async def verify_code(code: str) -> Optional[Dict[str, Any]]:
    return await _post("/internal/setup/verify-code", {"code": code}, timeout=40.0)


async def verify_password(password: str) -> Optional[Dict[str, Any]]:
    return await _post("/internal/setup/verify-password", {"password": password}, timeout=40.0)


async def save_config(values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return await _post("/internal/setup/config", {"values": values}, timeout=10.0)


def is_available() -> bool:
    """是否具备调用条件（有 token 即认为主进程已启动过桥）。"""
    return bool(_token())


async def run_summary(task_id: int):
    """让主进程立即执行一个总结任务。"""
    return await _post("/internal/summaries/run", {"task_id": int(task_id)})
