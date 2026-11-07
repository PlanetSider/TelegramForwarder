"""主进程 ⇄ 子进程的内部桥。

背景：RSS/控制台跑在 `multiprocessing.Process` 子进程里，
拿不到主进程持有的 Telethon 客户端。但控制台需要展示
「Telegram 是否在线」「有哪些会话可以绑定」这类只有主进程才知道的信息。

做法：主进程在自己的 asyncio loop 里起一个极小的 HTTP 服务，
只监听 127.0.0.1，用共享 token 鉴权；子进程通过 `bridge_client` 调用。

刻意保持只读：这里不提供「触发转发」「改数据库」这类写操作，
避免控制台成为绕过过滤链的后门。
"""
import asyncio
import logging
import os
import secrets
from typing import Any, Dict, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("BRIDGE_PORT", "9805"))

_TOKEN_ENV = "BRIDGE_TOKEN"


def ensure_token() -> str:
    """取出/生成内部 token。

    必须在 fork 出 RSS 子进程**之前**调用，这样子进程能从环境变量里读到同一个值。
    """
    token = os.getenv(_TOKEN_ENV)
    if not token:
        token = secrets.token_urlsafe(24)
        os.environ[_TOKEN_ENV] = token
        logger.info("已生成内部桥 token")
    return token


def current_token() -> Optional[str]:
    return os.getenv(_TOKEN_ENV)


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _check(request: web.Request) -> bool:
    token = current_token()
    if not token:
        return False
    supplied = request.headers.get("X-Bridge-Token") or request.query.get("token")
    return bool(supplied) and secrets.compare_digest(str(supplied), token)


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path.startswith("/internal/") and not _check(request):
        return _json_error("未授权", 401)
    return await handler(request)


def _clients():
    """从 main 模块取两个 Telethon 客户端（可能尚未就绪）。"""
    try:
        from utils.common import get_main_module
        main = get_main_module()
        return getattr(main, "user_client", None), getattr(main, "bot_client", None)
    except Exception as exc:
        logger.debug("获取客户端失败: %s", exc)
        return None, None


async def _handle_status(request: web.Request) -> web.Response:
    user_client, bot_client = _clients()

    payload: Dict[str, Any] = {
        "ok": True,
        "telegram": {
            "user_connected": bool(user_client and user_client.is_connected()),
            "bot_connected": bool(bot_client and bot_client.is_connected()),
        },
    }
    if bot_client is not None and bot_client.is_connected():
        try:
            me = await asyncio.wait_for(bot_client.get_me(), timeout=5)
            payload["telegram"]["bot_username"] = getattr(me, "username", None)
            payload["telegram"]["bot_id"] = getattr(me, "id", None)
        except Exception as exc:
            payload["telegram"]["bot_error"] = str(exc)

    # 飞书应用机器人：状态由 feishu_bot 模块维护，主进程内直接读取
    try:
        from utils.feishu_bot import get_bot_status
        payload["feishu"] = get_bot_status()
    except Exception as exc:  # pragma: no cover - 状态不可读不应影响概览
        payload["feishu"] = {"connected": False, "configured": False, "error": str(exc)}

    return web.json_response(payload)


async def _handle_dialogs(request: web.Request) -> web.Response:
    """列出用户账号可访问的会话，供控制台「新建规则」选择源频道。"""
    user_client, _ = _clients()
    if user_client is None or not user_client.is_connected():
        return _json_error("用户客户端未连接", 503)

    limit = request.query.get("limit", "200")
    try:
        limit = max(1, min(int(limit), 500))
    except ValueError:
        limit = 200

    items = []
    try:
        async for dialog in user_client.iter_dialogs(limit=limit):
            items.append({
                "id": dialog.id,
                "name": dialog.name or "未命名",
                "is_channel": bool(getattr(dialog, "is_channel", False)),
                "is_group": bool(getattr(dialog, "is_group", False)),
                "is_user": bool(getattr(dialog, "is_user", False)),
            })
    except Exception as exc:
        logger.warning("拉取会话列表失败: %s", exc)
        return _json_error(f"拉取会话列表失败: {exc}", 500)

    return web.json_response({"ok": True, "count": len(items), "items": items})


