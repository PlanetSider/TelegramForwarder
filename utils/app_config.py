"""配置项定义与读写：`.env` 的 WebUI 替代实现。

设计要点
--------
1. **键名与代码里 `os.getenv` 的键完全一致**。保存时同时写入数据库与本进程
   `os.environ`，存量代码一行都不用改。
2. **数据库是唯一真相源**。`utils.bootstrap` 在进程启动、import 业务模块之前
   把值注入 `os.environ`，因此 `utils/constants.py` 里那些模块级常量能读到新值。
3. **有默认值的键可以不填**。`get_current` 返回「DB 值 else 默认值」，
   空字符串一律视为未配置。

不在此列的键（因此无法在 WebUI 配置）：
    RSS_HOST / RSS_PORT      → 部署拓扑，交给 docker-compose 的 ports
    BRIDGE_HOST/PORT/TOKEN   → 主进程与子进程之的内部通信，程序自动管理
    RSS_SECRET_KEY           → JWT 签名密钥，自动生成并持久化
    DATABASE_URL / MAX_MEDIA_SIZE → 全仓库无调用点，已废弃
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from models.models import AppSetting, get_session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingSpec:
    """一条配置项的元数据。"""
    key: str
    label: str
    group: str
    kind: str = "text"          # text | password | int | bool | select | time | textarea
    default: str = ""
    help: str = ""
    secret: bool = False        # 界面上打码显示
    restart: bool = False       # 改动后需要重启容器才生效
    options: Tuple[Tuple[str, str], ...] = ()   # (值, 显示名)
    placeholder: str = ""


# 分组顺序即界面顺序
GROUPS: List[Tuple[str, str, str]] = [
    ("telegram", "Telegram", "账号凭据与管理员"),
    ("ai", "AI 服务", "各家模型的密钥与接入地址"),
    ("behavior", "默认行为", "新建规则时的默认取值"),
    ("switch", "功能开关", "整体能力的启用与停用"),
    ("ui", "界面与分页", "列表每页数量、键盘行数"),
    ("timing", "超时与批量", "延时、超时与并发参数"),
    ("storage", "存储路径", "容器内数据与媒体目录"),
    ("external", "外部服务", "反代地址与联动服务"),
    ("feishu", "飞书", "应用机器人、命令白名单与群推送"),
]

_YES_NO = (("true", "开启"), ("false", "关闭"))


SETTINGS: List[SettingSpec] = [
    # ---------------------------------------------------------------- Telegram
    SettingSpec("API_ID", "API ID", "telegram", "int",
                help="my.telegram.org → API development tools 里获取，纯数字"),
    SettingSpec("API_HASH", "API Hash", "telegram", "password", secret=True,
                help="与 API ID 配对的 32 位字符串"),
    SettingSpec("BOT_TOKEN", "机器人 Token", "telegram", "password", secret=True,
                help="向 @BotFather 申请，形如 123456:ABC-DEF..."),
    SettingSpec("PHONE_NUMBER", "手机号", "telegram", "text",
                placeholder="+8613800138000",
                help="监听消息所用的用户账号，需带国家码"),
    SettingSpec("USER_ID", "用户 ID", "telegram", "int",
                help="首次登录成功后自动填入；也可手工填 @userinfobot 给出的数字"),
    SettingSpec("ADMINS", "额外管理员", "telegram", "text",
                help="逗号分隔的用户 ID；只有这些人和机器人管理员能使用命令"),

    # ---------------------------------------------------------------- AI
    SettingSpec("AI_BASE_URL", "接口地址", "ai", "text",
                default="https://api.openai.com/v1",
                help="OpenAI 兼容接口的根地址，例如 https://api.deepseek.com/v1、"
                     "http://127.0.0.1:11434/v1、https://your-gateway/v1"),
    SettingSpec("AI_API_KEY", "API Key", "ai", "password", secret=True,
                help="对应服务商的密钥；本机 Ollama 这类免鉴权的可留空"),
    SettingSpec("AI_MODEL", "默认模型", "ai", "model",
                help="点右侧「获取模型」从接口读取列表后选择，也可以直接手填模型 ID"),
    SettingSpec("AI_REASONING", "思考模式", "ai", "select",
                options=(("", "跟随服务商默认"), ("low", "轻（快）"),
                         ("medium", "中"), ("high", "深（慢）")),
                help="对应 reasoning_effort 参数，只有部分模型支持，不支持的会被忽略"),

    # ---------------------------------------------------------------- 默认行为
    SettingSpec("PROJECT_NAME", "项目名称", "behavior", "text",
                default="TG Forwarder RSS", help="RSS 订阅源标题后缀"),
    SettingSpec("DEFAULT_TIMEZONE", "默认时区", "behavior", "text",
                default="Asia/Shanghai",
                help="IANA 时区名，影响消息时间显示与总结时间"),
    SettingSpec("DEFAULT_AI_MODEL", "默认 AI 模型", "behavior", "text",
                default="gpt-4o", help="新建规则启用 AI 时预选的模型"),
    SettingSpec("DEFAULT_AI_PROMPT", "默认 AI 提示词", "behavior", "textarea",
                default="请尊重原意，保持原有格式不变，用简体中文重写下面的内容："),
    SettingSpec("DEFAULT_SUMMARY_PROMPT", "默认总结提示词", "behavior", "textarea",
                default="请总结以下频道/群组24小时内的消息。"),
    SettingSpec("DEFAULT_SUMMARY_TIME", "默认总结时间", "behavior", "time",
                default="07:00", placeholder="07:00"),
    SettingSpec("DEFAULT_MAX_MEDIA_SIZE", "默认媒体大小上限", "behavior", "int",
                default="10", help="单位 MB，新建规则时的预选值"),

    # ---------------------------------------------------------------- 功能开关
    SettingSpec("RSS_ENABLED", "启用 RSS 订阅源", "switch", "bool", default="false",
                help="关闭后不再生成 RSS，也不往 RSS 面板推送条目；WebUI 本身始终可用"),
    SettingSpec("UFB_ENABLED", "启用 UFB 联动", "switch", "bool", default="false"),
    SettingSpec("USER_MESSAGE_DELETE_ENABLE", "允许删除用户消息", "switch", "bool",
                default="false", help="开启后机器人可以删除自己发出的提示消息"),

    # ---------------------------------------------------------------- 界面与分页
    SettingSpec("RULES_PER_PAGE", "规则每页条数", "ui", "int", default="20"),
    SettingSpec("PUSH_CHANNEL_PER_PAGE", "推送渠道每页条数", "ui", "int", default="10"),
    SettingSpec("KEYWORDS_PER_PAGE", "关键字每页条数", "ui", "int", default="50"),
    SettingSpec("AI_MODELS_PER_PAGE", "模型每页条数", "ui", "int", default="10"),
    SettingSpec("SUMMARY_TIME_ROWS", "总结时间 行数", "ui", "int", default="10"),
    SettingSpec("SUMMARY_TIME_COLS", "总结时间 列数", "ui", "int", default="6"),
    SettingSpec("DELAY_TIME_ROWS", "延时选项 行数", "ui", "int", default="10"),
    SettingSpec("DELAY_TIME_COLS", "延时选项 列数", "ui", "int", default="6"),
    SettingSpec("MEDIA_SIZE_ROWS", "媒体大小 行数", "ui", "int", default="10"),
    SettingSpec("MEDIA_SIZE_COLS", "媒体大小 列数", "ui", "int", default="6"),
    SettingSpec("MEDIA_EXTENSIONS_ROWS", "扩展名 行数", "ui", "int", default="6"),
    SettingSpec("MEDIA_EXTENSIONS_COLS", "扩展名 列数", "ui", "int", default="6"),

    # ---------------------------------------------------------------- 超时与批量
    SettingSpec("BOT_MESSAGE_DELETE_TIMEOUT", "机器人消息自动删除", "timing", "int",
                default="300", help="单位秒；0 表示不自动删除"),
    SettingSpec("CHAT_UPDATE_TIME", "会话名称刷新间隔", "timing", "int",
                default="3600", help="单位秒"),
    SettingSpec("SUMMARY_BATCH_SIZE", "总结分批大小", "timing", "int", default="20"),
    SettingSpec("SUMMARY_BATCH_DELAY", "总结分批间隔", "timing", "int",
                default="2", help="单位秒"),

    # ---------------------------------------------------------------- 存储路径
    SettingSpec("RSS_DATA_PATH", "RSS 数据目录", "storage", "text",
                default="./rss/data", restart=True,
                help="容器内路径，已由 compose 挂载；改动后需重启"),
    SettingSpec("RSS_MEDIA_PATH", "RSS 媒体目录", "storage", "text",
                default="./rss/media", restart=True),

    # ---------------------------------------------------------------- 外部服务
    SettingSpec("RSS_MEDIA_BASE_URL", "RSS 媒体反代地址", "external", "text",
                help="留空则用请求头自动推导；反代到不同域名时填写"),
    SettingSpec("UFB_SERVER_URL", "UFB 服务地址", "external", "text",
                help="启用 UFB 联动时必填"),
    SettingSpec("UFB_TOKEN", "UFB Token", "external", "password", secret=True),

    # ---------------------------------------------------------------- 飞书
    SettingSpec("FEISHU_APP_ID", "应用 App ID", "feishu", "text", restart=True,
                help="开放平台自建应用的 App ID；与 App Secret 一起填写后才启用长连接命令控制"),
    SettingSpec("FEISHU_APP_SECRET", "应用 App Secret", "feishu", "password", secret=True, restart=True,
                help="与 App ID 配对，用于建立长连接；保存后需重启容器才会重连"),
    SettingSpec("FEISHU_PLATFORM", "开放平台", "feishu", "select", default="feishu",
                options=(("feishu", "飞书（中国版 open.feishu.cn）"),
                         ("lark", "Lark 国际版（open.larksuite.com）")),
                restart=True,
                help="决定长连接连向哪个域名；应用在飞书开放平台创建就选飞书，"
                     "在 Lark 开放平台创建就选 Lark。选错会报 1000040351 Incorrect domain name"),
    SettingSpec("FEISHU_ALLOWED_USERS", "命令白名单", "feishu", "text",
                help="允许下发命令的 open_id，多个用逗号或分号分隔；留空表示拒绝所有命令，"
                     "第一次可先留空，从容器日志里取到来访者的 open_id 再填回"),
]

_BY_KEY: Dict[str, SettingSpec] = {s.key: s for s in SETTINGS}


def specs() -> List[SettingSpec]:
    return list(SETTINGS)


def spec(key: str) -> Optional[SettingSpec]:
    return _BY_KEY.get(key)


def grouped() -> List[Tuple[str, str, str, List[SettingSpec]]]:
    """按分组返回 (key, 标题, 说明, 配置项列表)，顺序同 GROUPS。"""
    out = []
    for gid, title, desc in GROUPS:
        items = [s for s in SETTINGS if s.group == gid]
        if items:
            out.append((gid, title, desc, items))
    return out


# ---------------------------------------------------------------- 读取

def get_from_db(session) -> Dict[str, str]:
    """取出数据库里已保存的值（不含默认值）。"""
    rows = session.query(AppSetting).all()
    return {r.key: (r.value or "") for r in rows}


def get_current(session) -> Dict[str, str]:
    """DB 值优先，缺失时回落到默认值。"""
    stored = get_from_db(session)
    current: Dict[str, str] = {}
    for s in SETTINGS:
        value = stored.get(s.key, "")
        current[s.key] = value if value != "" else s.default
    return current


def mask(value: str, keep: int = 4) -> str:
    """把密钥打码：保留头尾各 keep 位。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 8}{value[-keep:]}"


