# -*- coding: utf-8 -*-
"""总结转发任务的读写层。

一条 SummaryTask 描述：把哪些群的消息，用哪个 AI 供应商和模型、按什么提示词
总结，在什么时间，发到哪里。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from models.models import Chat, FeishuBot, FeishuWebhook, SummaryTask

logger = logging.getLogger(__name__)

TARGET_KINDS = [
    ("chat", "Telegram 会话"),
    ("webhook", "飞书群机器人"),
    ("bot", "飞书应用机器人"),
]

MERGE_MODES = [
    ("per_source", "每个群单独一条总结"),
    ("combined", "多个群合并成一条总结"),
]


def _parse_ids(raw: Optional[str]) -> List[int]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    result = []
    for item in data if isinstance(data, list) else []:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _dump_ids(values) -> str:
    ids = []
    for item in values or []:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return json.dumps(sorted(set(ids)))


def _chat_name(chat) -> str:
    return (getattr(chat, "name", None) or str(getattr(chat, "telegram_chat_id", "")) or "未知会话").strip()


def source_chats(session, task: SummaryTask) -> List[Chat]:
    ids = _parse_ids(task.source_chat_ids)
    if not ids:
        return []
    chats = session.query(Chat).filter(Chat.id.in_(ids)).all()
    order = {cid: i for i, cid in enumerate(ids)}
    return sorted(chats, key=lambda c: order.get(c.id, 999))


def _target_label(session, task: SummaryTask) -> str:
    value = (task.target_value or "").strip()
    if not value:
        return "未设置"
    if task.target_kind == "webhook":
        hook = session.query(FeishuWebhook).filter(FeishuWebhook.id == int(value)).first() if value.isdigit() else None
        return f"飞书群机器人 · {hook.label()}" if hook else f"飞书群机器人 #{value}"
    if task.target_kind == "bot":
        bot = session.query(FeishuBot).filter(FeishuBot.id == int(value)).first() if value.isdigit() else None
        return f"飞书应用机器人 · {bot.label()}" if bot else f"飞书应用机器人 #{value}"
    chat = session.query(Chat).filter(Chat.id == int(value)).first() if value.isdigit() else None
    return f"Telegram · {_chat_name(chat)}" if chat else f"Telegram #{value}"


def to_row(session, task: SummaryTask) -> Dict[str, Any]:
    chats = source_chats(session, task)
    return {
        "id": task.id,
        "name": task.label(),
        "sources": [{"id": c.id, "name": _chat_name(c)} for c in chats],
        "source_count": len(chats),
        "ai_provider_id": task.ai_provider_id or None,
        "model": (task.model or "").strip(),
        "reasoning": (task.reasoning or "").strip(),
        "summary_time": (task.summary_time or "07:00").strip(),
        "prompt": (task.prompt or "").strip(),
        "target_kind": (task.target_kind or "chat").strip(),
        "target_value": (task.target_value or "").strip(),
        "target_label": _target_label(session, task),
        "merge_mode": (task.merge_mode or "per_source").strip(),
        "enable": bool(task.enable),
    }


def rows(session) -> List[Dict[str, Any]]:
    tasks = session.query(SummaryTask).order_by(SummaryTask.id.asc()).all()
    return [to_row(session, t) for t in tasks]


def get(session, task_id: int) -> Optional[SummaryTask]:
    try:
        return session.query(SummaryTask).filter(SummaryTask.id == int(task_id)).first()
    except (TypeError, ValueError):
        return None


def create(session, **kwargs) -> SummaryTask:
    task = SummaryTask(
        name=(kwargs.get("name") or "").strip() or None,
        source_chat_ids=_dump_ids(kwargs.get("source_chat_ids")),
        ai_provider_id=_as_int(kwargs.get("ai_provider_id")),
        model=(kwargs.get("model") or "").strip() or None,
        reasoning=(kwargs.get("reasoning") or "").strip() or None,
        summary_time=(kwargs.get("summary_time") or "07:00").strip(),
        prompt=(kwargs.get("prompt") or "").strip() or None,
        target_kind=(kwargs.get("target_kind") or "chat").strip(),
        target_value=(kwargs.get("target_value") or "").strip() or None,
        merge_mode=(kwargs.get("merge_mode") or "per_source").strip(),
        enable=bool(kwargs.get("enable", True)),
    )
    session.add(task)
    session.commit()
    return task


def update(session, task_id: int, **kwargs) -> Optional[SummaryTask]:
    task = get(session, task_id)
    if not task:
        return None
    if "name" in kwargs:
        task.name = (kwargs.get("name") or "").strip() or None
    if "source_chat_ids" in kwargs:
        task.source_chat_ids = _dump_ids(kwargs.get("source_chat_ids"))
    if "ai_provider_id" in kwargs:
        task.ai_provider_id = _as_int(kwargs.get("ai_provider_id"))
    if "model" in kwargs:
        task.model = (kwargs.get("model") or "").strip() or None
    if "reasoning" in kwargs:
        task.reasoning = (kwargs.get("reasoning") or "").strip() or None
    if "summary_time" in kwargs:
        value = (kwargs.get("summary_time") or "").strip()
        task.summary_time = value or "07:00"
    if "prompt" in kwargs:
        task.prompt = (kwargs.get("prompt") or "").strip() or None
    if "target_kind" in kwargs:
        task.target_kind = (kwargs.get("target_kind") or "chat").strip()
    if "target_value" in kwargs:
        task.target_value = (kwargs.get("target_value") or "").strip() or None
    if "merge_mode" in kwargs:
        task.merge_mode = (kwargs.get("merge_mode") or "per_source").strip()
    if "enable" in kwargs:
        task.enable = bool(kwargs.get("enable"))
    session.commit()
    return task


def toggle(session, task_id: int) -> Optional[SummaryTask]:
    task = get(session, task_id)
    if not task:
        return None
    task.enable = not bool(task.enable)
    session.commit()
    return task


def delete(session, task_id: int) -> bool:
    task = get(session, task_id)
    if not task:
        return False
    session.delete(task)
    session.commit()
    return True


def _as_int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def options(session) -> Dict[str, Any]:
    """给前端下拉框用的候选集。"""
    chats = session.query(Chat).all()
    hooks = session.query(FeishuWebhook).all()
    bots = session.query(FeishuBot).all()
    return {
        "chats": [{"id": c.id, "name": _chat_name(c)} for c in chats],
        "webhooks": [{"id": h.id, "name": h.label()} for h in hooks],
        "bots": [{"id": b.id, "name": b.label()} for b in bots],
        "target_kinds": [{"value": k, "label": label} for k, label in TARGET_KINDS],
        "merge_modes": [{"value": v, "label": label} for v, label in MERGE_MODES],
    }
