"""通用 OpenAI 兼容提供者。

替代原先按厂商拆分的 openai/gemini/claude/deepseek/qwen/grok 六个实现：
统一走 ``/chat/completions``，图片按 ``image_url`` + base64 data URL 传，
因此只要服务端支持 OpenAI 多模态格式就能出图。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ai.base import BaseAIProvider
from utils.ai_client import chat_completion

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = "请尊重原意，保持原有格式不变，用简体中文重写下面的内容："


class GenericProvider(BaseAIProvider):
    """把「地址 + 密钥 + 模型 + 思考模式」四要素打包成一个提供者。"""

    def __init__(self,
                 model: Optional[str] = None,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 reasoning: Optional[str] = None) -> None:
        self.model = model or ""
        self.base_url = base_url or ""
        self.api_key = api_key or ""
        self.reasoning = reasoning or ""

    async def initialize(self, **kwargs) -> None:
        """兼容基类签名；配置在构造时就已就位，无需额外初始化。"""
        for key in ("model", "base_url", "api_key", "reasoning"):
            value = kwargs.get(key)
            if value:
                setattr(self, key, value)

    def _build_messages(self,
                        message: str,
                        prompt: Optional[str],
                        images: Optional[List[Dict[str, str]]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        system_prompt = (prompt or "").strip() or DEFAULT_PROMPT
        messages.append({"role": "system", "content": system_prompt})

        text = message or ""
        payload_images = [img for img in (images or []) if img and img.get("data")]

        if not payload_images:
            messages.append({"role": "user", "content": text})
            return messages

        content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        for img in payload_images:
            mime = img.get("mime_type") or "image/jpeg"
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{img['data']}"},
            })
        messages.append({"role": "user", "content": content})
        return messages

    async def process_message(self,
                              message: str,
                              prompt: Optional[str] = None,
                              images: Optional[List[Dict[str, str]]] = None,
                              **kwargs) -> str:
        if not self.api_key:
            logger.warning("未配置 AI API Key，若接口需要鉴权将会失败")

        messages = self._build_messages(message, prompt, images)
        logger.info("调用 AI 模型 %s（%d 张图片）", self.model or "(未指定)", len(images or []))

        return await chat_completion(
            messages=messages,
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            reasoning=self.reasoning,
            timeout=kwargs.get("timeout", 180.0),
        )


__all__ = ["GenericProvider"]
