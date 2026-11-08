"""通道路由：把「这条规则发到哪里」解析成一组明确的目标。

重构前这条链路是隐式的：`target_chat` 是唯一出口，推送只是附着在
Telegram 转发上的附加动作，所以必须靠 `enable_only_push` 把主出口反向关掉。

改成「任意通道」之后，两类目标是平级的：

* Telegram 目标：可选，0 或 1 个（`target_chat_id` 为空表示不发 Telegram）
* 推送目标：0 到 N 个（飞书群机器人 / 飞书应用机器人 / 任意 apprise 地址）

发送侧只关心「有没有 Telegram 目标」，推送侧自己读 `ForwardTarget`。
这样就不需要 `enable_only_push` 来倒转了——目标为空自然就是「只推送」。
"""
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

TELEGRAM = "telegram"
FEISHU_WEBHOOK = "feishu_webhook"
FEISHU_BOT = "feishu_bot"
APPRISE = "apprise"

CHANNEL_LABELS = {
    TELEGRAM: "Telegram",
    FEISHU_WEBHOOK: "飞书群机器人",
    FEISHU_BOT: "飞书应用机器人",
    APPRISE: "推送地址",
}


@dataclass(frozen=True)
class Target:
    """一个具体的投递目标。"""

    kind: str
    label: str
    ref: Any = None

    @property
    def is_telegram(self) -> bool:
        return self.kind == TELEGRAM


def has_telegram_target(rule) -> bool:
    """规则是否配置了 Telegram 目标会话。"""
    return getattr(rule, "target_chat_id", None) is not None


def telegram_target_label(rule) -> str:
    chat = getattr(rule, "target_chat", None)
    name = getattr(chat, "name", None) or getattr(chat, "telegram_chat_id", None)
    return f"Telegram · {name}" if name else "Telegram"


def _rule_forward_targets(rule) -> List[Any]:
    """取规则下的全部推送地址。

    `ForwardRule.delivery_target` 在模型里是 uselist=False（只反射第一条），
    一条规则挂多个地址时必须走 session 查询。
    """
    state = getattr(rule, "_sa_instance_state", None)
    session = getattr(state, "session", None)
    if session is None:
        return []
    try:
        from models.models import ForwardTarget
        return (session.query(ForwardTarget)
                .filter(ForwardTarget.rule_id == rule.id,
                        ForwardTarget.enabled == True)
                .all())
    except Exception:
        logger.debug("读取推送配置失败", exc_info=True)
        return []


def describe_targets(rule) -> List[Target]:
    """把一条规则的投递目标展开成列表，供界面展示与路由判断。"""
    targets: List[Target] = []

    if has_telegram_target(rule):
        targets.append(Target(TELEGRAM, telegram_target_label(rule),
                              getattr(rule, "target_chat_id", None)))

    bot_id = getattr(rule, "feishu_bot_id", None)
    if bot_id:
        targets.append(Target(FEISHU_BOT, "飞书应用机器人", bot_id))

    for push in _rule_forward_targets(rule):
        url = getattr(push, "address", "") or ""
        name = (getattr(push, "name", "") or "").strip()
        kind = APPRISE
        label = name or "推送地址"
        try:
            from utils.feishu import parse_feishu_url
            parsed = parse_feishu_url(url)
        except Exception:
            parsed = None
        if parsed is not None:
            kind = FEISHU_WEBHOOK
            label = name or parsed.label
        targets.append(Target(kind, f"{CHANNEL_LABELS.get(kind, '推送')} · {label}",
                              getattr(push, "id", None)))

    return targets


def describe_summary(rule) -> str:
    """一行摘要，用于列表和日志。"""
    targets = describe_targets(rule)
    if not targets:
        return "未配置目标"
    return " → ".join(t.label for t in targets)


def route_kind(rule) -> str:
    """同群重发 / 跨群转发 / 跨通道。"""
    if not has_telegram_target(rule):
        return "cross_channel"
    source_id = getattr(rule, "source_chat_id", None)
    target_id = getattr(rule, "target_chat_id", None)
    if source_id and source_id == target_id:
        return "same_group"
    has_target = getattr(rule, "target_chat_id", None) is None or getattr(rule, "feishu_bot_id", None)
    return "cross_channel" if has_target else "cross_group"


ROUTE_LABELS = {
    "same_group": "同群重发",
    "cross_group": "跨群转发",
    "cross_channel": "跨通道",
}


def route_label(rule) -> str:
    return ROUTE_LABELS.get(route_kind(rule), "转发")
