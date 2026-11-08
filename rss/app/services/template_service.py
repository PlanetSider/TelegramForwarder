"""转发模板服务。

模板就是「没有源会话和目标的整套规则设置」：新建转发规则时选一个模板，
把这里保存的字段值整体套用到新规则上。

模板本身不参与转发，也不持有任何 Telegram / 飞书会话。
"""
import json
import logging
from typing import Any, Dict, List, Optional

from models.models import (
    AddMode,
    ForwardMode,
    ForwardRule,
    ForwardTemplate,
    HandleMode,
    MessageMode,
    PreviewMode,
    get_session,
)

logger = logging.getLogger(__name__)

# 枚举字段 -> 枚举类（键同时用于取值和校验）
ENUM_KEYS = {
    "forward_mode": ForwardMode,
    "add_mode": AddMode,
    "extension_filter_mode": AddMode,
    "message_mode": MessageMode,
    "is_preview": PreviewMode,
    "handle_mode": HandleMode,
}

BOOL_KEYS = {
    "is_filter_user_info", "is_replace", "enable_reverse_blacklist",
    "enable_reverse_whitelist", "use_global_whitelist", "use_global_blacklist",
    "enable_media_type_filter", "enable_media_size_filter",
    "is_send_over_media_size_message", "enable_extension_filter",
    "media_allow_text", "is_ai", "enable_ai_upload_image", "is_keyword_after_ai",
    "is_summary", "is_top_summary", "is_original_sender", "is_original_time",
    "is_original_link", "is_delete_original", "enable_comment_button",
    "enable_delay", "is_ufb",
}

INT_KEYS = {"max_media_size", "delay_seconds", "ai_provider_id"}

STR_KEYS = {
    "ai_model", "ai_prompt", "summary_time", "summary_prompt",
    "userinfo_template", "time_template", "original_link_template",
    "ufb_domain", "ufb_item",
}

CONFIG_KEYS = sorted(set(ENUM_KEYS) | BOOL_KEYS | INT_KEYS | STR_KEYS)

DEFAULT_TEMPLATE_NAME = "默认模板"


def _enum_value(value) -> str:
    """把枚举/字符串统一成枚举的 name，便于存 JSON。"""
    return getattr(value, "name", None) or str(value or "")


def dump_rule(rule: ForwardRule) -> Dict[str, Any]:
    """从一条现有规则里抽出可复用设置。"""
    config: Dict[str, Any] = {}
    for key in CONFIG_KEYS:
        value = getattr(rule, key, None)
        if key in ENUM_KEYS:
            config[key] = _enum_value(value)
        elif key in BOOL_KEYS:
            config[key] = bool(value)
        elif key in INT_KEYS:
            config[key] = int(value) if value is not None else None
        else:
            config[key] = "" if value is None else str(value)
    return config


def normalize_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """只保留已知字段，并把值收敛到合法类型。"""
    result: Dict[str, Any] = {}
    for key, value in (config or {}).items():
        if key not in CONFIG_KEYS:
            continue
        if key in ENUM_KEYS:
            name = _enum_value(value)
            if name in ENUM_KEYS[key].__members__:
                result[key] = name
        elif key in BOOL_KEYS:
            if isinstance(value, str):
                result[key] = value.strip().lower() in ("1", "true", "yes", "on")
            else:
                result[key] = bool(value)
        elif key in INT_KEYS:
            try:
                result[key] = int(value)
            except (TypeError, ValueError):
                continue
        else:
            result[key] = "" if value is None else str(value)
    return result


def apply_to_rule(rule: ForwardRule, config: Optional[Dict[str, Any]]) -> List[str]:
    """把模板设置写到一条规则上，返回实际写入的字段名。"""
    applied: List[str] = []
    for key, value in normalize_config(config).items():
        try:
            if key in ENUM_KEYS:
                setattr(rule, key, ENUM_KEYS[key][value])
            elif key == "ai_provider_id":
                setattr(rule, key, value or None)
            else:
                setattr(rule, key, value)
            applied.append(key)
        except Exception as exc:  # 单字段异常不影响其它字段
            logger.warning("模板字段 %s 应用失败: %s", key, exc)
    return applied


