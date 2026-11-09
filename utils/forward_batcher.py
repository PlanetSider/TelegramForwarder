"""把一段时间内收到的多条纯文本消息合并成一条再交给过滤器链。

开启方式：全局设置 → 超时与批量 → 「转发合并间隔（秒）」填大于 0 的数。
0 表示关闭，行为与之前完全一致（每条消息各自转发）。

适用边界：
- 只合并**纯文本**消息。带媒体的消息、媒体组会立刻走原路径，不参与合并。
- 只对「使用机器人」的规则生效；用户账号模式是原生转发，无法合并。
- 处于「编辑原消息」模式的规则不参与合并（合并后无法对应到单条原消息）。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class _AggMessage:
    """伪造 Telethon Message 的最小接口，只实现过滤器链真正访问的字段。"""

    def __init__(self, chat_id: Any, last, text: str):
        self.chat_id = chat_id
        self.id = getattr(last, "id", 0)
        self.text = text
        self.raw_text = text
        self.message = text
        self.date = getattr(last, "date", None)
        self.sender_id = getattr(last, "sender_id", None)
        # 下面这些一律置空：合并后不存在单一媒体/按钮/回复
        self.media = None
        self.photo = None
        self.document = None
        self.video = None
        self.audio = None
        self.voice = None
        self.grouped_id = None
        self.buttons = None
        self.entities = None
        self.reply_to_msg_id = None
        self.sticker = None
        self.gif = None


class _AggEvent:
    """伪造 NewMessage.Event，只实现 get_chat / get_sender 两个异步方法。"""

    def __init__(self, client, chat, message, sender, chat_id):
        self.client = client
        self.chat = chat
        self.message = message
        self.chat_id = chat_id
        self._sender = sender
        self.sender_id = getattr(sender, "id", None)
        self.is_media_group = False

    async def get_chat(self):
        return self.chat

    async def get_sender(self):
        return self._sender


def _sender_label(event) -> str:
    """尽量取一个人话可读的发送者名字。"""
    sender = getattr(event, "sender", None)
    if sender is None:
        try:
            sender = event.message.sender
        except Exception:  # noqa: BLE001
            sender = None
    if sender is not None:
        for attr in ("title", "first_name", "username"):
            value = getattr(sender, attr, None)
            if value:
                return str(value)
    sender_id = getattr(event, "sender_id", None)
    return f"用户{sender_id}" if sender_id else ""


class ForwardBatcher:
    """按 (规则, 会话) 分桶，窗口到期后合并发送。"""

    def __init__(self) -> None:
        self._buckets: Dict[Tuple[int, str], Dict[str, Any]] = {}

    def pending(self, rule_id: int, chat_id: str) -> int:
        bucket = self._buckets.get((rule_id, chat_id))
        return len(bucket["items"]) if bucket else 0

    def pending_total(self) -> int:
        return sum(len(b["items"]) for b in self._buckets.values())

    async def add(self, client, event, chat_id: str, rule, seconds: float,
                  chat=None, sender=None) -> None:
        key = (rule.id, chat_id)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = {"items": [], "task": None, "chat": chat, "sender": sender}
            self._buckets[key] = bucket
        elif chat is not None:
            bucket["chat"] = chat
        elif sender is not None:
            bucket["sender"] = sender

        bucket["items"].append((event, getattr(event.message, "text", "") or ""))
        if sender is not None:
            bucket["sender"] = sender

        if bucket["task"] is None or bucket["task"].done():
            bucket["task"] = asyncio.create_task(self._flush(key, client, rule, seconds))

        logger.info(
            "[规则%s] 消息进入合并队列，当前 %s 条，%.1f 秒后统一发送",
            rule.id, len(bucket["items"]), seconds,
        )

    async def _flush(self, key: Tuple[int, str], client, rule, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return

        bucket = self._buckets.pop(key, None)
        if not bucket or not bucket["items"]:
            return

        items = bucket["items"]
        rule_id, chat_id = key

        lines: List[str] = []
        names_seen: List[str] = []
        for event, text in items:
            text = (text or "").strip()
            if not text:
                continue
            name = _sender_label(event)
            if name and name not in names_seen:
                names_seen.append(name)
            stamp = ""
            date = getattr(event.message, "date", None)
            if date is not None:
                try:
                    stamp = date.strftime("%H:%M")
                except Exception:  # noqa: BLE001
                    stamp = ""
            prefix = ""
            if stamp:
                prefix += f"[{stamp}] "
            if name:
                prefix += f"{name}: "
            lines.append(prefix + text)

        if not lines:
            logger.info("[规则%s] 合并窗口内没有可发送的文本，跳过", rule_id)
            return

        merged = "\n".join(lines)
        logger.info(
            "[规则%s] 合并 %s 条消息（%s 人）→ 一条，共 %s 字",
            rule_id, len(items), len(names_seen) or 1, len(merged),
        )

        last_event = items[-1][0]
        agg_message = _AggMessage(chat_id, last_event.message, merged)
        agg_event = _AggEvent(
            client,
            bucket.get("chat"),
            agg_message,
            bucket.get("sender"),
            chat_id,
        )

        try:
            from filters.process import process_forward_rule
            await process_forward_rule(client, agg_event, chat_id, rule)
        except Exception as exc:  # noqa: BLE001
            logger.error("[规则%s] 合并消息发送失败: %s", rule_id, exc)
            logger.exception(exc)


batcher = ForwardBatcher()
