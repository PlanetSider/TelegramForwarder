"""飞书 / Lark 应用扫码注册（设备码风格的 OAuth 自助建应用）。

飞书给 PersonalAgent 类应用留了一条自助注册通道：用户在手机飞书里扫码确认，
服务端就能直接拿到 `app_id` / `app_secret`，省掉「去开放平台建应用 →
复制两串凭据 → 回来粘贴」。

需要说明的两点：
1. 这条通道是飞书账号系统的内部接口，官方没有把它当稳定公开 API 承诺，
   所以控制台一直保留手动填写作为兜底；
2. 扫码只负责「建应用 + 发凭据」，应用还需要在开放平台开通权限并发布版本，
   权限才会真正生效——这一步没法绕过。
"""

import logging
import secrets
import time
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

FEISHU_ENDPOINT = "https://accounts.feishu.cn/oauth/v1/app/registration"
LARK_ENDPOINT = "https://accounts.larksuite.com/oauth/v1/app/registration"

ARCHETYPE = "PersonalAgent"
AUTH_METHOD = "client_secret"

# 扫码会话只放内存：一次部署一个进程，够用；重启后重新扫码即可。
_SESSIONS: Dict[str, Dict[str, Any]] = {}
_MAX_SESSIONS = 20
_DEFAULT_EXPIRES = 600
_DEFAULT_INTERVAL = 5
_MAX_INTERVAL = 60


def _endpoint(platform: str) -> str:
    return LARK_ENDPOINT if (platform or "").lower() == "lark" else FEISHU_ENDPOINT


def _new_client(timeout: float) -> httpx.AsyncClient:
    """本机 NO_PROXY 里带 [::1] 会让 httpx 构造失败，退一步关掉环境代理。"""
    try:
        return httpx.AsyncClient(timeout=timeout)
    except Exception:
        return httpx.AsyncClient(timeout=timeout, trust_env=False)


def _prune() -> None:
    now = time.time()
    for sid in [k for k, v in _SESSIONS.items() if v["expires_at"] <= now]:
        _SESSIONS.pop(sid, None)
    if len(_SESSIONS) > _MAX_SESSIONS:
        oldest = sorted(_SESSIONS.items(), key=lambda kv: kv[1]["created_at"])
        for sid, _ in oldest[: len(_SESSIONS) - _MAX_SESSIONS]:
            _SESSIONS.pop(sid, None)