def as_display(session) -> List[Dict[str, Any]]:
    """给模板用的扁平列表，敏感值打码并标记是否已配置。"""
    current = get_current(session)
    out = []
    for s in SETTINGS:
        raw = current.get(s.key, "")
        out.append({
            "key": s.key,
            "label": s.label,
            "group": s.group,
            "kind": s.kind,
            "help": s.help,
            "secret": s.secret,
            "restart": s.restart,
            "options": s.options,
            "placeholder": s.placeholder,
            "value": "" if s.secret else raw,
            "display": mask(raw) if s.secret else raw,
            "configured": bool(raw),
        })
    return out


# ---------------------------------------------------------------- 校验

def validate(spec_obj: SettingSpec, value: str) -> Tuple[bool, str]:
    """返回 (是否合法, 错误信息)。空值代表「清除该项」，一律允许。"""
    text = (value or "").strip()
    if text == "":
        return True, ""

    if spec_obj.kind == "int":
        try:
            number = int(text)
        except ValueError:
            return False, f"{spec_obj.label} 必须是整数"
        if number < 0:
            return False, f"{spec_obj.label} 不能为负数"
        return True, ""

    if spec_obj.kind == "bool":
        if text.lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            return False, f"{spec_obj.label} 只能是 true 或 false"
        return True, ""

    if spec_obj.kind == "time":
        import re
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
            return False, f"{spec_obj.label} 需要 HH:MM 格式"
        return True, ""

    if spec_obj.key == "DEFAULT_TIMEZONE":
        try:
            import pytz
            pytz.timezone(text)
        except Exception:
            return False, "时区名无法识别，例如 Asia/Shanghai"
        return True, ""

    return True, ""


