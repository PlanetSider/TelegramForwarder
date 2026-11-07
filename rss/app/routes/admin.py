"""Web 控制台路由（/admin）。

设计要点：
- 复用 RSS 面板的登录态（`get_current_user` + JWT Cookie），不新建用户体系；
- 页面只做渲染，业务查询走 `rss.app.services.rule_service`；
- 所有 ORM 对象在离开 session 前转换成 dict，避免模板阶段触发 DetachedInstanceError。

能力边界（重要）：
- 本进程是 RSS 子进程（multiprocessing），**拿不到 Telethon 客户端**；
  因此这里只做「数据库可完成」的配置管理，
  涉及 Telegram 实体的操作（拉聊天列表、立即转发）走主进程接口（见 P4 说明）。
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from models.models import get_session
from rss.app.routes.auth import get_current_user
from rss.app.services import rule_service
from utils.log_buffer import get_recent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="rss/app/templates")


# ---------------------------------------------------------------- 辅助

def _login_redirect():
    return RedirectResponse(url="/login", status_code=302)


def _ctx(request: Request, user, active: str, **extra):
    """统一模板上下文。"""
    data = {
        "request": request,
        "user": user,
        "active": active,
        "project_name": "TG Forwarder",
    }
    data.update(extra)
    return data


# ---------------------------------------------------------------- 页面

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_index(request: Request, user=Depends(get_current_user)):
    """控制台首页：概览。"""
    if not user:
        return _login_redirect()

    session = get_session()
    try:
        overview = rule_service.get_overview(session)
        health = rule_service.get_rule_health(session, limit=5)
    finally:
        session.close()

    # Telegram 状态只有主进程知道，经内部桥只读获取；不可达时为 None
    from rss.app.services import bridge_client
    telegram = await bridge_client.get_status()

    return templates.TemplateResponse(
        "admin_dashboard.html",
        _ctx(request, user, "dashboard", overview=overview, health=health,
             telegram=telegram),
    )


@router.get("/rules", response_class=HTMLResponse)
async def admin_rules(request: Request, user=Depends(get_current_user),
                      q: str = "", status: str = "", mode: str = ""):
    """规则列表。"""
    if not user:
        return _login_redirect()

    session = get_session()
    try:
        rules = rule_service.list_rules(session, q=q, status=status, mode=mode)
    finally:
        session.close()

    return templates.TemplateResponse(
        "admin_rules.html",
        _ctx(request, user, "rules", rules=rules,
             q=q, status=status, mode=mode),
    )


@router.get("/rules/{rule_id}", response_class=HTMLResponse)
async def admin_rule_detail(request: Request, rule_id: int,
                            user=Depends(get_current_user)):
    """规则详情（只读展示 + 表单骨架，写操作见 P1 接口）。"""
    if not user:
        return _login_redirect()

    session = get_session()
    try:
        rule = rule_service.get_rule(session, rule_id)
        siblings = rule_service.list_rules(session)
    finally:
        session.close()

    if not rule:
        return RedirectResponse(url="/admin/rules", status_code=302)

    return templates.TemplateResponse(
        "admin_rule_detail.html",
        _ctx(request, user, "rules", rule=rule, siblings=siblings),
    )


@router.get("/settings", response_class=HTMLResponse)
async def admin_settings(request: Request, user=Depends(get_current_user)):
    """全局设置：读写 app_settings 表里的全部配置项。"""
    if not user:
        return _login_redirect()

    from utils import app_config
    from utils import settings as settings_loader

    session = get_session()
    try:
        groups = app_config.grouped()
        rows = app_config.as_display(session)
    finally:
        session.close()

    # 模板按 key 取展示值：敏感项在这里已经打码，真实值不会进 HTML
    view = {row["key"]: row for row in rows}

    try:
        ai_models = settings_loader.load_ai_models(type="dict")
    except Exception as exc:
        logger.warning("加载 AI 模型配置失败: %s", exc)
        ai_models = {}

    from rss.app.services import config_service
    try:
        configs = config_service.list_configs()
    except Exception as exc:
        logger.warning("读取配置文件失败: %s", exc)
        configs = []

    return templates.TemplateResponse(
        "admin_settings.html",
        _ctx(request, user, "settings",
             groups=groups,
             view=view,
             ai_models=ai_models,
             configs=configs),
    )


@router.get("/logs", response_class=HTMLResponse)
async def admin_logs(request: Request, user=Depends(get_current_user),
                     level: str = "", q: str = ""):
    """日志页。"""
    if not user:
        return _login_redirect()

    entries = get_recent(limit=300, level=level or None, keyword=q or None)
    return templates.TemplateResponse(
        "admin_logs.html",
        _ctx(request, user, "logs", entries=entries, level=level, q=q),
    )


# ---------------------------------------------------------------- JSON 接口

@router.get("/api/logs")
async def admin_api_logs(user=Depends(get_current_user),
                         level: str = "", q: str = "", limit: int = 300):
    """供前端轮询/SSE 复用的日志接口。"""
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    limit = max(1, min(limit, 2000))
    return JSONResponse({
        "ok": True,
        "entries": get_recent(limit=limit, level=level or None, keyword=q or None),
    })


@router.get("/api/overview")
async def admin_api_overview(user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    session = get_session()
    try:
        data = rule_service.get_overview(session)
    finally:
        session.close()
    return JSONResponse({"ok": True, "data": data})


# ---------------------------------------------------------------- 写接口（P1）
#
# 统一约定：成功 {"ok": true, ...}；失败 {"ok": false, "error": "中文原因"}。
# 全部要求登录态，未登录返回 401（前端 toast 提示）。

def _fail(message: str, status: int = 400):
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def _json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@router.post("/api/rules/{rule_id}")
async def admin_api_update_rule(rule_id: int, request: Request,
                                user=Depends(get_current_user)):
    """批量更新规则字段（白名单，未知名静默忽略）。"""
    if not user:
        return _fail("未登录", 401)

    body = await _json_body(request)
    session = get_session()
    try:
        result = rule_service.update_rule_fields(session, rule_id, body)
        return JSONResponse({"ok": True, **{k: v for k, v in result.items() if k != "rule"}})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except Exception as exc:
        logger.exception("更新规则失败 rule=%s", rule_id)
        return _fail(f"保存失败: {exc}", 500)
    finally:
        session.close()


@router.post("/api/rules")
async def admin_api_create_rule(request: Request,
                                user=Depends(get_current_user)):
    """新建规则：实体解析在主进程完成，这里只做转发调用。"""
    if not user:
        return _fail("未登录", 401)

    from rss.app.services import bridge_client
    body = await _json_body(request)
    source = (body.get("source") or "").strip()
    target = (body.get("target") or "").strip()
    if not source or not target:
        return _fail("源与目标都必须填写")

    try:
        async with bridge_client._new_client(20.0) as client:  # noqa: SLF001
            resp = await client.post(
                f"{bridge_client._BASE}/internal/rules",  # noqa: SLF001
                headers={"X-Bridge-Token": bridge_client._token() or ""},  # noqa: SLF001
                json={"source": source, "target": target},
            )
            data = resp.json()
    except Exception as exc:
        logger.warning("调用主进程创建规则失败: %s", exc)
        return _fail("主进程不可达，无法创建规则（需要 Telethon 会话实体）", 503)

    if not data.get("ok"):
        return _fail(data.get("error") or "创建失败", resp.status_code if 400 <= resp.status_code < 500 else 400)
    return JSONResponse({"ok": True, "rule_id": data.get("rule_id")})


@router.post("/api/rules/{rule_id}/toggle")
async def admin_api_toggle_rule(rule_id: int, request: Request,
                                user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)

    body = await _json_body(request)
    enabled = body.get("enabled")
    session = get_session()
    try:
        value = rule_service.toggle_rule(session, rule_id, bool(enabled))
        return JSONResponse({"ok": True, "enabled": value})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except Exception as exc:
        logger.exception("切换规则状态失败 rule=%s", rule_id)
        return _fail(f"操作失败: {exc}", 500)
    finally:
        session.close()


# ---------------------------------------------------------------- 推送地址

@router.post("/api/rules/{rule_id}/push")
async def admin_api_add_push(rule_id: int, request: Request,
                             user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)

    body = await _json_body(request)
    url = (body.get("url") or "").strip()
    mode = body.get("media_send_mode") or "Single"

    # 先做格式校验，避免把无法使用的地址写进库
    try:
        from utils.feishu import parse_feishu_url
        import apprise
        if parse_feishu_url(url) is None:
            if not apprise.Apprise().add(url):
                return _fail("无法识别的推送地址（支持 Apprise URL 或飞书/Lark webhook）")
    except ImportError:
        pass  # 依赖缺失时交给运行时判断，不阻断保存
    except Exception as exc:
        return _fail(f"地址校验失败: {exc}")

    session = get_session()
    try:
        item = rule_service.add_push_channel(session, rule_id, url, mode)
        return JSONResponse({"ok": True, "item": item})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        logger.exception("新增推送地址失败 rule=%s", rule_id)
        return _fail(f"保存失败: {exc}", 500)
    finally:
        session.close()


@router.post("/api/push/{push_id}/toggle")
async def admin_api_toggle_push(push_id: int, request: Request,
                                user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    body = await _json_body(request)
    session = get_session()
    try:
        value = rule_service.toggle_push_channel(session, push_id, bool(body.get("enabled")))
        return JSONResponse({"ok": True, "enabled": value})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except Exception as exc:
        return _fail(f"操作失败: {exc}", 500)
    finally:
        session.close()


@router.delete("/api/push/{push_id}")
async def admin_api_delete_push(push_id: int, user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    session = get_session()
    try:
        rule_service.delete_push_channel(session, push_id)
        return JSONResponse({"ok": True})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except Exception as exc:
        return _fail(f"删除失败: {exc}", 500)
    finally:
        session.close()


# ---------------------------------------------------------------- 关键字

@router.post("/api/rules/{rule_id}/keywords")
async def admin_api_add_keyword(rule_id: int, request: Request,
                                user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    body = await _json_body(request)
    session = get_session()
    try:
        item = rule_service.add_keyword(
            session, rule_id,
            body.get("keyword") or "",
            bool(body.get("is_regex")),
            bool(body.get("is_blacklist", True)),
        )
        return JSONResponse({"ok": True, "item": item})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(f"保存失败: {exc}", 500)
    finally:
        session.close()


@router.delete("/api/keywords/{keyword_id}")
async def admin_api_delete_keyword(keyword_id: int, user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    session = get_session()
    try:
        rule_service.delete_keyword(session, keyword_id)
        return JSONResponse({"ok": True})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except Exception as exc:
        return _fail(f"删除失败: {exc}", 500)
    finally:
        session.close()


# ---------------------------------------------------------------- 替换

@router.post("/api/rules/{rule_id}/replaces")
async def admin_api_add_replace(rule_id: int, request: Request,
                                user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    body = await _json_body(request)
    session = get_session()
    try:
        item = rule_service.add_replace(
            session, rule_id, body.get("pattern") or "", body.get("content") or "")
        return JSONResponse({"ok": True, "item": item})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(f"保存失败: {exc}", 500)
    finally:
        session.close()


@router.delete("/api/replaces/{replace_id}")
async def admin_api_delete_replace(replace_id: int, user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    session = get_session()
    try:
        rule_service.delete_replace(session, replace_id)
        return JSONResponse({"ok": True})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except Exception as exc:
        return _fail(f"删除失败: {exc}", 500)
    finally:
        session.close()


# ---------------------------------------------------------------- 配置文件（P3）

@router.get("/api/configs")
async def admin_api_list_configs(user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    from rss.app.services import config_service
    return JSONResponse({"ok": True, "items": config_service.list_configs()})


@router.post("/api/configs/{name}")
async def admin_api_write_config(name: str, request: Request,
                                 user=Depends(get_current_user)):
    """写入 config/ 下的白名单文本配置。"""
    if not user:
        return _fail("未登录", 401)

    from rss.app.services import config_service
    body = await _json_body(request)
    content = body.get("content")
    if not isinstance(content, str):
        return _fail("内容必须是字符串")

    try:
        result = config_service.write_config(name, content)
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        logger.exception("写入配置失败 %s", name)
        return _fail(f"写入失败: {exc}", 500)


# ---------------------------------------------------------------- 配置向导

@router.get("/setup", response_class=HTMLResponse)
async def admin_setup(request: Request, user=Depends(get_current_user)):
    """初始化向导：填 Telegram 凭据 → 收验证码 → 两步验证。

    向导本身也要登录，否则任何人都能改掉 API_ID 把实例劫走。
    首次部署时唯一的账号就是刚注册的管理员。
    """
    if not user:
        return _login_redirect()

    from rss.app.services import bridge_client
    from utils import app_config

    status = await bridge_client.get_setup_status()

    session = get_session()
    try:
        current = app_config.as_display(session)
        configured = app_config.is_telegram_configured(session)
        values = app_config.get_current(session)
    finally:
        session.close()

    # 只回填非敏感项，密钥类一律留空让用户重填
    defaults = {
        "api_id": values.get("API_ID", ""),
        "phone": values.get("PHONE_NUMBER", ""),
    }

    return templates.TemplateResponse(
        "admin_setup.html",
        _ctx(request, user, "setup",
             status=status or {},
             configured=configured,
             current=current,
             defaults=defaults),
    )


@router.get("/api/setup/status")
async def admin_api_setup_status(user=Depends(get_current_user)):
    """前端轮询：登录阶段是否变化。"""
    if not user:
        return _fail("未登录", 401)

    from rss.app.services import bridge_client
    status = await bridge_client.get_setup_status()
    if status is None:
        return JSONResponse({"ok": False, "error": "主进程内部桥不可达"}, status_code=503)
    return JSONResponse(status)


@router.post("/api/setup/send-code")
async def admin_api_setup_send_code(request: Request, user=Depends(get_current_user)):
    """保存 Telegram 凭据并发送验证码。"""
    if not user:
        return _fail("未登录", 401)

    from rss.app.services import bridge_client

    body = await _json_body(request)
    api_id = str(body.get("api_id") or "").strip()
    api_hash = str(body.get("api_hash") or "").strip()
    phone = str(body.get("phone") or "").strip()
    if not (api_id and api_hash and phone):
        return _fail("API_ID、API_HASH、手机号都必须填写")

    result = await bridge_client.send_code(api_id, api_hash, phone)
    if result is None:
        return JSONResponse({"ok": False, "error": "主进程内部桥不可达"}, status_code=503)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/setup/verify-code")
async def admin_api_setup_verify_code(request: Request, user=Depends(get_current_user)):
    """提交登录验证码。"""
    if not user:
        return _fail("未登录", 401)

    from rss.app.services import bridge_client

    body = await _json_body(request)
    code = str(body.get("code") or "").strip()
    if not code:
        return _fail("请输入验证码")

    result = await bridge_client.verify_code(code)
    if result is None:
        return JSONResponse({"ok": False, "error": "主进程内部桥不可达"}, status_code=503)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/setup/verify-password")
async def admin_api_setup_verify_password(request: Request, user=Depends(get_current_user)):
    """提交两步验证（2FA）密码。"""
    if not user:
        return _fail("未登录", 401)

    from rss.app.services import bridge_client

    body = await _json_body(request)
    password = str(body.get("password") or "")
    if not password:
        return _fail("请输入两步验证密码")

    result = await bridge_client.verify_password(password)
    if result is None:
        return JSONResponse({"ok": False, "error": "主进程内部桥不可达"}, status_code=503)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/config")
@router.post("/api/setup/config")
async def admin_api_setup_config(request: Request, user=Depends(get_current_user)):
    """保存任意白名单配置项（配置页与向导共用）。"""
    if not user:
        return _fail("未登录", 401)

    body = await _json_body(request)
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        return _fail("没有需要保存的配置")

    from rss.app.services import bridge_client

    # 优先让主进程保存：它写库的同时会同步 os.environ，配置立即生效。
    result = await bridge_client.save_config(values)
    if result is not None:
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    # 桥不可达时退化为本地写库，改动会在主进程重启后生效
    from utils import app_config

    session = get_session()
    try:
        saved, errors = app_config.save(session, values)
    finally:
        session.close()

    if errors:
        return JSONResponse({"ok": False, "saved": saved, "errors": errors}, status_code=400)
    return JSONResponse({"ok": True, "saved": saved, "restart_required": True})
