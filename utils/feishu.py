"""飞书 / Lark 自定义机器人 webhook 推送。

Apprise 1.9.2 不带 ``lark://`` 插件（该插件从 1.9.6 才有，且只支持国际版
``open.larksuite.com``），因此这里单独实现，两个域名都覆盖。

配置写法（推送设置里填写其一）：

    feishu://<token>[?secret=<签名密钥>]   中国版飞书 open.feishu.cn
    lark://<token>[?secret=<签名密钥>]     Lark 国际版 open.larksuite.com
    https://open.feishu.cn/open-apis/bot/v2/hook/<token>
    https://open.larksuite.com/open-apis/bot/v2/hook/<token>

也支持直接在地址后加 ``?secret=xxx`` 指定签名密钥。

官方文档：
- 自定义机器人：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

FEISHU_WEBHOOK_BASE = "https://open.feishu.cn/open-apis/bot/v2/hook/"
LARK_WEBHOOK_BASE = "https://open.larksuite.com/open-apis/bot/v2/hook/"

# 官方限制：单条请求体不超过 20 KB，这里为 JSON 包装与签名字段预留空间
MAX_TEXT_BYTES = 20 * 1024 - 1024

# 官方限流：单租户单机器人 100 次/分钟、5 次/秒
_MAX_SENDS_PER_SECOND = 1.5  # 飞书自定义机器人限 100 次/分钟

_SCHEME_BASE = {
    "feishu": FEISHU_WEBHOOK_BASE,
    "lark": LARK_WEBHOOK_BASE,
}

# 飞书返回码 → 可读提示
_ERROR_HINTS = {
    19021: "签名校验失败，请检查 secret 是否正确、服务器时间是否准确",
    19024: "消息未包含自定义关键词，请检查机器人的关键词安全设置",
    9499: "请求体格式错误（消息可能过长或包含非法内容）",
    11232: "触发飞书限流（单机器人 100 次/分钟、5 次/秒）",
}

_last_send_at: dict[str, float] = {}


@dataclass
class FeishuTarget:
    """一个已解析的飞书 webhook 目标。"""

    webhook_url: str
    brand: str = "feishu"
    secret: Optional[str] = None

    @property
    def label(self) -> str:
        return "Lark" if self.brand == "lark" else "飞书"

    @property
    def masked_url(self) -> str:
        token = self.webhook_url.rsplit("/", 1)[-1]
        if len(token) <= 8:
            return f"{self.label} webhook (***)"
        return f"{self.label} webhook ({token[:4]}***{token[-4:]})"


def _extract_secret(query: str) -> Optional[str]:
    """从查询串里取 secret 参数。"""
    if not query:
        return None
    params = parse_qs(query)
    values = params.get("secret") or params.get("sign") or params.get("key")
    if not values:
        return None
    secret = values[0].strip()
    return secret or None


def parse_feishu_url(raw_url: str) -> Optional[FeishuTarget]:
    """识别飞书 / Lark webhook 配置。

    不是飞书配置时返回 ``None``，调用方继续走原有的 Apprise 分支。
    """
    if not raw_url or not isinstance(raw_url, str):
        return None

    value = raw_url.strip()
    lowered = value.lower()

    # 形式一：直接粘贴完整的 webhook 地址
    for scheme, base in _SCHEME_BASE.items():
        if not lowered.startswith(base.lower()):
            continue
        remainder = value[len(base):]
        token = remainder.split("?", 1)[0].strip().strip("/")
        if not token:
            return None
        query = remainder.split("?", 1)[1] if "?" in remainder else ""
        return FeishuTarget(
            webhook_url=f"{base}{token}",
            brand=scheme,
            secret=_extract_secret(query),
        )

    # 形式二：feishu://<token> 或 lark://<token>
    parsed = urlparse(value)
    base = _SCHEME_BASE.get(parsed.scheme.lower())
    if not base:
        return None

    token = (parsed.netloc or "").strip().strip("/")
    if not token and parsed.path:
        # 兼容 feishu:///?token=xxx 这类写法
        token = parsed.path.strip("/")
    if not token:
        query = parse_qs(parsed.query)
        token_values = query.get("token") or query.get("webhook")
        token = (token_values[0].strip() if token_values else "")
    if not token:
        return None

    return FeishuTarget(
        webhook_url=f"{base}{token}",
        brand="feishu" if base == FEISHU_WEBHOOK_BASE else "lark",
        secret=_extract_secret(parsed.query),
    )


def gen_sign(timestamp: int, secret: str) -> str:
    """按官方算法生成签名。

    ``string_to_sign = f"{timestamp}\\n{secret}"`` 作为 HMAC-SHA256 的密钥，
    对空内容计算摘要后再做 Base64。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.S)