# ---------------------------------------------------------------- 写入

def save(session, updates: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """保存一批配置。

    返回 (已保存的键, 错误信息列表)。写入数据库的同时更新本进程的 os.environ，
    使当前进程立刻生效；已启动的子进程需要重启容器才会读到新值。
    """
    saved: List[str] = []
    errors: List[str] = []

    for key, raw in updates.items():
        s = _BY_KEY.get(key)
        if s is None:
            continue

        value = (raw or "").strip()
        # 敏感字段的掩码值意味着「未修改」，跳过
        if s.secret and "*" * 8 in value:
            continue

        ok, message = validate(s, value)
        if not ok:
            errors.append(message)
            continue

        try:
            row = session.query(AppSetting).filter(AppSetting.key == key).first()
            if row is None:
                row = AppSetting(key=key, value=value)
                session.add(row)
            else:
                row.value = value
            session.flush()
        except Exception as exc:
            errors.append(f"{s.label} 保存失败: {exc}")
            continue

        # 同步进程内环境变量：当前进程立刻生效
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
        saved.append(key)

    if errors:
        session.rollback()
        return [], errors

    session.commit()
    return saved, []


def is_telegram_configured(session) -> bool:
    """判断是否已具备连接 Telegram 的最小配置。"""
    current = get_current(session)
    return all(current.get(k, "").strip() for k in
               ("API_ID", "API_HASH", "BOT_TOKEN", "PHONE_NUMBER"))


def read_credentials(session) -> Dict[str, str]:
    """取出连接 Telegram 所需的四项凭据。"""
    current = get_current(session)
    return {k: current.get(k, "").strip()
            for k in ("API_ID", "API_HASH", "BOT_TOKEN", "PHONE_NUMBER")}
