"""飞书 / Lark 应用机器人的增删改查。

控制台（Web 进程）和主进程都会用，所以只依赖 ORM 模型，不碰全局 db_ops。
"""

import logging
from typing import Any, Dict, List, Optional

from enums.enums import FeishuPlatform
from models.models import FeishuBot, ForwardRule

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