async def _resolve_entity(client, text: str):
    """把用户输入解析成 Telegram 实体。

    依次尝试：纯数字 ID → @username / 链接 → 会话名的模糊包含匹配。
    与 Bot 端 `/bind` 的解析顺序保持一致，避免两边行为不一致。
    """
    text = (text or "").strip()
    if not text:
        return None, "输入为空"

    # 链接取最后一段：https://t.me/xxx -> xxx
    candidate = text
    if candidate.startswith("http"):
        candidate = candidate.rstrip("/").split("/")[-1]

    if candidate.startswith("@"):
        candidate = candidate[1:]

    # 纯数字 ID
    try:
        return await client.get_entity(int(candidate)), ""
    except Exception:
        pass

    # username / 公开链接
    try:
        return await client.get_entity(candidate), ""
    except Exception as exc:
        last_error = str(exc)

    # 名称模糊匹配
    try:
        async for dialog in client.iter_dialogs(limit=300):
            if candidate.lower() in (dialog.name or "").lower():
                return dialog.entity, ""
    except Exception as exc:
        return None, f"解析失败: {exc}"

    return None, f"未找到会话（{last_error}）"


async def _handle_create_rule(request: web.Request) -> web.Response:
    """按「源 / 目标」创建一条转发规则（复用主进程的 Telethon 实体解析）。"""
    user_client, _ = _clients()
    if user_client is None or not user_client.is_connected():
        return _json_error("用户客户端未连接", 503)

    try:
        body = await request.json()
    except Exception:
        body = {}
    source_text = (body.get("source") or "").strip()
    target_text = (body.get("target") or "").strip()
    if not source_text or not target_text:
        return _json_error("源与目标都必须填写")

    source_entity, source_err = await _resolve_entity(user_client, source_text)
    if source_entity is None:
        return _json_error(f"源会话解析失败：{source_err}")
    target_entity, target_err = await _resolve_entity(user_client, target_text)
    if target_entity is None:
        return _json_error(f"目标会话解析失败：{target_err}")

    from models.models import get_session, Chat, ForwardRule
    from enums.enums import ForwardMode, AddMode

    session = get_session()
    try:
        def _upsert(entity):
            tg_id = str(entity.id)
            chat = (session.query(Chat)
                    .filter(Chat.telegram_chat_id == tg_id).first())
            if chat is None:
                chat = Chat(telegram_chat_id=tg_id,
                            name=getattr(entity, "title", None) or getattr(entity, "username", None))
                session.add(chat)
                session.flush()
            return chat

        source_chat = _upsert(source_entity)
        target_chat = _upsert(target_entity)

        existed = (session.query(ForwardRule)
                   .filter(ForwardRule.source_chat_id == source_chat.id,
                           ForwardRule.target_chat_id == target_chat.id).first())
        if existed:
            return _json_error(f"已存在相同的转发规则（#{existed.id}）", 409)

        same = source_chat.id == target_chat.id
        rule = ForwardRule(
            source_chat_id=source_chat.id,
            target_chat_id=target_chat.id,
            forward_mode=ForwardMode.WHITELIST if same else ForwardMode.BLACKLIST,
            add_mode=AddMode.WHITELIST if same else AddMode.BLACKLIST,
        )
        session.add(rule)
        session.commit()
        return web.json_response({"ok": True, "rule_id": rule.id})
    except Exception as exc:
        session.rollback()
        logger.exception("创建规则失败")
        return _json_error(f"创建失败: {exc}", 500)
    finally:
        session.close()


async def _handle_setup_status(request: web.Request) -> web.Response:
    """配置向导轮询用：当前登录阶段 + 凭据是否齐全。"""
    from models.models import get_session
    from utils import app_config
    from utils.telegram_login import login_manager

    session = get_session()
    try:
        configured = app_config.is_telegram_configured(session)
    except Exception as exc:
        logger.debug("读取配置状态失败: %s", exc)
        configured = False
    finally:
        session.close()

    payload: Dict[str, Any] = {"ok": True, "configured": configured}
    payload.update(login_manager.snapshot())
    return web.json_response(payload)


