"""规则健康巡检。

发送失败只能发现转发目标的问题。转发源被删、账号注销后不再有新消息，
规则会安静空转，永远等不到一次发送失败 —— 这类问题只能靠巡检发现。

每轮做的事：
  * 取所有「启用且未失效」的规则；
  * 用用户账号依次 get_entity 转发源、转发目标；
  * 命中 rule_health 认定的不可恢复错误就标失效并按 rule_notify 的顺序通知。

刻意只查实体可达性，不发送探测消息：
  * 探测消息会污染目标群，也会消耗发送配额；
  * 目标禁言/权限不足由发送失败时的即时标记覆盖，这里不重复判定，
    避免把「仅管理员可发言、bot 恰好是管理员」的群误判为失效。
  * FloodWait 或网络超时中止整轮，不写任何状态。
"""
import asyncio
import logging
import os
from types import SimpleNamespace

logger = logging.getLogger(__name__)

# 默认 24 小时一轮；可用 RULE_HEALTH_CHECK_HOURS 覆盖（最小 1 小时）
DEFAULT_INTERVAL_HOURS = 24
# 启动后先等这么久再首检，避免和登录、会话同步抢资源
FIRST_DELAY_SECONDS = int(os.getenv("RULE_HEALTH_FIRST_DELAY", "600"))


class RuleHealthChecker:
    def __init__(self, user_client, bot_client=None):
        self.user_client = user_client
        self.bot_client = bot_client
        self.interval_hours = self._read_interval()
        self._task = None

    @staticmethod
    def _read_interval() -> float:
        raw = os.getenv("RULE_HEALTH_CHECK_HOURS", str(DEFAULT_INTERVAL_HOURS))
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            hours = float(DEFAULT_INTERVAL_HOURS)
        return max(1.0, hours)

    async def start(self):
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())
        logger.info("规则健康巡检已启动：每 %.0f 小时一轮", self.interval_hours)

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self):
        await asyncio.sleep(FIRST_DELAY_SECONDS)
        while True:
            try:
                checked, marked = await self.check_once()
                logger.info("规则健康巡检完成：检查 %s 条，标记失效 %s 条", checked, marked)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("规则健康巡检出错")
            await asyncio.sleep(self.interval_hours * 3600)

    async def check_once(self):
        """跑一轮巡检，返回 (检查条数, 新标记失效条数)。"""
        rules = self._load_rules()
        checked = 0
        marked = 0
        for row in rules:
            try:
                reason = await self._probe(row)
            except Exception as exc:
                # FloodWait 等：整轮中止，等下一轮
                if type(exc).__name__ == "FloodWaitError":
                    logger.warning("巡检触发 FloodWait，提前结束本轮: %s", exc)
                    return checked, marked
                logger.debug("巡检规则 #%s 时出错: %s", row["id"], exc)
                continue
            if not reason:
                checked += 1
                continue
            checked += 1
            if self._mark_and_notify(row, reason):
                marked += 1
        return checked, marked

    @staticmethod
    def _load_rules():
        from models.models import get_session, ForwardRule

        session = get_session()
        try:
            rules = session.query(ForwardRule).filter(
                ForwardRule.enable_rule == True,
                ForwardRule.invalid_at.is_(None),
            ).all()
            return [{
                "id": rule.id,
                "source_tg_id": getattr(rule.source_chat, "telegram_chat_id", None) if rule.source_chat else None,
                "target_tg_id": getattr(rule.target_chat, "telegram_chat_id", None) if rule.target_chat else None,
                "feishu_bot_id": getattr(rule, "feishu_bot_id", None),
                "feishu_chat_id": getattr(rule, "feishu_chat_id", None),
            } for rule in rules]
        finally:
            session.close()

    async def _probe(self, row):
        """返回失效原因；可达或不判定时返回 None。"""
        from utils.rule_health import explain_invalid

        if self.user_client is None:
            return None
        checks = (("转发源", row.get("source_tg_id")), ("转发目标", row.get("target_tg_id")))
        for label, raw_id in checks:
            if raw_id in (None, ""):
                continue
            try:
                await asyncio.wait_for(
                    self.user_client.get_entity(int(raw_id)), timeout=15
                )
            except asyncio.TimeoutError:
                # 网络问题：本轮不下结论，避免批量误标
                return None
            except Exception as exc:
                if type(exc).__name__ == "FloodWaitError":
                    raise
                reason = explain_invalid(exc)
                if reason:
                    # 原因文本已经写明是转发源还是转发目标时不再拼前缀
                    if any(key in reason for key in ("转发源", "转发目标", "账号已")):
                        return reason
                    return f"{label}：{reason}"
                # 其他异常（权限、代理）无法确定为失效，放弃这条规则的判定
                logger.debug("巡检规则 #%s 的%s时遇到非失效错误: %s", row["id"], label, exc)
                return None
        return None

    def _mark_and_notify(self, row, reason: str) -> bool:
        from utils.rule_health import mark_rule_invalid

        if not mark_rule_invalid(row["id"], reason):
            return False
        proxy = SimpleNamespace(
            id=row["id"],
            feishu_bot_id=row.get("feishu_bot_id"),
            feishu_chat_id=row.get("feishu_chat_id"),
        )
        try:
            asyncio.create_task(self._notify(proxy, reason))
        except RuntimeError:
            logger.debug("没有运行中的事件循环，跳过失效通知")
        return True

    async def _notify(self, proxy, reason: str):
        from utils.rule_notify import notify_rule_invalid

        try:
            await notify_rule_invalid(proxy, reason, client=self.bot_client,
                                      fallback_client=self.user_client)
        except Exception:
            logger.debug("规则 #%s 的失效通知发送失败", getattr(proxy, "id", 0), exc_info=True)
