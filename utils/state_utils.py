import asyncio
import logging
from typing import Optional

from managers.state_manager import state_manager

logger = logging.getLogger(__name__)


async def cancel_state_after_timeout(
    user_id: int,
    chat_id: int,
    expected_state: Optional[str] = None,
    timeout_minutes: int = 5,
) -> None:
    """超时后清除用户状态。

    expected_state 用于身份校验：只有当当前状态仍与设置时一致才清除，
    避免用户在此期间设置了新状态、却被旧超时任务误清。
    """
    await asyncio.sleep(timeout_minutes * 60)
    cleared = state_manager.clear_state_if(user_id, chat_id, expected_state)
    if cleared:
        logger.info(f"状态超时自动取消 - user_id: {user_id}, chat_id: {chat_id}, state: {expected_state}")