async def _post(endpoint: str, payload: Dict[str, str], timeout: float = 15.0) -> Tuple[int, Dict[str, Any]]:
    """调注册端点。非 2xx 也可能带结构化 error，交给调用方判断。"""
    async with _new_client(timeout) as client:
        resp = await client.post(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    try:
        body = resp.json()
    except Exception:
        body = {"error": "invalid_response", "error_description": resp.text[:200]}
    return resp.status_code, body


def _safe_url(url: str) -> Optional[str]:
    """只接受飞书/Lark 官方域名的 https 地址，避免把上游 URL 当成钓鱼链接渲染。"""
    if not url or not url.startswith("https://"):
        return None
    from urllib.parse import urlparse

    parsed = urlparse(url)
    allowed = {
        "accounts.feishu.cn",
        "accounts.larksuite.com",
        "open.feishu.cn",
        "open.larksuite.com",
    }
    if parsed.hostname not in allowed:
        return None
    if parsed.port not in (None, 443):
        return None
    if parsed.username or parsed.password:
        return None
    return url


async def start(platform: str = "feishu") -> Dict[str, Any]:
    """发起一次扫码注册，返回会话号与二维码内容。"""
    _prune()
    endpoint = _endpoint(platform)

    status, body = await _post(endpoint, {"action": "init"})
    if status != 200 or not body.get("nonce"):
        return {"ok": False, "error": f"初始化失败：{body.get('error_description') or body.get('error') or status}"}

    status, body = await _post(endpoint, {
        "action": "begin",
        "archetype": ARCHETYPE,
        "auth_method": AUTH_METHOD,
        "nonce": body["nonce"],
    })
    if status != 200 or not body.get("device_code"):
        return {"ok": False, "error": f"创建扫码会话失败：{body.get('error_description') or body.get('error') or status}"}

    url = _safe_url(body.get("verification_uri_complete") or body.get("verification_uri") or "")
    if not url:
        return {"ok": False, "error": "飞书返回了无效的验证地址，请重试"}

    expires = body.get("expires_in") or body.get("expire_in") or _DEFAULT_EXPIRES
    try:
        expires = int(expires)
    except (TypeError, ValueError):
        expires = _DEFAULT_EXPIRES
    if expires <= 0 or expires > 1800:
        expires = _DEFAULT_EXPIRES

    interval = body.get("interval") or _DEFAULT_INTERVAL
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = _DEFAULT_INTERVAL
    interval = max(2, min(interval, _MAX_INTERVAL))

    session_id = secrets.token_urlsafe(16)
    now = time.time()
    _SESSIONS[session_id] = {
        "device_code": body["device_code"],
        "endpoint": endpoint,
        "platform": "lark" if endpoint == LARK_ENDPOINT else "feishu",
        "interval": interval,
        "expires_at": now + expires,
        "next_poll_at": now + interval,
        "created_at": now,
        "user_code": body.get("user_code") or "",
    }

    return {
        "ok": True,
        "session_id": session_id,
        "url": url,
        "user_code": body.get("user_code") or "",
        "expires_in": expires,
        "interval": interval,
        "platform": _SESSIONS[session_id]["platform"],
    }


async def poll(session_id: str) -> Dict[str, Any]:
    """查询扫码进度。已扫过码就返回 app_id / app_secret。"""
    session = _SESSIONS.get(session_id)
    if session is None:
        return {"status": "expired", "error": "扫码会话不存在或已过期，请重新生成"}

    now = time.time()
    if now >= session["expires_at"]:
        _SESSIONS.pop(session_id, None)
        return {"status": "expired", "error": "二维码已过期，请重新生成"}

    # 节流：没到轮询间隔就不打上游
    if now < session["next_poll_at"]:
        return {
            "status": "pending",
            "retry_after": max(1, int(session["next_poll_at"] - now)),
        }

    status, body = await _post(session["endpoint"], {
        "action": "poll",
        "device_code": session["device_code"],
    })

    error = (body.get("error") or "").strip()
    client_id = body.get("client_id") or body.get("app_id") or ""
    client_secret = body.get("client_secret") or body.get("app_secret") or ""

    # 还在等用户扫码：飞书用 4xx + authorization_pending 表达，属于正常中间态
    if error == "authorization_pending" or (not error and not client_id):
        session["next_poll_at"] = time.time() + session["interval"]
        return {"status": "pending", "retry_after": session["interval"]}

    if error == "slow_down":
        session["interval"] = min(session["interval"] + 5, _MAX_INTERVAL)
        session["next_poll_at"] = time.time() + session["interval"]
        return {"status": "pending", "retry_after": session["interval"]}

    if error in ("access_denied", "user_denied"):
        _SESSIONS.pop(session_id, None)
        return {"status": "denied", "error": "你取消了授权"}

    if error in ("expired_token", "expired"):
        _SESSIONS.pop(session_id, None)
        return {"status": "expired", "error": "二维码已过期，请重新生成"}

    if not client_id or not client_secret:
        # 其它未知情况不直接判失败，把上游原话透出去，同时继续轮询
        session["next_poll_at"] = time.time() + session["interval"]
        return {
            "status": "pending",
            "error": body.get("error_description") or error or "等待授权",
            "retry_after": session["interval"],
        }

    user_info = body.get("user_info") or {}
    tenant_brand = (user_info.get("tenant_brand") or "").lower()
    platform = session["platform"]
    if tenant_brand in ("feishu", "lark"):
        platform = tenant_brand

    _SESSIONS.pop(session_id, None)

    return {
        "status": "complete",
        "app_id": client_id,
        "app_secret": client_secret,
        "platform": platform,
        "open_id": user_info.get("open_id") or "",
        "tenant_brand": tenant_brand,
    }