def load_config(template: ForwardTemplate) -> Dict[str, Any]:
    if not template or not template.config:
        return {}
    try:
        return normalize_config(json.loads(template.config))
    except (ValueError, TypeError) as exc:
        logger.warning("模板 %s 的配置不是合法 JSON: %s", template.id, exc)
        return {}


def rows(session) -> List[Dict[str, Any]]:
    """模板列表（默认模板排最前）。"""
    templates = (
        session.query(ForwardTemplate)
        .order_by(ForwardTemplate.is_default.desc(), ForwardTemplate.id.asc())
        .all()
    )
    result = []
    for t in templates:
        config = load_config(t)
        result.append({
            "id": t.id,
            "name": t.label(),
            "description": (t.description or "").strip(),
            "is_default": bool(t.is_default),
            "config": config,
            "field_count": len(config),
            "updated_at": t.updated_at.isoformat() if t.updated_at else "",
        })
    return result


def get(session, template_id: int) -> Optional[ForwardTemplate]:
    try:
        return session.query(ForwardTemplate).filter(ForwardTemplate.id == int(template_id)).first()
    except (TypeError, ValueError):
        return None


def create(session, name: str, description: str = "",
           config: Optional[Dict[str, Any]] = None,
           is_default: bool = False) -> ForwardTemplate:
    name = (name or "").strip()
    if not name:
        raise ValueError("模板名称不能为空")
    template = ForwardTemplate(
        name=name,
        description=(description or "").strip() or None,
        config=json.dumps(normalize_config(config or {}), ensure_ascii=False),
        is_default=bool(is_default),
    )
    session.add(template)
    session.flush()
    if template.is_default:
        _clear_default(session, except_id=template.id)
    session.commit()
    return template


def update(session, template_id: int, **kwargs) -> ForwardTemplate:
    template = get(session, template_id)
    if template is None:
        raise LookupError("模板不存在")

    if "name" in kwargs and (kwargs["name"] or "").strip():
        template.name = kwargs["name"].strip()
    if "description" in kwargs:
        template.description = (kwargs["description"] or "").strip() or None
    if "config" in kwargs:
        template.config = json.dumps(normalize_config(kwargs["config"]), ensure_ascii=False)
    if kwargs.get("is_default"):
        _clear_default(session, except_id=template.id)
        template.is_default = True
    session.commit()
    return template


def delete(session, template_id: int) -> None:
    template = get(session, template_id)
    if template is None:
        raise LookupError("模板不存在")
    if template.is_default:
        raise ValueError("默认模板不能删除，请先把其它模板设为默认")
    session.delete(template)
    session.commit()


def set_default(session, template_id: int) -> ForwardTemplate:
    template = get(session, template_id)
    if template is None:
        raise LookupError("模板不存在")
    _clear_default(session, except_id=template.id)
    template.is_default = True
    session.commit()
    return template


def _clear_default(session, except_id: Optional[int] = None) -> None:
    query = session.query(ForwardTemplate).filter(ForwardTemplate.is_default.is_(True))
    if except_id is not None:
        query = query.filter(ForwardTemplate.id != int(except_id))
    for template in query.all():
        template.is_default = False


def default_template(session) -> Optional[ForwardTemplate]:
    template = (
        session.query(ForwardTemplate)
        .filter(ForwardTemplate.is_default.is_(True))
        .first()
    )
    if template is None:
        template = session.query(ForwardTemplate).order_by(ForwardTemplate.id.asc()).first()
    return template


def ensure_default(session, defaults: Optional[Dict[str, Any]] = None) -> ForwardTemplate:
    """保证至少有一个模板；首个模板用传入的默认值初始化。"""
    template = default_template(session)
    if template is not None:
        return template
    return create(
        session,
        name=DEFAULT_TEMPLATE_NAME,
        description="新建转发规则时默认套用的设置",
        config=defaults or {},
        is_default=True,
    )
