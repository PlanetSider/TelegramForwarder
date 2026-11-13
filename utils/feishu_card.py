"""飞书消息卡片。

应用机器人和 webhook 群机器人都能发 ``interactive``，但投递格式不同：

- 应用机器人: ``{"msg_type": "interactive", "content": "<卡片 JSON 字符串>"}``
- webhook:    ``{"msg_type": "interactive", "card": <卡片对象>}``

webhook 发出的卡片是单向的（没有 message_id，也没有事件通道），
所以这里只做展示型卡片，不放按钮。
"""
from __future__ import annotations

import re
from typing import List, Optional

# 单张卡片的正文保守上限。飞书建议整个 JSON ≤ 30KB，留足余量。
CARD_CONTENT_LIMIT = 6000
CARD_TITLE_LIMIT = 60
CARD_FOOTER_LIMIT = 400

_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def strip_html(text: str) -> str:
    """去掉 HTML 标签，保留 Markdown。

    卡片正文用 ``lark_md`` 渲染，它认 ``**粗体**``、``[文本](链接)`` 这类语法，
    但会把 HTML 标签原样打出来，所以只清标签、不动 Markdown。
    """
    if not text:
        return ""
    return _HTML_TAG_RE.sub("", text)


def build_card(
    title: Optional[str],
    content: str,
    footer: Optional[str] = None,
    template: str = "blue",
) -> dict:
    """组装一张展示型卡片（卡片 JSON 1.0 语法，兼容性最好）。"""
    safe_title = (title or "转发消息").strip()[:CARD_TITLE_LIMIT]
    body = (content or "").strip()[:CARD_CONTENT_LIMIT]
    if not body:
        body = "（空消息）"

    elements: List[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": body}},
    ]

    footer_text = (footer or "").strip()
    if footer_text:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": footer_text[:CARD_FOOTER_LIMIT]}],
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": safe_title},
            "template": template,
        },
        "elements": elements,
    }


def is_webhook_v2(webhook_url: str) -> bool:
    """V1 地址（``/open-apis/bot/hook/xxx``）只支持纯文本，不能发卡片。"""
    return "/bot/v2/" in (webhook_url or "")
