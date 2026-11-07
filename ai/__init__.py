"""AI 提供者装配。

现在只保留一个通用提供者：控制台里填「OpenAI 兼容接口地址 + 密钥 + 模型 +
思考模式」，其余服务商只要兼容 ``/chat/completions`` 与 ``/models`` 就能直接用。

旧的多厂商实现（openai/gemini/claude/deepseek/qwen/grok 六个文件）仍然留在
目录里作为参考，但不再参与装配——它们各自依赖私有 SDK，配置项也太散。
"""
from __future__ import annotations

import logging
from typing import Optional

from ai.base import BaseAIProvider
from ai.generic_provider import GenericProvider
from utils.constants import DEFAULT_AI_MODEL

logger = logging.getLogger(__name__)

__all__ = ["BaseAIProvider", "GenericProvider", "get_ai_provider"]


def _read_ai_config() -> dict:
    """从数据库读一份 AI 配置；失败时返回空字典而不是抛错。"""
    try:
        from models.models import get_session
        from utils import app_config

        session = get_session()
        try:
            return app_config.get_current(session)
        finally:
            session.close()
    except Exception as exc:  # pragma: no cover - 仅在库不可用时触发
        logger.warning("读取 AI 配置失败: %s", exc)
        return {}


def _resolve_provider(provider_id: Optional[int] = None) -> dict:
    """从 ai_providers 表取一套配置；表为空时回落到 app_config 的旧键。"""
    try:
        from models.models import get_session
        from rss.app.services import ai_service

        session = get_session()
        try:
            cfg = ai_service.resolve_settings(session, provider_id)
        finally:
            session.close()
        if cfg.get("base_url") or cfg.get("api_key"):
            return cfg
    except Exception as exc:  # pragma: no cover - 仅在库不可用时触发
        logger.warning("读取 AI 供应商失败: %s", exc)

    legacy = _read_ai_config()
    return {
        "name": "",
        "base_url": (legacy.get("AI_BASE_URL") or "").strip(),
        "api_key": (legacy.get("AI_API_KEY") or "").strip(),
        "model": (legacy.get("AI_MODEL") or "").strip(),
        "reasoning": (legacy.get("AI_REASONING") or "").strip(),
    }


async def get_ai_provider(model: Optional[str] = None,
                          provider_id: Optional[int] = None) -> GenericProvider:
    """按控制台配置构造提供者。

    ``provider_id`` 指定用哪套供应商（规则上可选），不传则用默认供应商；
    ``model`` 是规则上单独指定的模型，优先于供应商自己的模型。
    """
    cfg = _resolve_provider(provider_id)

    base_url = cfg["base_url"]
    api_key = cfg["api_key"]
    reasoning = cfg["reasoning"]

    chosen = ((model or "").strip()
              or cfg.get("model", "")
              or DEFAULT_AI_MODEL)

    if not api_key and "127.0.0.1" not in base_url and "localhost" not in base_url:
        raise ValueError("尚未配置 AI API Key，请在控制台「通道设置 · AI 服务」中添加供应商")

    return GenericProvider(model=chosen, base_url=base_url, api_key=api_key,
                           reasoning=reasoning)