_MD_UNDERSCORE_BOLD_RE = re.compile(r"__(.+?)__", re.S)
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.S)
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def to_plain_text(text: str) -> str:
    """把 Markdown / HTML 标记压成纯文本。

    飞书 ``text`` 类型消息不解析 Markdown，保留 ``**`` 之类的符号反而难看。
    """
    if not text:
        return ""

    result = _MD_LINK_RE.sub(lambda m: f"{m.group(1)}: {m.group(2)}", text)
    result = _MD_BOLD_RE.sub(r"\1", result)
    result = _MD_UNDERSCORE_BOLD_RE.sub(r"\1", result)
    result = _MD_STRIKE_RE.sub(r"\1", result)
    result = _MD_CODE_RE.sub(r"\1", result)
    result = _MD_ITALIC_RE.sub(r"\1", result)
    result = _HTML_TAG_RE.sub("", result)
    return result


def _truncate(text: str) -> str:
    """按 UTF-8 字节数截断，避免触发飞书 20 KB 限制。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return text
    truncated = encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    return f"{truncated}\n…（内容过长，已截断）"


async def _throttle(key: str) -> None:
    """单 webhook 每秒最多 5 条，超出则等待。"""
    interval = 1.0 / _MAX_SENDS_PER_SECOND
    now = time.monotonic()
    last = _last_send_at.get(key, 0.0)
    wait = interval - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_send_at[key] = time.monotonic()


def _new_client(timeout: float) -> httpx.AsyncClient:
    """创建 httpx 客户端。

    某些部署环境会把 ``::1``、``[::1]`` 写进 ``NO_PROXY``，httpx 0.28 解析这类
    条目时会把 ``[::1]`` 误当成 host:port 并抛 ``Invalid port: ':1]'``，导致
    客户端根本无法构造。这里先按默认配置尝试，失败后退化为忽略环境变量直连，
    至少保证推送能发出去。
    """
    try:
        return httpx.AsyncClient(timeout=timeout)
    except Exception as exc:
        logger.debug(f'httpx 默认配置不可用（{exc}），改为直连模式')
        return httpx.AsyncClient(timeout=timeout, trust_env=False)


async def send_feishu_webhook(
    target: FeishuTarget,
    text: str,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    """发送一条纯文本消息，返回 ``(是否成功, 说明)``。"""
    body = to_plain_text(text or "")
    if not body.strip():
        return False, "内容为空，未发送"

    body = _truncate(body)

    payload = {
        "msg_type": "text",
        "content": {"text": body},
    }

    if target.secret:
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = gen_sign(timestamp, target.secret)

    await _throttle(target.webhook_url)

    try:
        async with _new_client(timeout) as client:
            response = await client.post(target.webhook_url, json=payload)
    except Exception as exc:
        return False, f"请求失败: {type(exc).__name__}: {exc}".rstrip(": ")

    if response.status_code != 200:
        return False, f"HTTP {response.status_code}: {response.text[:200]}"

    try:
        data = response.json()
    except Exception:
        return False, f"响应不是 JSON: {response.text[:200]}"

    # 新版返回 code/msg，旧版返回 StatusCode/StatusMessage
    code = data.get("code", data.get("StatusCode", 0))
    msg = data.get("msg", data.get("StatusMessage", ""))

    if code == 0:
        return True, "ok"

    hint = _ERROR_HINTS.get(code)
    detail = f"code={code} msg={msg}"
    if hint:
        detail = f"{detail}（{hint}）"
    return False, detail
