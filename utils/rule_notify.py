"""规则失效通知。

送达顺序：
  1. 规则自己的推送目标（ForwardTarget / 绑定的飞书应用机器人）——
     用户配置这些通道本来就是为了收消息；
  2. 上面全失败、或规则根本没配目标时，私聊管理员（bot 要求管理员先与它
     对话过，失败再用用户账号试）。

通知失败只记日志，绝不能再反过来把规则或转发流程搅乱。
"""
import asyncio
import logging

from models.models import get_session, ForwardRule, ForwardTarget

logger = logging.getLogger(__name__)


def _target_rows(rule_id: int):
    """启用的推送目标：[(id, 名称, 地址)]。"""
    session = get_session()
    try:
        rows = session.query(ForwardTarget).filter(
            ForwardTarget.rule_id == rule_id,
            ForwardTarget.enabled == True,
        ).all()
        return [(row.id, (row.name or "").strip(), (row.address or "").strip()) for row in rows]
    except Exception:
        logger.debug("读取推送目标失败", exc_info=True)
        return []
    finally:
        session.close()


def describe_rule(rule_id: int):
    """（源名称，目标名称）。失效通知需要的两个名字，脱开 ORM session 也能取。"""
    from utils.common import target_display

    source, target = "?", "?"
    session = get_session()
    try:
        rule = session.query(ForwardRule).get(rule_id)
        if rule is not None:
            source = (getattr(rule.source_chat, "name", "") or "") or f"#{rule.source_chat_id}"
            try:
                target = target_display(rule)
            except Exception:
                target = "推送通道"
    except Exception:
        logger.debug("读取规则名称失败", exc_info=True)
    finally:
        session.close()
    return source, target


def build_invalid_text(rule, reason: str) -> str:
    source, target = describe_rule(getattr(rule, "id", 0))
    return "\n".join([
        f"⚠️ 转发规则 #{getattr(rule, 'id', '?')} 已失效",
        f"转发源：{source}",
        f"转发目标：{target}",
        f"原因：{reason}",
        "",
        "该规则已自动停发，不会继续重试。请修复转发源或转发目标后，",
        "在控制台「规则」页点「恢复」重新启用。",
    ])


async def _send_address(address: str, text: str, title: str):
    """按地址类型发送：飞书 / Lark webhook 单独处理，其余交给 apprise。"""
    from utils.feishu import parse_feishu_url, send_feishu_webhook

    feishu_target = parse_feishu_url(address)
    if feishu_target is not None:
        ok, detail = await send_feishu_webhook(feishu_target, text, title=title)
        return bool(ok), detail or ""

    try:
        import apprise
    except Exception as exc:  # pragma: no cover - 依赖缺失时不影响主流程
        return False, f"apprise 不可用: {exc}"

    apobj = apprise.Apprise()
    if not apobj.add(address):
        return False, "推送地址无法识别"
    try:
        ok = await asyncio.to_thread(apobj.notify, body=text, title=title)
        return bool(ok), "" if ok else "推送未成功"
    except Exception as exc:
        return False, str(exc)


async def _send_via_feishu_bot(rule, text: str, title: str):
    """规则绑定了飞书应用机器人时，用机器人发到配置的群或创建者私聊。"""
    from models.models import FeishuBot
    from utils import feishu_app

    session = get_session()
    try:
        bot = session.query(FeishuBot).get(getattr(rule, "feishu_bot_id", None))
        if bot is None:
            return False, "绑定的飞书机器人已删除"
        chat_id = (getattr(rule, "feishu_chat_id", "") or "").strip()
        if chat_id:
            ok, detail = await feishu_app.send_text(bot, chat_id, text, title=title)
        else:
            ok, detail = await feishu_app.send_to_owner(bot, text, title=title)
        return bool(ok), detail or ""
    except Exception as exc:
        return False, str(exc)
    finally:
        session.close()


async def _notify_admin(rule_id: int, text: str, clients) -> bool:
    from utils.common import get_admin_list

    try:
        admin_ids = get_admin_list()
    except Exception:
        logger.warning("规则 #%s 已失效，但没有可用的管理员配置", rule_id)
        return False

    for admin_id in admin_ids:
        for client in clients:
            if client is None:
                continue
            try:
                await client.send_message(admin_id, text)
                logger.info("规则 #%s 的失效通知已发送给管理员 %s", rule_id, admin_id)
                return True
            except Exception as exc:
                logger.debug("通知管理员 %s 失败: %s", admin_id, exc)
    logger.warning("规则 #%s 已失效，但所有通知通道都失败了", rule_id)
    return False


async def notify_rule_invalid(rule, reason: str, client=None, fallback_client=None) -> bool:
    """发送失效通知。返回是否至少送达一处。"""
    rule_id = getattr(rule, "id", 0)
    title = "转发规则失效"
    text = build_invalid_text(rule, reason)
    delivered = False

    for _tid, name, address in _target_rows(rule_id):
        if not address:
            continue
        try:
            ok, detail = await _send_address(address, text, title)
        except Exception as exc:
            ok, detail = False, str(exc)
        if ok:
            delivered = True
        else:
            logger.warning("规则 #%s 的失效通知发送失败（%s）: %s", rule_id, name or "未命名", detail)

    if not delivered and getattr(rule, "feishu_bot_id", None):
        try:
            ok, detail = await _send_via_feishu_bot(rule, text, title)
            delivered = bool(ok)
            if not ok:
                logger.warning("规则 #%s 的失效通知发送失败（飞书应用机器人）: %s", rule_id, detail)
        except Exception as exc:
            logger.warning("规则 #%s 的失效通知发送失败（飞书应用机器人）: %s", rule_id, exc)

    if delivered:
        return True
    return await _notify_admin(rule_id, text, (client, fallback_client))
