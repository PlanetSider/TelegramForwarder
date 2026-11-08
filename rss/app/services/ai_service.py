"""AI 供应商的存取。

一个供应商 = 一套 OpenAI 兼容接口配置（地址 + Key + 模型 + 思考模式）。
可以存多套，规则里按需选用；没有指定时用默认供应商。
"""
import logging
from typing import Any, Dict, List, Optional

from models.models import AIProvider

logger = logging.getLogger(__name__)

# 界面上展示的思考模式，与 utils.ai_client 的 reasoning_effort 对应
REASONING_OPTIONS = [
    ("", "跟随服务商默认"),
    ("minimal", "最小（minimal）"),
    ("low", "低（low）"),
    ("medium", "中（medium）"),
    ("high", "高（high）"),
    ("max", "极高（max）"),
]


def _mask_key(key: Optional[str]) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "***"
    return f"{key[:4]}…{key[-4:]}"


def list_providers(session) -> List[AIProvider]:
    return (session.query(AIProvider)
            .order_by(AIProvider.is_default.desc(), AIProvider.id.asc())
            .all())


def get_provider(session, provider_id: int) -> Optional[AIProvider]:
    if not provider_id:
        return None
    return session.query(AIProvider).get(int(provider_id))


def get_default_provider(session) -> Optional[AIProvider]:
    """优先取标记为默认的；没有标记时退回最早创建的一条。"""
    provider = (session.query(AIProvider)
                .filter(AIProvider.is_default == True)  # noqa: E712
                .first())
    if provider is not None:
        return provider
    return session.query(AIProvider).order_by(AIProvider.id.asc()).first()


def provider_rows(session) -> List[Dict[str, Any]]:
    rows = []
    for p in list_providers(session):
        rows.append({
            "id": p.id,
            "name": p.label(),
            "base_url": p.base_url or "",
            "key_display": _mask_key(p.api_key),
            "has_key": bool((p.api_key or "").strip()),
            "model": p.model or "",
            "reasoning": p.reasoning or "",
            "is_default": bool(p.is_default),
        })
    return rows


def add_provider(session, name: str, base_url: str = "", api_key: str = "",
                 model: str = "", reasoning: str = "",
                 is_default: bool = False) -> AIProvider:
    name = (name or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    provider = AIProvider(
        name=name,
        base_url=(base_url or "").strip() or None,
        api_key=(api_key or "").strip() or None,
        model=(model or "").strip() or None,
        reasoning=(reasoning or "").strip() or None,
    )
    session.add(provider)
    session.flush()
    if is_default:
        _clear_default(session, except_id=provider.id)
        provider.is_default = True
    elif not get_default_provider(session):
        provider.is_default = True
    session.commit()
    return provider


def update_provider(session, provider_id: int, **kwargs) -> AIProvider:
    provider = get_provider(session, provider_id)
    if provider is None:
        raise LookupError("供应商不存在")

    if "name" in kwargs and (kwargs["name"] or "").strip():
        provider.name = kwargs["name"].strip()
    if "base_url" in kwargs:
        provider.base_url = (kwargs["base_url"] or "").strip() or None
    if "api_key" in kwargs:
        raw = (kwargs["api_key"] or "").strip()
        # 前端把已有 Key 渲染成打码串，原样提交时表示“不修改”
        # 留空或原样回传打码串都表示「不修改」，避免编辑时把 Key 清空
        if raw and "*" not in raw and "…" not in raw:
            provider.api_key = raw
    if "model" in kwargs:
        provider.model = (kwargs["model"] or "").strip() or None
    if "reasoning" in kwargs:
        provider.reasoning = (kwargs["reasoning"] or "").strip() or None
    if kwargs.get("is_default"):
        _clear_default(session, except_id=provider.id)
        provider.is_default = True

    session.commit()
    return provider


def delete_provider(session, provider_id: int) -> None:
    provider = get_provider(session, provider_id)
    if provider is None:
        raise LookupError("供应商不存在")
    was_default = bool(provider.is_default)
    session.delete(provider)
    session.commit()
    if was_default:
        remain = session.query(AIProvider).order_by(AIProvider.id.asc()).first()
        if remain is not None:
            remain.is_default = True
            session.commit()


def set_default(session, provider_id: int) -> None:
    provider = get_provider(session, provider_id)
    if provider is None:
        raise LookupError("供应商不存在")
    _clear_default(session, except_id=provider.id)
    provider.is_default = True
    session.commit()


def _clear_default(session, except_id: Optional[int] = None) -> None:
    query = session.query(AIProvider).filter(AIProvider.is_default == True)  # noqa: E712
    if except_id:
        query = query.filter(AIProvider.id != except_id)
    for row in query.all():
        row.is_default = False


def resolve_settings(session, provider_id: Optional[int] = None) -> Dict[str, str]:
    """给调用方用的最终配置；指定 id 优先，其次是默认供应商。"""
    provider = get_provider(session, provider_id) if provider_id else None
    if provider is None:
        provider = get_default_provider(session)
    if provider is None:
        return {"name": "", "base_url": "", "api_key": "",
                "model": "", "reasoning": ""}
    return {
        "name": provider.label(),
        "base_url": (provider.base_url or "").strip(),
        "api_key": (provider.api_key or "").strip(),
        "model": (provider.model or "").strip(),
        "reasoning": (provider.reasoning or "").strip(),
    }
