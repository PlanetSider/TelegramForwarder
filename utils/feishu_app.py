"""飞书 / Lark 应用机器人的主动发送。

与 ``utils/feishu.py`` 的区别：
- ``feishu.py`` 走的是群自定义机器人的 webhook，只能发、不能收；
- 本模块走开放平台应用凭证（app_id / app_secret），能查机器人所在的群、
  能往群里发消息，也是 ``feishu_bot.py`` 那条长连接命令通道的同一批应用。

飞书（中国）与 Lark（国际）是两个独立的开放平台，域名和 token 都不通用，
所以每个机器人单独记 platform。
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

FEISHU_DOMAIN = "https://open.feishu.cn"
LARK_DOMAIN = "https://open.larksuite.com"

# 单条文本上限按 20KB 留一点余量，超过就截断
MAX_TEXT_BYTES = 20 * 1024 - 1024

_TOKEN_CACHE: Dict[str, Tuple[str, float]] = {}


def _domain(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    return LARK_DOMAIN if str(value).lower() == "lark" else FEISHU_DOMAIN


def _attr(bot: Any, name: str, default: Any = None) -> Any:
    if isinstance(bot, dict):
        return bot.get(name, default)
    return getattr(bot, name, default)


def bot_label(bot: Any) -> str:
    """列表里展示的名字：有昵称用昵称，没有就退到 app_id。"""
    name = (_attr(bot, "name") or "").strip()
    return name or (_attr(bot, "app_id") or "未命名机器人")


def _new_client(timeout: float) -> httpx.AsyncClient:
    """本机 NO_PROXY 里含 ``[::1]`` 时 httpx 会解析失败，退一步关掉环境代理。"""
    try:
        return httpx.AsyncClient(timeout=timeout)
    except Exception:
        return httpx.AsyncClient(timeout=timeout, trust_env=False)


def _truncate(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    data = raw.encode("utf-8")
    if len(data) <= MAX_TEXT_BYTES:
        return raw
    cut = data[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
    return cut + "\n\n（内容过长，已截断）"


async def get_tenant_token(bot: Any, timeout: float = 10.0) -> Optional[str]:
    """取 tenant_access_token，进程内带缓存（提前 5 分钟过期）。"""
    app_id = _attr(bot, "app_id")
    app_secret = _attr(bot, "app_secret")
    if not app_id or not app_secret:
        return None

    cached = _TOKEN_CACHE.get(app_id)
    if cached and cached[1] > time.time():
        return cached[0]

    url = _domain(_attr(bot, "platform")) + "/open-apis/auth/v3/tenant_access_token/internal"
    try:
        async with _new_client(timeout) as client:
            resp = await client.post(
                url, json={"app_id": app_id, "app_secret": app_secret}
            )
        data = resp.json()
    except Exception as exc:
        logger.warning(f"获取飞书 tenant_access_token 失败（{app_id}）: {exc}")
        return None

    if data.get("code") != 0:
        logger.warning(
            f"获取飞书 tenant_access_token 失败（{app_id}）: "
            f"{data.get('code')} {data.get('msg')}"
        )
        return None

    token = data.get("tenant_access_token")
    expire = int(data.get("expire") or 7200)
    if token:
        _TOKEN_CACHE[app_id] = (token, time.time() + max(expire - 300, 60))
    return token


async def list_chats(bot: Any, timeout: float = 20.0) -> List[Dict[str, Any]]:
    """机器人所在的群列表（只返回群，不含单聊）。"""
    token = await get_tenant_token(bot)
    if not token:
        return []

    url = _domain(_attr(bot, "platform")) + "/open-apis/im/v1/chats"
    out: List[Dict[str, Any]] = []
    page_token = ""
    try:
        async with _new_client(timeout) as client:
            for _ in range(10):  # 最多 10 页，够用了
                params = {"page_size": 100}
                if page_token:
                    params["page_token"] = page_token
                resp = await client.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
                data = resp.json()
                if data.get("code") != 0:
                    logger.warning(
                        f"获取飞书群列表失败（{_attr(bot, 'app_id')}）: "
                        f"{data.get('code')} {data.get('msg')}"
                    )
                    break
                payload = data.get("data") or {}
                for item in payload.get("items") or []:
                    out.append(
                        {
                            "chat_id": item.get("chat_id"),
                            "name": item.get("name") or item.get("chat_id"),
                            "description": item.get("description") or "",
                        }
                    )
                if not payload.get("has_more"):
                    break
                page_token = payload.get("page_token") or ""
                if not page_token:
                    break
    except Exception as exc:
        logger.warning(f"获取飞书群列表出错（{_attr(bot, 'app_id')}）: {exc}")
    return out


async def send_text(
    bot: Any, chat_id: str, text: str, timeout: float = 20.0
) -> Tuple[bool, str]:
    """用应用机器人往群里发一条纯文本。返回 (是否成功, 说明)。"""
    body_text = _truncate(text)
    if not body_text:
        return False, "内容为空"
    if not chat_id:
        return False, "未选择目标群"

    token = await get_tenant_token(bot)
    if not token:
        return False, "获取 tenant_access_token 失败（检查 App ID / Secret / 平台）"

    url = _domain(_attr(bot, "platform")) + "/open-apis/im/v1/messages"
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": body_text}, ensure_ascii=False),
    }
    try:
        async with _new_client(timeout) as client:
            resp = await client.post(
                url,
                params={"receive_id_type": "chat_id"},
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        data = resp.json()
    except Exception as exc:
        return False, f"请求失败: {exc}"

    if data.get("code") == 0:
        return True, "ok"
    return False, f"{data.get('code')} {data.get('msg') or '发送失败'}"
