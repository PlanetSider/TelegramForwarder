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
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from models.models import get_session
from rss.app.routes.auth import get_current_user
from rss.app.services import rule_service
from utils.log_buffer import get_recent
from utils import ai_client, app_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="rss/app/templates")


# ---------------------------------------------------------------- 辅助

def _login_redirect():
    return RedirectResponse(url="/login", status_code=302)


def _ctx(request: Request, user, active: str, **extra):
    """统一模板上下文。"""
    from utils import app_config

    data = {
        "request": request,
        "user": user,
        "active": active,
        "project_name": "TG Forwarder",
        # 侧边栏「全局设置」下的二级菜单：Telegram / 飞书 已提升为「通道设置」一级入口
        "settings_groups": [(gid, title) for gid, title, _ in app_config.GROUPS
                            if gid not in ("telegram", "feishu", "ai")],
        "settings_group": "",
        # 侧边栏「通道设置」里飞书 / Lark 是两个页面，靠它判断高亮
        "platform": "",
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
        channels = rule_service.list_feishu_channels(session)
    finally:
        session.close()

    # 按品牌分组：飞书与 Lark 各自统计，0 或 1 个时只占一行状态位
    channel_groups: Dict[str, List[Dict[str, Any]]] = {"feishu": [], "lark": []}
    for item in channels:
        channel_groups.setdefault(item["brand"], []).append(item)

    # Telegram / 飞书应用机器人的状态只有主进程知道，经内部桥只读获取；不可达时为 None
    from rss.app.services import bridge_client
    status = await bridge_client.get_status()

    return templates.TemplateResponse(
        "admin_dashboard.html",
        _ctx(request, user, "dashboard", overview=overview, health=health,
             telegram=status, channel_groups=channel_groups),
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

    from rss.app.services import ai_service

    session = get_session()
    try:
        rule = rule_service.get_rule(session, rule_id)
        siblings = rule_service.list_rules(session)
        providers = ai_service.provider_rows(session)
    finally:
        session.close()

    if not rule:
        return RedirectResponse(url="/admin/rules", status_code=302)

    return templates.TemplateResponse(
        "admin_rule_detail.html",
        _ctx(request, user, "rules", rule=rule, siblings=siblings,
             providers=providers),
    )


@router.get("/settings", response_class=HTMLResponse)
async def admin_settings_root(user=Depends(get_current_user)):
    """全局设置：默认进入第一个分组，避免把所有配置堆在一屏。"""
    if not user:
        return _login_redirect()

    from utils import app_config
    return RedirectResponse(url=f"/admin/settings/{app_config.GROUPS[0][0]}", status_code=302)


@router.get("/settings/{group}", response_class=HTMLResponse)
async def admin_settings(request: Request, group: str, user=Depends(get_current_user)):
    """全局设置：一个分组一页。"""
    if not user:
        return _login_redirect()

    from utils import app_config
    from utils import settings as settings_loader

    entries = app_config.grouped()
    current = next((entry for entry in entries if entry[0] == group), None)
    if current is None:
        return RedirectResponse(url=f"/admin/settings/{app_config.GROUPS[0][0]}", status_code=302)

    providers = []
    session = get_session()
    try:
        rows = app_config.as_display(session)
        if group == "behavior":
            from rss.app.services import ai_service
            providers = ai_service.provider_rows(session)
    finally:
        session.close()

    # 只把当前分组的展示行交给模板：敏感项已打码，真实值不会进 HTML
    view = {row["key"]: row for row in rows if row.get("group") == group}

    ai_models = {}
    if group == "ai":
        try:
            ai_models = settings_loader.load_ai_models(type="dict")
        except Exception as exc:
            logger.warning("加载 AI 模型配置失败: %s", exc)

    configs = []
    if group in ("ai", "storage"):
        from rss.app.services import config_service
        try:
            configs = config_service.list_configs()
        except Exception as exc:
            logger.warning("读取配置文件失败: %s", exc)

    return templates.TemplateResponse(
        "admin_settings.html",
        _ctx(request, user, "settings",
             settings_group=group,
             groups=entries,
             current=current,
             view=view,
             ai_models=ai_models,
             providers=providers,
             configs=configs),
    )


DIALOG_KINDS = [
    ("group", "群组", "超级群与普通群"),
    ("channel", "频道", "订阅频道"),
    ("bot", "通知机器人", "机器人会话"),
]


@router.get("/dialogs")
async def admin_dialogs_root(user=Depends(get_current_user)):
    if not user:
        return _login_redirect()
    return RedirectResponse(url="/admin/dialogs/group", status_code=302)


@router.get("/dialogs/{kind}")
async def admin_dialogs(request: Request, kind: str, user=Depends(get_current_user)):
    if not user:
        return _login_redirect()

    valid = {k for k, _, _ in DIALOG_KINDS}
    if kind not in valid:
        return RedirectResponse(url="/admin/dialogs/group", status_code=302)

    from models.models import get_session
    from rss.app.services import bridge_client, feishu_service

    data = await bridge_client.get_dialogs() or {}
    all_items = data.get("items") or []
    items = [d for d in all_items if d.get("kind") == kind]
    counts = {k: sum(1 for d in all_items if d.get("kind") == k) for k in valid}

    # 弹窗里要能选「飞书应用机器人」和「飞书群机器人」，所以把两者一并带上
    session = get_session()
    try:
        bots = feishu_service.bot_rows(session)
        hooks = feishu_service.webhook_rows(session)
    finally:
        session.close()

    return templates.TemplateResponse("admin_dialogs.html", _ctx(
        request, user, "dialogs",
        kind=kind, kinds=DIALOG_KINDS, items=items, counts=counts,
        reachable=bool(data), all_items=all_items, bots=bots, hooks=hooks,
    ))


@router.get("/feishu", response_class=HTMLResponse)
async def admin_feishu_root(user=Depends(get_current_user)):
    """飞书通道入口。"""
    if not user:
        return _login_redirect()
    return RedirectResponse(url="/admin/feishu/feishu", status_code=302)


@router.get("/feishu/{platform}", response_class=HTMLResponse)
async def admin_feishu(request: Request, platform: str, user=Depends(get_current_user)):
    """飞书通道管理页：应用机器人 + 群机器人。

    早先按平台拆成「飞书 / Lark」两个页面，现在统一只做飞书；
    旧书签访问 /admin/feishu/lark 会跳回来。
    """
    if not user:
        return _login_redirect()
    if platform != "feishu":
        return RedirectResponse(url="/admin/feishu/feishu", status_code=302)

    from rss.app.services import feishu_service

    session = get_session()
    try:
        rows = app_config.as_display(session)
        bots = feishu_service.bot_rows(session, "feishu")
        webhooks = feishu_service.webhook_rows(session)
        targets = rule_service.list_feishu_channels(session)
    finally:
        session.close()

    view = {row["key"]: row for row in rows if row.get("key") == "FEISHU_ALLOWED_USERS"}

    return templates.TemplateResponse(
        "admin_feishu.html",
        _ctx(
            request,
            user,
            "feishu",
            view=view,
            bots=bots,
            webhooks=webhooks,
            platform="feishu",
            platform_label="飞书",
            platform_domain="open.feishu.cn",
            targets=targets,
        ),
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

    # 飞书相关字段必须一并透传给主进程，否则推送目标会被丢弃，
    # 规则会退化成「源 → 源」的 Telegram 自绑规则。
    payload = {"source": source, "target": target}
    for key in ("feishu_bot_id", "feishu_chat_id", "feishu_webhook_id"):
        value = body.get(key)
        if value not in (None, ""):
            payload[key] = value

    try:
        async with bridge_client._new_client(20.0) as client:  # noqa: SLF001
            resp = await client.post(
                f"{bridge_client._BASE}/internal/rules",  # noqa: SLF001
                headers={"X-Bridge-Token": bridge_client._token() or ""},  # noqa: SLF001
                json=payload,
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


@router.delete("/api/rules/{rule_id}")
async def admin_api_delete_rule(rule_id: int, user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)

    session = get_session()
    try:
        removed = rule_service.delete_rule(session, rule_id)
        if not removed:
            return _fail("规则不存在", 404)
        return JSONResponse({"ok": True, "deleted": rule_id})
    except Exception as exc:
        logger.exception("删除规则失败 rule=%s", rule_id)
        return _fail(f"删除失败: {exc}", 500)
    finally:
        session.close()


# ---------------------------------------------------------------- 转发目标

@router.post("/api/rules/{rule_id}/targets")
async def admin_api_add_target(rule_id: int, request: Request,
                             user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)

    body = await _json_body(request)
    url = (body.get("url") or "").strip()
    mode = body.get("media_send_mode") or "Single"
    name = (body.get("name") or "").strip() or None

    # 先做格式校验，避免把无法使用的地址写进库
    try:
        from utils.feishu import parse_feishu_url
        import apprise
        if parse_feishu_url(url) is None:
            if not apprise.Apprise().add(url):
                return _fail("无法识别的转发目标地址（支持 Apprise URL 或飞书/Lark webhook）")
    except ImportError:
        pass  # 依赖缺失时交给运行时判断，不阻断保存
    except Exception as exc:
        return _fail(f"地址校验失败: {exc}")

    session = get_session()
    try:
        item = rule_service.add_delivery_target(session, rule_id, url, mode, name=name)
        return JSONResponse({"ok": True, "item": item})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:
        logger.exception("新增转发目标失败 rule=%s", rule_id)
        return _fail(f"保存失败: {exc}", 500)
    finally:
        session.close()


@router.post("/api/targets/{target_id}/toggle")
async def admin_api_toggle_target(target_id: int, request: Request,
                                user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    body = await _json_body(request)
    session = get_session()
    try:
        value = rule_service.toggle_delivery_target(session, target_id, bool(body.get("enabled")))
        return JSONResponse({"ok": True, "enabled": value})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except Exception as exc:
        return _fail(f"操作失败: {exc}", 500)
    finally:
        session.close()


@router.delete("/api/targets/{target_id}")
async def admin_api_delete_target(target_id: int, user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    session = get_session()
    try:
        rule_service.delete_delivery_target(session, target_id)
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

@router.get("/ai", response_class=HTMLResponse)
async def admin_ai(request: Request, user=Depends(get_current_user)):
    """AI 服务：多套 OpenAI 兼容供应商。"""
    if not user:
        return _login_redirect()

    from rss.app.services import ai_service

    session = get_session()
    try:
        providers = ai_service.provider_rows(session)
    finally:
        session.close()

    return templates.TemplateResponse(
        "admin_ai.html",
        _ctx(request, user, "ai", providers=providers,
             reasoning_options=ai_service.REASONING_OPTIONS),
    )


@router.get("/api/ai/providers")
async def admin_api_ai_providers(user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    from rss.app.services import ai_service
    session = get_session()
    try:
        return JSONResponse({"ok": True, "providers": ai_service.provider_rows(session)})
    finally:
        session.close()


@router.post("/api/ai/providers")
async def admin_api_ai_provider_add(request: Request, user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    from rss.app.services import ai_service
    body = await _json_body(request)
    session = get_session()
    try:
        provider = ai_service.add_provider(
            session,
            name=body.get("name") or "",
            base_url=body.get("base_url") or "",
            api_key=body.get("api_key") or "",
            model=body.get("model") or "",
            reasoning=body.get("reasoning") or "",
            is_default=bool(body.get("is_default")),
        )
        return JSONResponse({"ok": True, "id": provider.id})
    except ValueError as exc:
        return _fail(str(exc), 400)
    except Exception as exc:
        logger.exception("新增 AI 供应商失败")
        return _fail(f"保存失败: {exc}", 500)
    finally:
        session.close()


@router.post("/api/ai/providers/{provider_id}")
async def admin_api_ai_provider_update(provider_id: int, request: Request,
                                       user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    from rss.app.services import ai_service
    body = await _json_body(request)
    session = get_session()
    try:
        ai_service.update_provider(session, provider_id, **body)
        return JSONResponse({"ok": True})
    except LookupError as exc:
        return _fail(str(exc), 404)
    except Exception as exc:
        logger.exception("更新 AI 供应商失败")
        return _fail(f"保存失败: {exc}", 500)
    finally:
        session.close()


@router.delete("/api/ai/providers/{provider_id}")
async def admin_api_ai_provider_delete(provider_id: int,
                                       user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    from rss.app.services import ai_service
    session = get_session()
    try:
        ai_service.delete_provider(session, provider_id)
        return JSONResponse({"ok": True})
    except LookupError as exc:
        return _fail(str(exc), 404)
    finally:
        session.close()


@router.post("/api/ai/providers/{provider_id}/default")
async def admin_api_ai_provider_default(provider_id: int,
                                        user=Depends(get_current_user)):
    if not user:
        return _fail("未登录", 401)
    from rss.app.services import ai_service
    session = get_session()
    try:
        ai_service.set_default(session, provider_id)
        return JSONResponse({"ok": True})
    except LookupError as exc:
        return _fail(str(exc), 404)
    finally:
        session.close()


@router.post("/api/ai/models")
async def admin_api_ai_models(request: Request, user=Depends(get_current_user)):
    """按当前地址与密钥读取可用模型列表（也接受临时传入，便于先测后存）。"""
    if not user:
        return _fail("请先登录", 401)

    body = await _json_body(request)
    session = get_session()
    try:
        cfg = app_config.get_current(session)
    finally:
        session.close()

    base_url = (body.get("base_url") or cfg.get("AI_BASE_URL") or "").strip()
    api_key = (body.get("api_key") or cfg.get("AI_API_KEY") or "").strip()

    try:
        models = await ai_client.list_models(base_url, api_key)
    except Exception as exc:
        return _fail(f"获取模型失败：{exc}", 502)

    return {"ok": True, "models": models,
            "base_url": ai_client.normalize_base_url(base_url)}


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


# ---------------------------------------------------------------- 飞书机器人

@router.post("/api/feishu/bots")
async def admin_api_add_feishu_bot(request: Request, user=Depends(get_current_user)):
    """新增一个应用机器人。"""
    if not user:
        return _fail("请先登录", 401)

    from rss.app.services import feishu_service

    body = await _json_body(request)
    session = get_session()
    try:
        ok, message = feishu_service.add_bot(
            session,
            app_id=body.get("app_id"),
            app_secret=body.get("app_secret"),
            name=body.get("name"),
            platform=body.get("platform") or "feishu",
            is_default=bool(body.get("is_default")),
        )
    finally:
        session.close()
    if not ok:
        return _fail(message)
    return {"ok": True, "message": message}


@router.post("/api/feishu/bots/{bot_id}")
async def admin_api_update_feishu_bot(bot_id: int, request: Request,
                                      user=Depends(get_current_user)):
    """改昵称 / 换密钥 / 启停 / 设为默认 / 补创建者 open_id。"""
    if not user:
        return _fail("请先登录", 401)

    from rss.app.services import feishu_service

    body = await _json_body(request)
    kwargs = {}
    for key in ("name", "app_secret", "enable", "is_default", "owner_open_id"):
        if key in body:
            kwargs[key] = body.get(key)

    session = get_session()
    try:
        ok, message = feishu_service.update_bot(session, bot_id, **kwargs)
    finally:
        session.close()
    if not ok:
        return _fail(message)
    return {"ok": True, "message": message}


@router.delete("/api/feishu/bots/{bot_id}")
async def admin_api_delete_feishu_bot(bot_id: int, user=Depends(get_current_user)):
    if not user:
        return _fail("请先登录", 401)

    from rss.app.services import feishu_service

    session = get_session()
    try:
        ok, message = feishu_service.delete_bot(session, bot_id)
    finally:
        session.close()
    if not ok:
        return _fail(message)
    return {"ok": True, "message": message}


@router.get("/api/feishu/bots/{bot_id}/chats")
async def admin_api_feishu_chats(bot_id: int, user=Depends(get_current_user)):
    """列出机器人所在的群，供「对话列表 → 转发」选目标。"""
    if not user:
        return _fail("请先登录", 401)

    from rss.app.services import feishu_service
    from utils import feishu_app

    session = get_session()
    try:
        bot = feishu_service.get_bot(session, bot_id)
        if not bot:
            return _fail("机器人不存在", 404)
        chats = await feishu_app.list_chats(bot)
        label = bot.label()
        platform = getattr(bot.platform, "value", "feishu")
    finally:
        session.close()

    return {"ok": True, "label": label, "platform": platform, "chats": chats}


# --------------------------------------------------- 飞书机器人扫码注册

def _qr_svg(text: str) -> str:
    """把二维码画成内联 SVG；没装 qrcode 就返回空串，前端回落成链接。"""
    try:
        import io

        import qrcode
        import qrcode.image.svg
    except Exception:
        return ""
    try:
        image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage)
        buffer = io.BytesIO()
        image.save(buffer)
        return buffer.getvalue().decode("utf-8")
    except Exception as exc:
        logger.warning("二维码生成失败: %s", exc)
        return ""


@router.post("/api/feishu/register/start")
async def admin_api_feishu_register_start(request: Request,
                                          user=Depends(get_current_user)):
    """生成扫码注册会话，返回二维码。"""
    if not user:
        return _fail("请先登录", 401)

    from utils import feishu_register

    body = await _json_body(request)
    platform = (body.get("platform") or "feishu").strip().lower()
    if platform not in ("feishu", "lark"):
        platform = "feishu"

    result = await feishu_register.start(platform)
    if not result.get("ok"):
        return _fail(result.get("error") or "生成二维码失败", 502)

    result["svg"] = _qr_svg(result["url"])
    return result


@router.get("/api/feishu/register/poll")
async def admin_api_feishu_register_poll(session_id: str,
                                         user=Depends(get_current_user)):
    """查询扫码进度；拿到凭据就顺手存进机器人列表。"""
    if not user:
        return _fail("请先登录", 401)

    from rss.app.services import feishu_service
    from utils import feishu_register

    result = await feishu_register.poll(session_id)
    if result.get("status") != "complete":
        return result

    session = get_session()
    try:
        has_default = feishu_service.get_default(session) is not None
        ok, message = feishu_service.add_bot(
            session,
            app_id=result["app_id"],
            app_secret=result["app_secret"],
            name=None,
            platform=result.get("platform") or "feishu",
            is_default=not has_default,
        )
    finally:
        session.close()

    return {**result, "saved": ok, "message": message}


# ------------------------------------------------------ 群机器人（webhook）

@router.post("/api/feishu/webhooks")
async def admin_api_add_feishu_webhook(request: Request, user=Depends(get_current_user)):
    """添加一个群自定义机器人（只能发，不能收）。"""
    if not user:
        return _fail("请先登录", 401)

    from rss.app.services import feishu_service

    body = await _json_body(request)
    session = get_session()
    try:
        ok, message = feishu_service.add_webhook(
            session,
            url=body.get("url"),
            name=body.get("name"),
            secret=body.get("secret"),
            enable=bool(body.get("enable", True)),
        )
    finally:
        session.close()
    if not ok:
        return _fail(message)
    return {"ok": True, "message": message}


@router.post("/api/feishu/webhooks/{hook_id}")
async def admin_api_update_feishu_webhook(hook_id: int, request: Request,
                                          user=Depends(get_current_user)):
    if not user:
        return _fail("请先登录", 401)

    from rss.app.services import feishu_service

    body = await _json_body(request)
    kwargs = {}
    for key in ("name", "secret", "enable", "url"):
        if key in body:
            kwargs[key] = body.get(key)

    session = get_session()
    try:
        ok, message = feishu_service.update_webhook(session, hook_id, **kwargs)
    finally:
        session.close()
    if not ok:
        return _fail(message)
    return {"ok": True, "message": message}


@router.delete("/api/feishu/webhooks/{hook_id}")
async def admin_api_delete_feishu_webhook(hook_id: int, user=Depends(get_current_user)):
    if not user:
        return _fail("请先登录", 401)

    from rss.app.services import feishu_service

    session = get_session()
    try:
        ok, message = feishu_service.delete_webhook(session, hook_id)
    finally:
        session.close()
    if not ok:
        return _fail(message)
    return {"ok": True, "message": message}


# ------------------------------------------------------------ 账号设置

@router.post("/api/account")
async def admin_api_account(request: Request, user=Depends(get_current_user)):
    """改登录用户名 / 密码。改任何一项都要求验证当前密码。"""
    if not user:
        return _fail("请先登录", 401)

    from models.models import User
    from utils.password_policy import validate_password
    from werkzeug.security import check_password_hash, generate_password_hash

    body = await _json_body(request)
    current_password = body.get("current_password") or ""
    new_username = (body.get("username") or "").strip()
    new_password = body.get("new_password") or ""
    confirm_password = body.get("confirm_password") or ""

    relogin = False
    session = get_session()
    try:
        account = session.query(User).filter(User.id == user.id).first()
        if account is None:
            return _fail("账号不存在", 404)
        if not check_password_hash(account.password, current_password):
            return _fail("当前密码不正确")

        changed = []

        if new_username and new_username != account.username:
            if len(new_username) < 3:
                return _fail("用户名至少 3 个字符")
            taken = session.query(User).filter(User.username == new_username).first()
            if taken is not None:
                return _fail("这个用户名已经被占用了")
            account.username = new_username
            changed.append("用户名")
            relogin = True

        if new_password:
            if new_password != confirm_password:
                return _fail("两次输入的新密码不一致")
            rule_ok, rule_error = validate_password(new_password)
            if not rule_ok:
                return _fail(rule_error)
            account.password = generate_password_hash(new_password)
            changed.append("密码")

        if not changed:
            return _fail("没有需要修改的内容")

        session.commit()
    except Exception as exc:
        session.rollback()
        logger.exception("更新账号失败")
        return _fail(f"保存失败：{exc}", 500)
    finally:
        session.close()

    return {"ok": True, "message": "已更新" + "和".join(changed), "relogin": relogin}


@router.get("/account", response_class=HTMLResponse)
async def admin_account(request: Request, user=Depends(get_current_user)):
    """WebUI 登录账号与密码设置。"""
    if not user:
        return _login_redirect()

    from utils.password_policy import describe as describe_password_rule

    return templates.TemplateResponse(
        "admin_account.html",
        _ctx(request, user, "account",
             account=user,
             password_rule=describe_password_rule()),
    )
