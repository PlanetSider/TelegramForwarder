"""总结转发的执行器。

从 summary_tasks 表里读配置，每天到点取来源群最近 24 小时的消息，
交给 AI 总结后按配置的发送目标发出去。
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz

from models.models import FeishuBot, FeishuWebhook, SummaryTask, get_session
from rss.app.services import ai_service
from utils import ai_client, feishu_app
from utils.feishu import parse_feishu_url, send_feishu_webhook

logger = logging.getLogger(__name__)

MAX_MESSAGE_PART_LENGTH = 4096 - 300
MAX_MESSAGES_PER_CHAT = 200
LOOKBACK_HOURS = 24
DEFAULT_SUMMARY_PROMPT = os.getenv(
    "DEFAULT_SUMMARY_PROMPT", "请总结以下频道/群组24小时内的消息。"
)


def _timezone():
    return pytz.timezone(os.getenv("DEFAULT_TIMEZONE", "Asia/Shanghai"))


def _split(text: str, max_length: int = MAX_MESSAGE_PART_LENGTH) -> List[str]:
    """按段落/句子把长文切成多段。"""
    text = (text or "").strip()
    if len(text) <= max_length:
        return [text] if text else []
    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        cut = text.rfind("\n\n", 0, max_length)
        if cut <= 0:
            cut = text.rfind("\n", 0, max_length)
        if cut <= 0:
            cut = text.rfind(" ", 0, max_length)
        if cut <= 0:
            cut = max_length
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [p for p in parts if p]


class SummaryTaskRunner:
    """所有启用中的总结任务共用一个轮询循环，每分钟对表。"""

    def __init__(self, user_client, bot_client):
        self.user_client = user_client
        self.bot_client = bot_client
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_run: Dict[int, str] = {}

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("总结任务调度器已启动")

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("总结任务调度器已停止")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("总结调度轮询出错: %s", exc)
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        now = datetime.now(_timezone())
        stamp = now.strftime("%Y-%m-%d")
        current = now.strftime("%H:%M")

        session = get_session()
        try:
            tasks = session.query(SummaryTask).filter(SummaryTask.enable.is_(True)).all()
            due = []
            for task in tasks:
                if (task.summary_time or "").strip() != current:
                    continue
                if self._last_run.get(task.id) == stamp:
                    continue
                due.append(task.id)
        finally:
            session.close()

        for task_id in due:
            self._last_run[task_id] = stamp
            try:
                await self.run_once(task_id)
            except Exception as exc:
                logger.warning("总结任务 %s 执行失败: %s", task_id, exc)

    async def run_once(self, task_id: int) -> None:
        session = get_session()
        try:
            task = session.query(SummaryTask).get(task_id)
            if task is None:
                return
            try:
                chat_ids = [int(x) for x in json.loads(task.source_chat_ids or "[]")]
            except (TypeError, ValueError):
                chat_ids = []
            target_kind = (task.target_kind or "chat").strip()
            target_value = (task.target_value or "").strip()
            merge_mode = (task.merge_mode or "per_source").strip()
            model = (task.model or "").strip()
            reasoning = (task.reasoning or "").strip()
            prompt = (task.prompt or "").strip() or DEFAULT_SUMMARY_PROMPT
            settings = ai_service.resolve_settings(session, task.ai_provider_id)
            label = task.label()
        finally:
            session.close()

        if not chat_ids:
            logger.info("总结任务 %s 没有来源群，跳过", task_id)
            return

        logger.info("开始执行总结任务 %s（%s），来源 %d 个群", task_id, label, len(chat_ids))

        if merge_mode == "combined":
            body = await self._summarize_many(chat_ids, prompt, model, reasoning, settings)
            if body:
                await self._deliver(target_kind, target_value, body)
        else:
            for chat_id in chat_ids:
                body = await self._summarize_many([chat_id], prompt, model, reasoning, settings)
                if body:
                    await self._deliver(target_kind, target_value, body)

    async def _summarize_many(self, chat_ids: List[int], prompt: str, model: str,
                              reasoning: str, settings: Dict[str, str]) -> str:
        chunks: List[str] = []
        for chat_id in chat_ids:
            text = await self._collect(chat_id)
            if text:
                chunks.append(text)
        if not chunks:
            logger.info("总结任务：来源群最近 %d 小时没有可总结的文本", LOOKBACK_HOURS)
            return ""

        content = prompt + "\n\n" + "\n\n".join(chunks)
        if len(content) > 20000:
            content = content[:20000] + "\n\n（内容过长，已截断）"

        try:
            result = await ai_client.chat_completion(
                [{"role": "user", "content": content}],
                model=model or settings.get("model", ""),
                base_url=settings.get("base_url", ""),
                api_key=settings.get("api_key", ""),
                reasoning=reasoning or settings.get("reasoning", ""),
            )
        except Exception as exc:
            logger.warning("总结任务调用 AI 失败: %s", exc)
            return ""
        return (result or "").strip()

    async def _collect(self, chat_id: int) -> str:
        since = datetime.now(pytz.utc) - timedelta(hours=LOOKBACK_HOURS)
        lines: List[str] = []
        try:
            async for message in self.user_client.iter_messages(
                chat_id, limit=MAX_MESSAGES_PER_CHAT
            ):
                if message.date and message.date < since:
                    break
                text = (getattr(message, "message", "") or "").strip()
                if not text:
                    continue
                sender = ""
                try:
                    sender = (message.sender.name or "") if message.sender else ""
                except Exception:
                    sender = ""
                stamp = message.date.strftime("%H:%M") if message.date else ""
                prefix = " ".join(x for x in (stamp, sender) if x)
                lines.append(f"[{prefix}] {text}" if prefix else text)
        except Exception as exc:
            logger.warning("总结任务读取会话 %s 的消息失败: %s", chat_id, exc)
            return ""
        lines.reverse()
        return "\n".join(lines)

    async def _deliver(self, target_kind: str, target_value: str, body: str) -> None:
        parts = _split(body)
        if not parts:
            return

        if target_kind == "chat":
            try:
                chat_id = int(target_value)
            except (TypeError, ValueError):
                logger.warning("总结任务：Telegram 目标无效 %s", target_value)
                return
            for part in parts:
                try:
                    await self.user_client.send_message(chat_id, part)
                except Exception as exc:
                    logger.warning("总结任务发送到 Telegram %s 失败: %s", chat_id, exc)
            logger.info("总结已发送到 Telegram %s", chat_id)
            return

        if target_kind == "webhook":
            session = get_session()
            try:
                hook = session.query(FeishuWebhook).get(int(target_value)) if target_value.isdigit() else None
                url = (hook.webhook_url if hook else target_value).strip()
                name = hook.label() if hook else "飞书群机器人"
            finally:
                session.close()
            target = parse_feishu_url(url)
            if target is None:
                logger.warning("总结任务：飞书 webhook 地址无效 %s", url)
                return
            for part in parts:
                ok, detail = await send_feishu_webhook(target, part)
                if not ok:
                    logger.warning("总结发送到 %s 失败: %s", name, detail)
            logger.info("总结已发送到 %s", name)
            return

        if target_kind == "bot":
            session = get_session()
            try:
                bot = session.query(FeishuBot).get(int(target_value)) if target_value.isdigit() else None
            finally:
                session.close()
            if bot is None:
                logger.warning("总结任务：飞书应用机器人不存在 %s", target_value)
                return
            for part in parts:
                ok, detail = await feishu_app.send_to_owner(bot, part)
                if not ok:
                    logger.warning("总结发送到飞书应用机器人失败: %s", detail)
            logger.info("总结已发送到飞书应用机器人 %s", feishu_app.bot_label(bot))
            return

        logger.warning("总结任务：未知的发送目标类型 %s", target_kind)

    async def run_all_now(self) -> int:
        """手动触发所有启用任务，返回执行个数。"""
        session = get_session()
        try:
            ids = [t.id for t in session.query(SummaryTask).filter(SummaryTask.enable.is_(True)).all()]
        finally:
            session.close()
        for task_id in ids:
            try:
                await self.run_once(task_id)
            except Exception as exc:
                logger.warning("手动执行总结任务 %s 失败: %s", task_id, exc)
        return len(ids)
