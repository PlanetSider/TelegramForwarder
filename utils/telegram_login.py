"""主进程内的 Telegram 登录状态机。

为什么必须在主进程：`phone_code_hash` 保存在 Telethon 客户端实例里，
发码与验码必须是同一个 client、同一个进程，否则签到必然失败。
WebUI 跑在 RSS 子进程，只能通过 `utils.main_bridge` 的 HTTP 接口反向调用这里。

流程与 Telethon 的非交互登录一致：

    connect() → is_user_authorized()?
        ├─ 是：直接取 USER_ID，结束
        └─ 否：send_code_request(phone) → 用户在浏览器填验证码
                 sign_in(phone, code)
                   ├─ SessionPasswordNeededError → 还需 2FA 密码
                   └─ 成功：取 USER_ID，断开临时客户端

成功后 `./sessions/user` 里已经是有效凭据，正式客户端直接 `start()` 不会再要验证码。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SESSION_PATH = "./sessions/user"

STATUS_IDLE = "idle"
STATUS_CODE_SENT = "code_sent"
STATUS_NEED_PASSWORD = "need_password"
STATUS_AUTHORIZED = "authorized"
STATUS_ERROR = "error"


class TelegramLoginManager:
    """一次只处理一个登录会话；重复调用 begin 会先丢弃上一个。"""

    def __init__(self) -> None:
        self._client = None
        self._phone: Optional[str] = None
        self.status = STATUS_IDLE
        self.error = ""
        self.user_id: Optional[int] = None

    # ---------------------------------------------------------------- 状态

    def snapshot(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "user_id": self.user_id,
            "phone": self._phone,
        }

    def reset(self) -> None:
        self.status = STATUS_IDLE
        self.error = ""
        self.user_id = None
        self._phone = None

    # ---------------------------------------------------------------- 动作

    async def abort(self) -> None:
        """断开临时客户端（失败也不抛）。"""
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("断开登录客户端失败", exc_info=True)

    async def begin(self, api_id: str, api_hash: str, phone: str) -> Dict[str, Any]:
        """提交凭据并发送验证码。"""
        from telethon import TelegramClient

        await self.abort()
        self.reset()

        try:
            api_id_int = int(str(api_id).strip())
        except (TypeError, ValueError):
            self.status = STATUS_ERROR
            self.error = "API_ID 必须是数字"
            return self.snapshot()

        self._phone = (phone or "").strip()
        if not self._phone:
            self.status = STATUS_ERROR
            self.error = "手机号不能为空"
            return self.snapshot()

        os.makedirs(os.path.dirname(SESSION_PATH) or ".", exist_ok=True)

        try:
            self._client = TelegramClient(SESSION_PATH, api_id_int, str(api_hash).strip())
            await self._client.connect()

            # 会话文件里可能已有有效登录（比如重启后重新走一遍向导）
            if await self._client.is_user_authorized():
                return await self._finish()

            await self._client.send_code_request(self._phone)
            self.status = STATUS_CODE_SENT
            return self.snapshot()

        except Exception as exc:
            logger.warning("发送验证码失败: %s", exc)
            self.status = STATUS_ERROR
            self.error = self._explain(exc)
            await self.abort()
            return self.snapshot()

    async def submit_code(self, code: str) -> Dict[str, Any]:
        """提交短信/App 内验证码。"""
        if self._client is None:
            self.status = STATUS_ERROR
            self.error = "登录会话已失效，请重新发送验证码"
            return self.snapshot()

        try:
            await self._client.sign_in(phone=self._phone, code=(code or "").strip())
        except Exception as exc:
            from telethon.errors import SessionPasswordNeededError

            if isinstance(exc, SessionPasswordNeededError):
                self.status = STATUS_NEED_PASSWORD
                self.error = ""
                return self.snapshot()

            logger.warning("验证码校验失败: %s", exc)
            self.status = STATUS_CODE_SENT
            self.error = self._explain(exc)
            return self.snapshot()

        return await self._finish()

    async def submit_password(self, password: str) -> Dict[str, Any]:
        """提交两步验证（2FA）密码。"""
        if self._client is None:
            self.status = STATUS_ERROR
            self.error = "登录会话已失效，请重新发送验证码"
            return self.snapshot()

        try:
            await self._client.sign_in(password=(password or ""))
        except Exception as exc:
            logger.warning("2FA 密码校验失败: %s", exc)
            self.status = STATUS_NEED_PASSWORD
            self.error = self._explain(exc)
            return self.snapshot()

        return await self._finish()

    # ---------------------------------------------------------------- 内部

    async def _finish(self) -> Dict[str, Any]:
        """登录成功：取 USER_ID，断开临时客户端，写回配置。"""
        try:
            me = await self._client.get_me()
            self.user_id = getattr(me, "id", None)
        except Exception:
            logger.debug("获取自身信息失败", exc_info=True)

        self.status = STATUS_AUTHORIZED
        self.error = ""
        await self.abort()

        if self.user_id:
            try:
                from models.models import get_session
                from utils import app_config

                session = get_session()
                try:
                    app_config.save(session, {"USER_ID": str(self.user_id)})
                finally:
                    session.close()
                logger.info("Telegram 登录成功，USER_ID=%s 已写入配置", self.user_id)
            except Exception:
                logger.warning("写入 USER_ID 配置失败", exc_info=True)

        return self.snapshot()

    @staticmethod
    def _explain(exc: Exception) -> str:
        """把 Telethon 异常翻成能看懂的提示。"""
        name = type(exc).__name__
        mapping = {
            "PhoneCodeInvalidError": "验证码不正确",
            "PhoneCodeExpiredError": "验证码已过期，请重新发送",
            "PasswordHashInvalidError": "两步验证密码不正确",
            "PhoneNumberInvalidError": "手机号格式不正确，需要带国家码",
            "PhoneNumberBannedError": "该手机号已被限制",
            "ApiIdInvalidError": "API_ID 与 API_HASH 不匹配",
            "AuthKeyUnregisteredError": "会话已失效，请重新登录",
            "FloodWaitError": "操作过于频繁，请稍后再试",
        }
        if name in mapping:
            return mapping[name]
        return f"{name}: {exc}"


# 主进程持有唯一实例；桥接口直接引用它
login_manager = TelegramLoginManager()
