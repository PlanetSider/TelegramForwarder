"""飞书 / Lark 应用机器人的增删改查。

控制台（Web 进程）和主进程都会用，所以只依赖 ORM 模型，不碰全局 db_ops。
"""

import logging
from typing import Any, Dict, List, Optional

from enums.enums import FeishuPlatform
from models.models import FeishuBot, FeishuWebhook, ForwardRule

logger = logging.getLogger(__name__)

PLATFORMS = [("feishu", "飞书", "open.feishu.cn"), ("lark", "Lark", "open.larksuite.com")]


def _platform(value: Any) -> FeishuPlatform:
    return FeishuPlatform.LARK if str(value).lower() == "lark" else FeishuPlatform.FEISHU


def _mask(secret: Optional[str]) -> str:
    text = (secret or "").strip()
    if not text:
        return "未设置"
    if len(text) <= 8:
        return text[:2] + "****"
    return text[:4] + "****" + text[-2:]


def list_bots(session, platform: Optional[str] = None) -> List[FeishuBot]:
    query = session.query(FeishuBot)
    if platform:
        query = query.filter(FeishuBot.platform == _platform(platform))
    return query.order_by(FeishuBot.is_default.desc(), FeishuBot.id).all()


def bot_rows(session, platform: Optional[str] = None) -> List[Dict[str, Any]]:
    """模板用：昵称为空时显示 App ID。"""
    rows = []
    for bot in list_bots(session, platform):
        used = session.query(ForwardRule).filter(ForwardRule.feishu_bot_id == bot.id).count()
        rows.append(
            {
                "id": bot.id,
                "app_id": bot.app_id,
                "name": (bot.name or "").strip(),
                "label": bot.label(),
                "platform": getattr(bot.platform, "value", str(bot.platform)),
                "is_default": bool(bot.is_default),
                "enable": bool(bot.enable),
                "secret": _mask(bot.app_secret),
                "rule_count": used,
                # 有没有扫码创建者：决定「直接发送」（私聊）这一项可不可用
                "has_owner": bool((getattr(bot, "owner_open_id", "") or "").strip()),
                "owner_name": (getattr(bot, "owner_name", "") or "").strip(),
            }
        )
    return rows


def get_bot(session, bot_id: int) -> Optional[FeishuBot]:
    return session.query(FeishuBot).get(bot_id)


def get_default(session) -> Optional[FeishuBot]:
    """接收命令的那一个：优先 is_default，其次第一个启用的。"""
    bot = (
        session.query(FeishuBot)
        .filter(FeishuBot.is_default == True, FeishuBot.enable == True)  # noqa: E712
        .first()
    )
    if bot:
        return bot
    return (
        session.query(FeishuBot)
        .filter(FeishuBot.enable == True)  # noqa: E712
        .order_by(FeishuBot.id)
        .first()
    )


def _clear_other_defaults(session, keep_id: Optional[int]) -> None:
    query = session.query(FeishuBot)
    if keep_id is not None:
        query = query.filter(FeishuBot.id != keep_id)
    query.update({"is_default": False}, synchronize_session=False)


def add_bot(
    session,
    app_id: str,
    app_secret: str,
    name: Optional[str] = None,
    platform: str = "feishu",
    is_default: bool = False,
) -> tuple:
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id:
        return False, "App ID 不能为空"
    if not app_secret:
        return False, "App Secret 不能为空"
    if session.query(FeishuBot).filter(FeishuBot.app_id == app_id).first():
        return False, "这个 App ID 已经加过了"

    try:
        bot = FeishuBot(
            app_id=app_id,
            app_secret=app_secret,
            name=(name or "").strip() or None,
            platform=_platform(platform),
            is_default=bool(is_default),
            enable=True,
        )
        session.add(bot)
        session.flush()
        if bot.is_default:
            _clear_other_defaults(session, bot.id)
        session.commit()
        return True, f"已添加 {bot.label()}"
    except Exception as exc:
        session.rollback()
        logger.error(f"添加飞书机器人失败: {exc}")
        return False, f"添加失败: {exc}"


def update_bot(
    session,
    bot_id: int,
    name: Optional[str] = None,
    app_secret: Optional[str] = None,
    enable: Optional[bool] = None,
    is_default: Optional[bool] = None,
) -> tuple:
    bot = session.query(FeishuBot).get(bot_id)
    if not bot:
        return False, "机器人不存在"

    try:
        if name is not None:
            bot.name = name.strip() or None
        if app_secret is not None and app_secret.strip():
            bot.app_secret = app_secret.strip()
        if enable is not None:
            bot.enable = bool(enable)
        if is_default is True:
            bot.is_default = True
            _clear_other_defaults(session, bot.id)
        elif is_default is False:
            bot.is_default = False
        session.commit()
        return True, "已保存"
    except Exception as exc:
        session.rollback()
        logger.error(f"更新飞书机器人失败: {exc}")
        return False, f"保存失败: {exc}"


