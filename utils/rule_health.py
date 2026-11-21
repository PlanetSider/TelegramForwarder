"""转发规则健康：把「校验不过」的错误翻译成可读的失效原因。

只认下面几类不可恢复的错误。FloodWait、网络超时、临时 5xx 都必须当成可重试，
不能把规则永久标失效 —— 否则一次抖动就要人工点恢复。

判定对两处生效：
  * 发送失败时的即时标记：只能发现转发目标的问题；
  * 定时巡检：能同时覆盖转发源。源被删除/注销后不会再有新消息，
    也就永远等不到发送失败，只能靠巡检发现。
"""
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# 异常类名 -> 失效原因。按类名判断，不依赖具体 telethon 版本导出了哪些类。
INVALID_ERRORS = {
    "PeerIdInvalidError": "转发源或转发目标不存在（会话 ID 无效）",
    "ChannelInvalidError": "转发源或转发目标不存在（频道无效）",
    "ChannelPrivateError": "转发源或转发目标已转为私有，账号已不在其中",
    "UserDeactivatedError": "账号已注销（转发源或转发目标）",
    "UserDeactivatedBanError": "账号已被封禁（转发源或转发目标）",
    "ChatWriteForbiddenError": "转发目标不允许发言（全员禁言或权限不足）",
}

# 异常被拼成字符串后只剩文本，退回关键词匹配。顺序有意义：先具体，后笼统。
_TEXT_HINTS = (
    ("chat_write_forbidden", "转发目标不允许发言（全员禁言或权限不足）"),
    ("you're banned from sending messages", "转发目标不允许发言（被禁言）"),
    ("not enough rights to send", "转发目标不允许发言（权限不足）"),
    ("cannot write in this chat", "转发目标不允许发言（禁言或权限不足）"),
    ("channel_private", "转发源或转发目标已转为私有，账号已不在其中"),
    ("the channel specified is private", "转发源或转发目标已转为私有，账号已不在其中"),
    ("channel_invalid", "转发源或转发目标不存在（频道无效）"),
    ("channel is invalid", "转发源或转发目标不存在（频道无效）"),
    ("the channel specified is invalid", "转发源或转发目标不存在（频道无效）"),
    ("invalid channel", "转发源或转发目标不存在（频道无效）"),
    ("peer_id_invalid", "转发源或转发目标不存在（会话 ID 无效）"),
    ("peer id invalid", "转发源或转发目标不存在（会话 ID 无效）"),
    ("user_deactivated_ban", "账号已被封禁（转发源或转发目标）"),
    ("user_deactivated", "账号已注销（转发源或转发目标）"),
    ("the user was deleted", "账号已注销（转发源或转发目标）"),
    ("the user is deleted", "账号已注销（转发源或转发目标）"),
    ("user is deleted", "账号已注销（转发源或转发目标）"),
)

# 明确可重试，绝不当作失效
_RETRYABLE = {"FloodWaitError", "TimeoutError", "ServerError", "ConnectionError",
              "AuthKeyUnregisteredError"}


def explain_invalid(error) -> Optional[str]:
    """返回失效原因；可重试、或与失效无关的错误返回 None。"""
    if error is None:
        return None
    name = type(error).__name__
    if name in _RETRYABLE:
        return None
    if name in INVALID_ERRORS:
        return INVALID_ERRORS[name]
    text = error.lower() if isinstance(error, str) else str(error).lower()
    for hint, reason in _TEXT_HINTS:
        if hint in text:
            return reason
    return None


def explain_context_errors(errors) -> Optional[str]:
    """从过滤器链 context.errors（字符串列表）里找出第一条失效原因。"""
    for item in errors or []:
        reason = explain_invalid(item)
        if reason:
            return reason
    return None


def rule_is_usable(rule) -> bool:
    """转发入口统一判断：启用且未失效。"""
    return bool(getattr(rule, "enable_rule", False)) and getattr(rule, "invalid_at", None) is None


def mark_rule_invalid(rule_id: int, reason: str) -> bool:
    """标记规则失效，返回 True 表示本次是首次标记（调用方据此决定要不要发通知）。

    并发下两条消息同时失败时，后到的会看到 invalid_at 已写，不会重复通知。
    """
    from models.models import get_session, ForwardRule

    session = get_session()
    try:
        rule = session.query(ForwardRule).get(rule_id)
        if rule is None or rule.invalid_at is not None:
            return False
        rule.invalid_at = datetime.utcnow()
        rule.invalid_reason = (reason or "")[:255]
        session.commit()
        logger.warning("规则 #%s 已标记失效: %s", rule_id, reason)
        return True
    except Exception:
        session.rollback()
        logger.debug("标记规则失效失败", exc_info=True)
        return False
    finally:
        session.close()


def clear_rule_invalid(rule_id: int) -> bool:
    """手动恢复：清掉失效标记。"""
    from models.models import get_session, ForwardRule

    session = get_session()
    try:
        rule = session.query(ForwardRule).get(rule_id)
        if rule is None:
            return False
        rule.invalid_at = None
        rule.invalid_reason = None
        session.commit()
        logger.info("规则 #%s 已恢复", rule_id)
        return True
    except Exception:
        session.rollback()
        logger.debug("恢复规则失败", exc_info=True)
        return False
    finally:
        session.close()
