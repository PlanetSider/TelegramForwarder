"""规则查询与配置聚合服务。

Web 控制台与（后续的）Bot 命令共用这一层，避免把业务逻辑写进路由。
本模块当前提供**只读**能力，写操作见各 update_* 函数（P1 阶段补齐）。
"""
import logging
import re
from typing import Any, Dict, List, Optional

from models.models import (
    ForwardRule, Chat, Keyword, ReplaceRule, MediaTypes, MediaExtensions,
    PushConfig, RSSConfig, RSSPattern, RuleSync,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 序列化

def _chat_brief(chat: Optional[Chat], placeholder: str = "") -> Optional[Dict[str, Any]]:
    if not chat:
        # 推送规则不复制到别的 Telegram 会话，target 为空，用占位名代替
        if placeholder:
            return {"id": None, "name": placeholder, "tg_id": None}
        return None
    return {"id": chat.id, "name": chat.name or "未命名",
            "tg_id": chat.telegram_chat_id}


def _enum_value(value, default: str = "") -> str:
    """Enum 取 .value；已是字符串则原样返回。"""
    if value is None:
        return default
    return getattr(value, "value", str(value))


def _route_kind(rule: ForwardRule) -> str:
    """规则的路由类型，列表页状态列用。

    same_group 同群重发 / cross_group 跨群转发 / cross_channel 跨通道。
    """
    if getattr(rule, "feishu_bot_id", None) or rule.enable_only_push:
        return "cross_channel"
    if rule.target_chat_id is None:
        return "cross_channel"
    if rule.source_chat_id and rule.source_chat_id == rule.target_chat_id:
        return "same_group"
    return "cross_group"


def _target_label(rule: ForwardRule) -> str:
    """目标的可读描述，带通道前缀，如「飞书群机器人 · OVH群机器人」。"""
    if getattr(rule, "feishu_bot_id", None):
        name = ""
        try:
            from models.models import FeishuBot
            session = rule._sa_instance_state.session
            if session is not None:
                bot = session.query(FeishuBot).get(rule.feishu_bot_id)
                name = (getattr(bot, "name", "") or "").strip()
        except Exception:
            name = ""
        return f"飞书应用机器人 · {name or ('#' + str(rule.feishu_bot_id))}"

    cfg = getattr(rule, "push_config", None)
    if cfg is not None:
        name = (getattr(cfg, "name", "") or "").strip() or "群机器人"
        return f"飞书群机器人 · {name}"

    if rule.target_chat_id is None:
        return "飞书推送"

    name = (rule.target_chat.name if rule.target_chat else "") or "?"
    return f"Telegram · {name}"


def rule_brief(rule: ForwardRule) -> Dict[str, Any]:
    """列表页用的轻量结构，不展开子表。"""
    return {
        "id": rule.id,
        "source": _chat_brief(rule.source_chat),
        "target": _chat_brief(rule.target_chat, "飞书推送"),
        "target_label": _target_label(rule),
        "kind": _route_kind(rule),
        "enable_rule": bool(rule.enable_rule),
        "use_bot": bool(rule.use_bot),
        "handle_mode": _enum_value(rule.handle_mode, "FORWARD"),
        "forward_mode": _enum_value(rule.forward_mode, "blacklist"),
        "is_replace": bool(rule.is_replace),
        "is_ai": bool(rule.is_ai),
        "is_summary": bool(rule.is_summary),
        "enable_push": bool(rule.enable_push),
        "enable_only_push": bool(rule.enable_only_push),
        "enable_rss": bool(rule.rss_config.enable_rss) if rule.rss_config else False,
        "enable_media_type_filter": bool(rule.enable_media_type_filter),
        "enable_media_size_filter": bool(rule.enable_media_size_filter),
        "enable_extension_filter": bool(rule.enable_extension_filter),
        "only_rss": bool(rule.only_rss),
        "enable_sync": bool(rule.enable_sync),
        "keyword_count": len(rule.keywords or []),
        "replace_count": len(rule.replace_rules or []),
    }


def rule_detail(rule: ForwardRule) -> Dict[str, Any]:
    """详情页用的完整结构，展开所有子表。"""
    media = rule.media_types
    media_dict = {
        "photo": bool(media.photo) if media else False,
        "document": bool(media.document) if media else False,
        "video": bool(media.video) if media else False,
        "audio": bool(media.audio) if media else False,
        "voice": bool(media.voice) if media else False,
    } if media else None

    rss = rule.rss_config
    rss_dict = None
    if rss:
        rss_dict = {
            "id": rss.id,
            "enable_rss": bool(rss.enable_rss),
            "rule_title": rss.rule_title or "",
            "rule_description": rss.rule_description or "",
            "language": rss.language or "zh-CN",
            "max_items": rss.max_items or 50,
            "is_auto_title": bool(rss.is_auto_title),
            "is_auto_content": bool(rss.is_auto_content),
            "is_ai_extract": bool(rss.is_ai_extract),
            "ai_extract_prompt": rss.ai_extract_prompt or "",
            "is_auto_markdown_to_html": bool(rss.is_auto_markdown_to_html),
            "enable_custom_title_pattern": bool(rss.enable_custom_title_pattern),
            "enable_custom_content_pattern": bool(rss.enable_custom_content_pattern),
            "patterns": [
                {"id": p.id, "pattern": p.pattern, "pattern_type": p.pattern_type,
                 "priority": p.priority}
                for p in sorted(rss.patterns or [], key=lambda x: (x.priority or 0, x.id))
            ],
        }

    feishu_bot = None
    bot_id = getattr(rule, "feishu_bot_id", None)
    if bot_id:
        try:
            from models.models import FeishuBot
            session = rule._sa_instance_state.session
            bot = session.query(FeishuBot).get(bot_id) if session is not None else None
            if bot is not None:
                name = (bot.name or "").strip()
                feishu_bot = {
                    "id": bot.id,
                    "app_id": bot.app_id,
                    "name": name,
                    "label": name or bot.app_id,
                    "platform": _enum_value(bot.platform, "feishu"),
                    "chat_id": (rule.feishu_chat_id or "").strip(),
                }
        except Exception:
            feishu_bot = None

    return {
        "id": rule.id,
        "feishu_bot": feishu_bot,
        "source": _chat_brief(rule.source_chat),
        "target": _chat_brief(rule.target_chat, "飞书推送"),
        # 只走推送的规则（目标为空，或显式开了「仅推送不转发」），
        # Telegram 专属的设置项在页面上不展示
        "is_push_only": rule.target_chat_id is None or bool(rule.enable_only_push),
        # 基础
        "enable_rule": bool(rule.enable_rule),
        "use_bot": bool(rule.use_bot),
        "handle_mode": _enum_value(rule.handle_mode, "FORWARD"),
        "message_mode": _enum_value(rule.message_mode, "Markdown"),
        "is_preview": _enum_value(rule.is_preview, "follow"),
        "enable_delay": bool(rule.enable_delay),
        "delay_seconds": rule.delay_seconds or 0,
        # 附信息
        "is_original_sender": bool(rule.is_original_sender),
        "is_original_link": bool(rule.is_original_link),
        "is_original_time": bool(rule.is_original_time),
        "userinfo_template": rule.userinfo_template or "",
        "original_link_template": rule.original_link_template or "",
        "time_template": rule.time_template or "",
        "is_delete_original": bool(rule.is_delete_original),
        "enable_comment_button": bool(rule.enable_comment_button),
        "is_filter_user_info": bool(rule.is_filter_user_info),
        # 过滤
        "forward_mode": _enum_value(rule.forward_mode, "blacklist"),
        "add_mode": _enum_value(rule.add_mode, "blacklist"),
        "enable_reverse_blacklist": bool(rule.enable_reverse_blacklist),
        "enable_reverse_whitelist": bool(rule.enable_reverse_whitelist),
        "keywords": [
            {"id": k.id, "keyword": k.keyword or "", "is_regex": bool(k.is_regex),
             "is_blacklist": bool(k.is_blacklist)}
            for k in (rule.keywords or [])
        ],
        # 替换
        "is_replace": bool(rule.is_replace),
        "replaces": [
            {"id": r.id, "pattern": r.pattern, "content": r.content or ""}
            for r in (rule.replace_rules or [])
        ],
        # 媒体
        "enable_media_type_filter": bool(rule.enable_media_type_filter),
        "enable_media_size_filter": bool(rule.enable_media_size_filter),
        "max_media_size": rule.max_media_size,
        "is_send_over_media_size_message": bool(rule.is_send_over_media_size_message),
        "enable_extension_filter": bool(rule.enable_extension_filter),
        "extension_filter_mode": _enum_value(rule.extension_filter_mode, "blacklist"),
        "media_allow_text": bool(rule.media_allow_text),
        "media_types": media_dict,
        "media_extensions": [e.extension for e in (rule.media_extensions or [])],
        # AI
        "is_ai": bool(rule.is_ai),
        "ai_model": rule.ai_model or "",
        "ai_provider_id": getattr(rule, "ai_provider_id", None),
        "ai_prompt": rule.ai_prompt or "",
        "enable_ai_upload_image": bool(rule.enable_ai_upload_image),
        "is_keyword_after_ai": bool(rule.is_keyword_after_ai),
        "is_summary": bool(rule.is_summary),
        "summary_time": rule.summary_time or "07:00",
        "summary_prompt": rule.summary_prompt or "",
        "is_top_summary": bool(rule.is_top_summary),
        # 推送
        "enable_push": bool(rule.enable_push),
        "enable_only_push": bool(rule.enable_only_push),
        "push_channels": [
            {"id": p.id, "url": p.push_channel,
             "name": (p.name or "").strip(),
             "enabled": bool(p.enable_push_channel),
             "media_send_mode": p.media_send_mode or "Single",
             **_push_label(p.push_channel)}
            for p in _push_rows(rule)
        ],
        # RSS
        "rss": rss_dict,
        "only_rss": bool(rule.only_rss),
        # 高级
        "is_ufb": bool(rule.is_ufb),
        "ufb_domain": rule.ufb_domain or "",
        "ufb_item": rule.ufb_item or "main",
        "enable_sync": bool(rule.enable_sync),
        "use_global_whitelist": bool(getattr(rule, "use_global_whitelist", False)),
        "use_global_blacklist": bool(getattr(rule, "use_global_blacklist", False)),
        "syncs": [s.sync_rule_id for s in (rule.rule_syncs or [])],
    }


def _mask_url(url: str, head: int = 14, tail: int = 4) -> str:
    """把地址中间部分打码，避免在页面上暴露完整 token。"""
    if len(url) <= head + tail + 3:
        return url
    return f"{url[:head]}…{url[-tail:]}"


def _push_label(url: Optional[str]) -> Dict[str, str]:
    """识别推送地址类型，供界面显示品牌标签与打码串。"""
    if not url:
        return {"label": "空地址", "brand": "unknown", "display": ""}
    try:
        from utils.feishu import parse_feishu_url
        target = parse_feishu_url(url)
    except Exception:  # feishu 模块不可用不应影响页面
        target = None
    if target is not None:
        return {
            "label": target.label,
            "brand": target.brand,
            "display": _mask_url(target.webhook_url),
        }
    if "://" in url:
        scheme = url.split("://", 1)[0].lower()
        return {"label": scheme.upper(), "brand": "other",
                "display": _mask_url(url)}
    return {"label": "未知", "brand": "unknown", "display": _mask_url(url)}


def _push_rows(rule: ForwardRule) -> List[PushConfig]:
    """一条规则可挂多个推送地址（PushConfig 无唯一约束）。

    `rule.push_config` 是 uselist=False 的关系，只反映第一条，
    因此这里直接走 session 查询，保证多地址场景可见。
    """
    session = getattr(rule, "_sa_instance_state", None)
    obj_session = session.session if session is not None else None
    if obj_session is None:
        return [rule.push_config] if rule.push_config else []
    try:
        return (obj_session.query(PushConfig)
                .filter(PushConfig.rule_id == rule.id)
                .order_by(PushConfig.id)
                .all())
    except Exception as exc:  # pragma: no cover - 查询失败不应炸页面
        logger.warning("查询推送配置失败 rule=%s: %s", rule.id, exc)
        return []


# ---------------------------------------------------------------- 查询

def list_rules(session, q: str = "", status: str = "", mode: str = "") -> List[Dict[str, Any]]:
    """规则列表，支持关键词/状态/模式筛选。"""
    query = session.query(ForwardRule)
    if q:
        like = f"%{q.strip()}%"
        query = query.join(Chat, ForwardRule.source_chat_id == Chat.id, isouter=True)
        query = query.filter(Chat.name.like(like))
    if status == "enabled":
        query = query.filter(ForwardRule.enable_rule.is_(True))
    elif status == "disabled":
        query = query.filter(ForwardRule.enable_rule.is_(False))
    if mode == "bot":
        query = query.filter(ForwardRule.use_bot.is_(True))
    elif mode == "user":
        query = query.filter(ForwardRule.use_bot.is_(False))

    rules = query.order_by(ForwardRule.id).all()
    return [rule_brief(r) for r in rules]


def get_rule(session, rule_id: int) -> Optional[Dict[str, Any]]:
    rule = session.query(ForwardRule).get(rule_id)
    return rule_detail(rule) if rule else None


def get_overview(session) -> Dict[str, Any]:
    """概览页数据。转发计数来自 ForwardStat（P2 新增），此处做缺省兜底。"""
    total = session.query(ForwardRule).count()
    enabled = session.query(ForwardRule).filter(ForwardRule.enable_rule.is_(True)).count()
    bot_mode = session.query(ForwardRule).filter(ForwardRule.use_bot.is_(True)).count()
    rss_enabled = (session.query(ForwardRule)
                   .join(RSSConfig, ForwardRule.id == RSSConfig.rule_id)
                   .filter(RSSConfig.enable_rss.is_(True)).count())
    push_enabled = session.query(ForwardRule).filter(ForwardRule.enable_push.is_(True)).count()
    ai_enabled = session.query(ForwardRule).filter(ForwardRule.is_ai.is_(True)).count()

    stats = {"today_ok": 0, "today_fail": 0}
    try:
        from models.models import ForwardStat
        import datetime
        today = datetime.date.today()
        rows = (session.query(ForwardStat)
                .filter(ForwardStat.day == today).all())
        stats["today_ok"] = sum(r.ok_count or 0 for r in rows)
        stats["today_fail"] = sum(r.fail_count or 0 for r in rows)
    except Exception as exc:  # 表尚未迁移或统计未启用
        logger.debug("读取转发统计失败（可忽略）: %s", exc)

    return {
        "total": total,
        "enabled": enabled,
        "disabled": total - enabled,
        "bot_mode": bot_mode,
        "user_mode": total - bot_mode,
        "rss_enabled": rss_enabled,
        "push_enabled": push_enabled,
        "ai_enabled": ai_enabled,
        "today_ok": stats["today_ok"],
        "today_fail": stats["today_fail"],
    }


def get_rule_health(session, limit: int = 5) -> List[Dict[str, Any]]:
    """概览页的规则健康列表（当前按 id 取前 N 条，成功率为占位）。"""
    rules = (session.query(ForwardRule)
             .filter(ForwardRule.enable_rule.is_(True))
             .order_by(ForwardRule.id).limit(limit).all())
    out = []
    for r in rules:
        brief = rule_brief(r)
        out.append({
            "id": r.id,
            "name": brief["source"]["name"] if brief["source"] else f"规则 {r.id}",
            "use_bot": brief["use_bot"],
            "is_ai": brief["is_ai"],
            "enable_rss": brief["enable_rss"],
            "success_rate": None,
        })
    return out


# ---------------------------------------------------------------- 写操作
#
# 控制台只暴露「配置类」字段。源/目标频道属于 Telegram 实体，
# 创建与改绑仍走 Bot 的 /bind 流程（见 P4 的说明）。

_BOOL_FIELDS = {
    "enable_rule", "use_bot", "enable_delay", "is_replace",
    "is_original_sender", "is_original_link", "is_original_time",
    "is_delete_original", "enable_comment_button", "is_filter_user_info",
    "enable_reverse_blacklist", "enable_reverse_whitelist",
    "enable_media_type_filter", "enable_media_size_filter",
    "is_send_over_media_size_message", "enable_extension_filter",
    "media_allow_text", "is_ai", "enable_ai_upload_image",
    "is_keyword_after_ai", "is_summary", "is_top_summary",
    "enable_push", "enable_only_push", "only_rss", "is_ufb", "enable_sync",
    "use_global_whitelist", "use_global_blacklist",
}

_INT_FIELDS = {"delay_seconds": 0, "max_media_size": 10}

_STR_FIELDS = {
    "ai_model", "ai_prompt", "summary_time", "summary_prompt",
    "userinfo_template", "original_link_template", "time_template",
    "ufb_domain", "ufb_item",
}

_MEDIA_FLAGS = {"photo", "document", "video", "audio", "voice"}

_RSS_BOOL_FIELDS = {
    "enable_rss", "is_auto_title", "is_auto_content", "is_ai_extract",
    "is_auto_markdown_to_html", "enable_custom_title_pattern",
    "enable_custom_content_pattern",
}


def _enum_maps():
    from enums.enums import ForwardMode, PreviewMode, MessageMode, AddMode, HandleMode
    return {
        "forward_mode": ForwardMode,
        "add_mode": AddMode,
        "extension_filter_mode": ForwardMode,
        "message_mode": MessageMode,
        "is_preview": PreviewMode,
        "handle_mode": HandleMode,
    }


def _coerce(value, kind: str):
    """把 JSON 值转成目标类型；无法转换时抛 ValueError 由调用方拒绝。"""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind == "int":
        if value is None or value == "":
            raise ValueError("不能为空")
        return int(value)
    if value is None:
        return ""
    return str(value)


def update_rule_fields(session, rule_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    """按字段白名单更新规则；返回 {updated, skipped, rule}。

    未知字段一律忽略（不报错），避免前端多传一个字段就整单失败。
    """
    rule = session.query(ForwardRule).get(rule_id)
    if not rule:
        raise LookupError("规则不存在")

    enums = _enum_maps()
    updated: List[str] = []
    skipped: List[str] = []

    for key, raw in (data or {}).items():
        try:
            # 扩展名列表 / 同步目标：以逗号分隔的文本整体替换子表
            if key == "media_extensions_text":
                raw_text = "" if raw is None else str(raw)
                exts = [x.strip().lstrip(".").lower()
                        for x in re.split(r"[,，\s]+", raw_text) if x.strip()]
                for row in list(rule.media_extensions or []):
                    session.delete(row)
                rule.media_extensions = [
                    MediaExtensions(rule_id=rule.id, extension=e)
                    for e in dict.fromkeys(exts)
                ]
                for row in rule.media_extensions:
                    session.add(row)
                updated.append(key)
                continue
            if key == "syncs_text":
                raw_text = "" if raw is None else str(raw)
                sync_ids: List[int] = []
                for token in re.split(r"[,，\s]+", raw_text):
                    token = token.strip().lstrip("#")
                    if token.isdigit() and int(token) != rule.id:
                        sync_ids.append(int(token))
                for row in list(rule.rule_syncs or []):
                    session.delete(row)
                rule.rule_syncs = [
                    RuleSync(rule_id=rule.id, sync_rule_id=i)
                    for i in dict.fromkeys(sync_ids)
                ]
                for row in rule.rule_syncs:
                    session.add(row)
                updated.append(key)
                continue
            if key == "ai_provider_id":
                raw_text = "" if raw is None else str(raw).strip()
                try:
                    setattr(rule, key, int(raw_text) if raw_text else None)
                except ValueError:
                    skipped.append(key)
                    continue
                updated.append(key)
                continue
            # 媒体类型子表
            if key.startswith("media."):
                flag = key.split(".", 1)[1]
                if flag not in _MEDIA_FLAGS:
                    skipped.append(key)
                    continue
                media = rule.media_types
                if media is None:
                    media = MediaTypes(rule_id=rule.id)
                    session.add(media)
                    rule.media_types = media
                setattr(media, flag, _coerce(raw, "bool"))
                updated.append(key)
                continue
            # RSS 子表
            if key.startswith("rss."):
                field = key.split(".", 1)[1]
                if field not in _RSS_BOOL_FIELDS:
                    skipped.append(key)
                    continue
                cfg = rule.rss_config
                if cfg is None:
                    cfg = RSSConfig(rule_id=rule.id)
                    session.add(cfg)
                    rule.rss_config = cfg
                setattr(cfg, field, _coerce(raw, "bool"))
                updated.append(key)
                continue
            # 枚举
            if key in enums:
                enum_cls = enums[key]
                try:
                    setattr(rule, key, enum_cls(raw))
                except ValueError:
                    skipped.append(key)
                    continue
                updated.append(key)
                continue
            # 布尔 / 整数 / 字符串
            if key in _BOOL_FIELDS:
                setattr(rule, key, _coerce(raw, "bool"))
                updated.append(key)
            elif key in _INT_FIELDS:
                setattr(rule, key, _coerce(raw, "int"))
                updated.append(key)
            elif key in _STR_FIELDS:
                setattr(rule, key, _coerce(raw, "str"))
                updated.append(key)
            else:
                skipped.append(key)
        except Exception as exc:
            logger.warning("更新规则字段失败 rule=%s field=%s: %s", rule_id, key, exc)
            skipped.append(key)

    session.commit()
    return {"updated": updated, "skipped": skipped, "rule": rule_detail(rule)}


def toggle_rule(session, rule_id: int, enabled: bool) -> bool:
    rule = session.query(ForwardRule).get(rule_id)
    if not rule:
        raise LookupError("规则不存在")
    rule.enable_rule = bool(enabled)
    session.commit()
    return rule.enable_rule


def delete_rule(session, rule_id: int) -> bool:
    """删除规则及其附属数据。

    keywords 与 push_configs 在模型里没有配 cascade（push_config 还写成了
    uselist=False，多地址时只会级联第一条），所以这里显式清理，避免留孤儿行。
    """
    rule = session.query(ForwardRule).get(rule_id)
    if rule is None:
        return False

    session.query(Keyword).filter(Keyword.rule_id == rule_id).delete(synchronize_session=False)
    session.query(PushConfig).filter(PushConfig.rule_id == rule_id).delete(synchronize_session=False)
    try:
        from models.models import ForwardStat
        session.query(ForwardStat).filter(ForwardStat.rule_id == rule_id).delete(synchronize_session=False)
    except Exception:
        logger.debug("清理转发统计失败，忽略", exc_info=True)

    session.delete(rule)
    session.commit()
    return True


# ---------------------------------------------------------------- 推送地址

def add_push_channel(session, rule_id: int, url: str,
                     media_send_mode: str = "Single",
                     name: str = None) -> Dict[str, Any]:
    rule = session.query(ForwardRule).get(rule_id)
    if not rule:
        raise LookupError("规则不存在")
    url = (url or "").strip()
    if not url:
        raise ValueError("地址不能为空")

    existed = (session.query(PushConfig)
               .filter(PushConfig.rule_id == rule_id,
                       PushConfig.push_channel == url).first())
    if existed:
        raise ValueError("该地址已存在")

    clean_name = (name or "").strip() or None
    config = PushConfig(rule_id=rule_id, name=clean_name, push_channel=url,
                        enable_push_channel=True,
                        media_send_mode=media_send_mode or "Single")
    session.add(config)
    session.commit()
    return {"id": config.id, "url": config.push_channel, "name": config.name}


def toggle_push_channel(session, push_id: int, enabled: bool) -> bool:
    config = session.query(PushConfig).get(push_id)
    if not config:
        raise LookupError("推送配置不存在")
    config.enable_push_channel = bool(enabled)
    session.commit()
    return config.enable_push_channel


def delete_push_channel(session, push_id: int) -> None:
    config = session.query(PushConfig).get(push_id)
    if not config:
        raise LookupError("推送配置不存在")
    session.delete(config)
    session.commit()


# ---------------------------------------------------------------- 关键字 / 替换

def add_keyword(session, rule_id: int, keyword: str,
                is_regex: bool = False, is_blacklist: bool = True) -> Dict[str, Any]:
    rule = session.query(ForwardRule).get(rule_id)
    if not rule:
        raise LookupError("规则不存在")
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("关键字不能为空")

    existed = (session.query(Keyword)
               .filter(Keyword.rule_id == rule_id, Keyword.keyword == keyword,
                       Keyword.is_regex == bool(is_regex),
                       Keyword.is_blacklist == bool(is_blacklist)).first())
    if existed:
        raise ValueError("该关键字已存在")

    item = Keyword(rule_id=rule_id, keyword=keyword,
                   is_regex=bool(is_regex), is_blacklist=bool(is_blacklist))
    session.add(item)
    session.commit()
    return {"id": item.id, "keyword": item.keyword}


def delete_keyword(session, keyword_id: int) -> None:
    item = session.query(Keyword).get(keyword_id)
    if not item:
        raise LookupError("关键字不存在")
    session.delete(item)
    session.commit()


def add_replace(session, rule_id: int, pattern: str, content: str = "") -> Dict[str, Any]:
    rule = session.query(ForwardRule).get(rule_id)
    if not rule:
        raise LookupError("规则不存在")
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("匹配式不能为空")
    item = ReplaceRule(rule_id=rule_id, pattern=pattern, content=content or "")
    session.add(item)
    session.commit()
    return {"id": item.id, "pattern": item.pattern}


def delete_replace(session, replace_id: int) -> None:
    item = session.query(ReplaceRule).get(replace_id)
    if not item:
        raise LookupError("替换规则不存在")
    session.delete(item)
    session.commit()


# ---------------------------------------------------------------- 飞书推送汇总

def list_feishu_channels(session) -> List[Dict[str, Any]]:
    """汇总所有规则下配置成飞书 / Lark webhook 的推送地址。

    一条规则可以挂多个推送地址（PushConfig 无唯一约束），这里平铺展示，
    便于在飞书设置页集中看到「哪些规则的哪些群机器人」在被使用。
    """
    from utils.feishu import parse_feishu_url

    items: List[Dict[str, Any]] = []
    for row in session.query(PushConfig).all():
        target = parse_feishu_url(row.push_channel or "")
        if not target:
            continue
        rule = session.query(ForwardRule).get(row.rule_id)
        if rule and rule.source_chat:
            src = rule.source_chat.name or rule.source_chat.telegram_chat_id
            dst = (rule.target_chat.name or rule.target_chat.telegram_chat_id) if rule.target_chat else "推送通道"
            rule_name = f"{src} → {dst}"
        else:
            rule_name = f"规则 #{row.rule_id}"
        items.append({
            "push_id": row.id,
            "rule_id": row.rule_id,
            "rule_name": rule_name,
            "name": (row.name or "").strip(),
            "brand": target.brand,
            "display": target.masked_url,
            "enabled": bool(row.enable_push_channel),
            "media_mode": row.media_send_mode or "Single",
        })
    return items