def delete_bot(session, bot_id: int) -> tuple:
    bot = session.query(FeishuBot).get(bot_id)
    if not bot:
        return False, "机器人不存在"

    used = session.query(ForwardRule).filter(ForwardRule.feishu_bot_id == bot_id).count()
    if used:
        return False, f"还有 {used} 条规则在用它，先把这些规则的目标改掉再删除"

    try:
        session.delete(bot)
        session.commit()
        return True, "已删除"
    except Exception as exc:
        session.rollback()
        logger.error(f"删除飞书机器人失败: {exc}")
        return False, f"删除失败: {exc}"


# ------------------------------------------------------------ 群机器人（webhook）

def _mask_url(url: str) -> str:
    text = (url or "").strip()
    if len(text) <= 32:
        return text
    return text[:26] + "..." + text[-6:]


def _normalize_webhook(url: str) -> tuple:
    """校验群机器人地址，返回 (ok, 规范化地址, 错误文案)。"""
    text = (url or "").strip()
    if not text:
        return False, "", "webhook 地址不能为空"
    if not text.lower().startswith("https://"):
        return False, "", "webhook 地址要以 https:// 开头"
    if "/open-apis/bot/v2/hook/" not in text:
        return False, "", "这不是飞书群机器人地址（应含 /open-apis/bot/v2/hook/）"
    return True, text, ""


def list_webhooks(session, enable: Optional[bool] = None) -> List[FeishuWebhook]:
    query = session.query(FeishuWebhook)
    if enable is not None:
        query = query.filter(FeishuWebhook.enable == enable)
    return query.order_by(FeishuWebhook.id).all()


def webhook_rows(session) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for hook in list_webhooks(session):
        rows.append({
            "id": hook.id,
            "name": hook.name or "",
            "label": hook.label(),
            "url": hook.webhook_url,
            "display": _mask_url(hook.webhook_url),
            "has_secret": bool((hook.secret or "").strip()),
            "enable": bool(hook.enable),
            # 必须可 JSON 序列化：模板里要 tojson 给前端用
            "created_at": hook.created_at.isoformat() if hook.created_at else "",
        })
    return rows


def get_webhook(session, hook_id: int) -> Optional[FeishuWebhook]:
    return session.query(FeishuWebhook).get(hook_id)


def add_webhook(session, url: str, name: Optional[str] = None,
                secret: Optional[str] = None, enable: bool = True) -> tuple:
    ok, normalized, error = _normalize_webhook(url)
    if not ok:
        return False, error

    exists = session.query(FeishuWebhook).filter(FeishuWebhook.webhook_url == normalized).first()
    if exists:
        return False, "这个 webhook 已经添加过了"

    hook = FeishuWebhook(
        name=(name or "").strip() or None,
        webhook_url=normalized,
        secret=(secret or "").strip() or None,
        enable=bool(enable),
    )
    session.add(hook)
    try:
        session.commit()
        return True, "已添加群机器人"
    except Exception as exc:
        session.rollback()
        logger.error(f"保存群机器人失败: {exc}")
        return False, f"保存失败: {exc}"


def update_webhook(session, hook_id: int, **kwargs) -> tuple:
    hook = get_webhook(session, hook_id)
    if hook is None:
        return False, "群机器人不存在"

    if "name" in kwargs:
        hook.name = (kwargs.get("name") or "").strip() or None
    if "secret" in kwargs:
        hook.secret = (kwargs.get("secret") or "").strip() or None
    if "enable" in kwargs and kwargs.get("enable") is not None:
        hook.enable = bool(kwargs.get("enable"))
    if kwargs.get("url"):
        ok, normalized, error = _normalize_webhook(kwargs.get("url"))
        if not ok:
            return False, error
        hook.webhook_url = normalized

    try:
        session.commit()
        return True, "已保存"
    except Exception as exc:
        session.rollback()
        logger.error(f"更新群机器人失败: {exc}")
        return False, f"保存失败: {exc}"


def delete_webhook(session, hook_id: int) -> tuple:
    hook = get_webhook(session, hook_id)
    if hook is None:
        return False, "群机器人不存在"

    try:
        session.delete(hook)
        session.commit()
        return True, "已删除"
    except Exception as exc:
        session.rollback()
        logger.error(f"删除群机器人失败: {exc}")
        return False, f"删除失败: {exc}"