async def _handle_send_code(request: web.Request) -> web.Response:
    """保存 Telegram 凭据并发送登录验证码。"""
    from models.models import get_session
    from utils import app_config
    from utils.telegram_login import login_manager

    try:
        body = await request.json()
    except Exception:
        body = {}

    api_id = str(body.get("api_id") or "").strip()
    api_hash = str(body.get("api_hash") or "").strip()
    phone = str(body.get("phone") or "").strip()
    if not (api_id and api_hash and phone):
        return _json_error("API_ID、API_HASH、手机号都必须填写")

    session = get_session()
    try:
        _, errors = app_config.save(session, {
            "API_ID": api_id,
            "API_HASH": api_hash,
            "PHONE_NUMBER": phone,
        })
    finally:
        session.close()
    if errors:
        return _json_error("；".join(errors))

    snapshot = await login_manager.begin(api_id, api_hash, phone)
    ok = snapshot.get("status") in ("code_sent", "authorized")
    return web.json_response({"ok": ok, **snapshot}, status=200 if ok else 400)


async def _handle_verify_code(request: web.Request) -> web.Response:
    """提交登录验证码。"""
    from utils.telegram_login import login_manager

    try:
        body = await request.json()
    except Exception:
        body = {}

    code = str(body.get("code") or "").strip()
    if not code:
        return _json_error("请输入验证码")

    snapshot = await login_manager.submit_code(code)
    ok = snapshot.get("status") in ("authorized", "need_password")
    return web.json_response({"ok": ok, **snapshot}, status=200 if ok else 400)


async def _handle_verify_password(request: web.Request) -> web.Response:
    """提交两步验证（2FA）密码。"""
    from utils.telegram_login import login_manager

    try:
        body = await request.json()
    except Exception:
        body = {}

    password = str(body.get("password") or "")
    if not password:
        return _json_error("请输入两步验证密码")

    snapshot = await login_manager.submit_password(password)
    ok = snapshot.get("status") == "authorized"
    return web.json_response({"ok": ok, **snapshot}, status=200 if ok else 400)


async def _handle_save_config(request: web.Request) -> web.Response:
    """保存配置项（向导与配置页共用），只接受白名单内的键。"""
    from models.models import get_session
    from utils import app_config

    try:
        body = await request.json()
    except Exception:
        return _json_error("请求体不是合法 JSON")

    updates = body.get("values")
    if not isinstance(updates, dict) or not updates:
        return _json_error("没有需要保存的配置")

    session = get_session()
    try:
        saved, errors = app_config.save(session, updates)
    finally:
        session.close()

    if errors:
        return web.json_response({"ok": False, "saved": saved, "errors": errors}, status=400)
    return web.json_response({"ok": True, "saved": saved})


async def start_bridge(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    """在当前事件循环里启动内部 HTTP 服务（幂等，失败只告警）。"""
    ensure_token()

    app = web.Application(middlewares=[_auth_middleware])
    app.router.add_get("/internal/status", _handle_status)
    app.router.add_get("/internal/dialogs", _handle_dialogs)
    app.router.add_post("/internal/rules", _handle_create_rule)
    app.router.add_get("/internal/setup/status", _handle_setup_status)
    app.router.add_post("/internal/setup/send-code", _handle_send_code)
    app.router.add_post("/internal/setup/verify-code", _handle_verify_code)
    app.router.add_post("/internal/setup/verify-password", _handle_verify_password)
    app.router.add_post("/internal/setup/config", _handle_save_config)

    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"内部桥已启动: http://{host}:{port}")
        return runner
    except Exception as exc:
        logger.warning(f"内部桥启动失败（控制台将显示 Telegram 状态不可用）: {exc}")
        try:
            await runner.cleanup()
        except Exception:
            pass
        return None
