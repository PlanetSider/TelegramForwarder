"""OpenAI 兼容接口的极简客户端。

项目不再内置各家 SDK / 各自的鉴权分支：只要服务端提供 ``/models`` 与
``/chat/completions`` 两个接口，就能接进来（OpenAI、DeepSeek、通义、Grok、
Ollama、vLLM、one-api、new-api 等等）。

这样做的代价是丢掉各家私有能力（例如 Anthropic 的 messages 协议、Gemini 的
generateContent），收益是配置从十几个键收敛成「地址 + 密钥 + 模型」三项。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_TIMEOUT = 20.0
_CHAT_TIMEOUT = 180.0
_HAS_VERSION = re.compile(r"/v\d+[a-z0-9]*$", re.IGNORECASE)


def _new_client(timeout: float) -> httpx.AsyncClient:
    """构造 httpx 客户端。

    某些环境的 NO_PROXY 含 ``[::1]`` 这种带方括号的写法，httpx 0.28 解析端口时
    会抛 ``Invalid port``，因此失败后退回不读环境变量的客户端。
    """
    try:
        return httpx.AsyncClient(timeout=timeout)
    except Exception:  # pragma: no cover - 取决于运行环境
        return httpx.AsyncClient(timeout=timeout, trust_env=False)


def normalize_base_url(base_url: Optional[str]) -> str:
    """把用户填的地址补成带版本段的根地址。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return DEFAULT_BASE_URL
    if _HAS_VERSION.search(base):
        return base
    return base + "/v1"


def _endpoint(base_url: Optional[str], path: str) -> str:
    return normalize_base_url(base_url) + path


def _headers(api_key: Optional[str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def list_models(base_url: Optional[str] = None,
                      api_key: Optional[str] = None,
                      timeout: float = _DEFAULT_TIMEOUT) -> List[str]:
    """读取 ``GET {base}/models``，返回去重排序后的模型 ID 列表。"""
    url = _endpoint(base_url, "/models")
    async with _new_client(timeout) as client:
        resp = await client.get(url, headers=_headers(api_key))

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"返回的不是 JSON: {exc}") from exc

    items = data.get("data") if isinstance(data, dict) else None
    if items is None and isinstance(data, dict):
        items = data.get("models")
    if not isinstance(items, list):
        raise RuntimeError("响应里没有 data 列表，可能不是 OpenAI 兼容接口")

    models: List[str] = []
    for item in items:
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("name")
        else:
            model_id = str(item)
        if model_id:
            models.append(str(model_id))

    return sorted(set(models))


async def chat_completion(messages: List[Dict[str, Any]],
                          model: str,
                          base_url: Optional[str] = None,
                          api_key: Optional[str] = None,
                          reasoning: Optional[str] = None,
                          timeout: float = _CHAT_TIMEOUT) -> str:
    """调用 ``POST {base}/chat/completions``，返回纯文本结果。"""
    if not model:
        raise RuntimeError("没有指定模型")

    payload: Dict[str, Any] = {"model": model, "messages": messages}
    mode = (reasoning or "").strip()
    if mode and mode != "default":
        payload["reasoning_effort"] = mode

    url = _endpoint(base_url, "/chat/completions")
    async with _new_client(timeout) as client:
        resp = await client.post(url, headers=_headers(api_key), json=payload)

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"返回的不是 JSON: {exc}") from exc

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise RuntimeError(f"响应中没有 choices：{json.dumps(data, ensure_ascii=False)[:300]}")

    message = choices[0].get("message") or {}
    content = message.get("content")

    # 兼容 content 为分段数组的多模态返回
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("text"):
                parts.append(str(part["text"]))
        content = "".join(parts)

    text = (content or "").strip()
    if not text:
        # 只返回了思维链的模型（部分推理模型）退而取 reasoning_content
        text = (message.get("reasoning_content") or "").strip()
    if not text:
        raise RuntimeError("模型返回了空内容")

    return text
