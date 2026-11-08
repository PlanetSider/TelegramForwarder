"""飞书 / Lark 应用机器人：在飞书里发命令控制转发器。

与 ``utils/feishu.py`` 的分工：
- ``utils/feishu.py``：自定义机器人 webhook，**只能发**（推送出口）。
- 本模块：应用机器人长连接，**能收也能发**（控制入口）。

使用前需要在飞书开放平台创建「企业自建应用」，完成：
1. 添加「机器人」能力；
2. 权限里开通 ``im:message``（接收与发送消息）；
3. 事件订阅选择「使用长连接接收事件」，并订阅 ``im.message.receive_v1``；
4. 发布版本并把自己加入可用范围。

环境变量（任一品牌配置齐全即启用，不配置则功能静默关闭）：

    FEISHU_APP_ID / FEISHU_APP_SECRET      中国版飞书
    LARK_APP_ID  / LARK_APP_SECRET         Lark 国际版
    FEISHU_ALLOWED_USERS                   允许操作的用户 open_id，逗号分隔

``FEISHU_ALLOWED_USERS`` 为空时拒绝所有操作——第一次给机器人发消息后，
日志里会打印你的 ``open_id``，把它填进变量即可。
"""

import asyncio
import threading
import logging
import os
import re
from typing import Any, Dict, Optional

from models.models import ForwardRule, get_session

logger = logging.getLogger(__name__)

# 应用机器人运行状态：主进程写入，控制台经内部桥只读
_STATUS: Dict[str, Any] = {
    "connected": False,
    "configured": False,
    "app_id": None,
    "error": None,
}


def get_bot_status() -> Dict[str, Any]:
    """返回应用机器人当前状态，供控制台概览展示。"""
    return dict(_STATUS)


def _remember_owner(app_id: str, open_id: str, name: str = "") -> None:
    """把首个给机器人发消息的人记为创建者。

    「直接发送」需要一个 open_id 才能私聊，早期建的机器人没记过，
    让用户随便发一句 /help 就能补上。
    """
    if not app_id or not open_id:
        return
    try:
        from models.models import FeishuBot

        session = get_session()
        try:
            bot = session.query(FeishuBot).filter(FeishuBot.app_id == app_id).first()
            if bot is None:
                logger.info(f"飞书消息来自 {open_id}，但库里没有 app_id={app_id} 的机器人记录")
                return
            if (getattr(bot, "owner_open_id", "") or "").strip():
                return
            bot.owner_open_id = open_id
            if name:
                bot.owner_name = name[:64]
            session.commit()
            logger.info(f"已记录飞书应用机器人创建者: {open_id}")
        finally:
            session.close()
    except Exception as exc:
        logger.debug(f"记录飞书应用机器人创建者失败: {exc}")


# 飞书群聊里 @机器人 会在文本中留下 @_user_1 之类的占位符
_MENTION_RE = re.compile(r"@_user_\d+")

HELP_TEXT = """可用命令：
/list            查看所有转发规则
/rule <ID>       查看单条规则详情
/enable <ID>     启用规则
/disable <ID>    停用规则
/help            显示本帮助"""


def _env(*names: str) -> Optional[str]:
    """按顺序取第一个非空环境变量。"""
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _default_bot_from_db():
    """取数据库里设了默认（或第一个启用）的应用机器人。"""
    try:
        from models.models import get_session, FeishuBot

        session = get_session()
        try:
            return (session.query(FeishuBot)
                    .filter(FeishuBot.enable == True)  # noqa: E712
                    .order_by(FeishuBot.is_default.desc(), FeishuBot.id)
                    .first())
        finally:
            session.close()
    except Exception as exc:
        logger.debug(f"读取数据库里的飞书机器人失败: {exc}")
        return None


def get_feishu_bot_config():
    """读取默认应用机器人配置；未配置时返回 None。

    优先用控制台「通道设置」里保存的机器人（数据库），
    环境变量作为旧部署的兼容兜底。
    """
    platform = (_env("FEISHU_PLATFORM") or "auto").strip().lower()
    app_id = app_secret = None

    bot = _default_bot_from_db()
    if bot is not None:
        app_id = (bot.app_id or "").strip() or None
        app_secret = (bot.app_secret or "").strip() or None
        raw_platform = getattr(bot.platform, "value", None) or str(bot.platform or "")
        if raw_platform in ("feishu", "lark"):
            platform = raw_platform

    if not app_id or not app_secret:
        app_id = _env("FEISHU_APP_ID", "LARK_APP_ID")
        app_secret = _env("FEISHU_APP_SECRET", "LARK_APP_SECRET")

    if not app_id or not app_secret:
        return None

    raw = _env("FEISHU_ALLOWED_USERS") or ""
    allowed = {
        item.strip()
        for item in raw.replace(";", ",").split(",")
        if item.strip()
    }
    if platform not in ("feishu", "lark", "auto"):
        platform = "auto"
    return app_id, app_secret, allowed, platform


def _chat_name(chat) -> str:
    """Chat 记录里的展示名，缺失时退回 telegram_chat_id。"""
    if chat is None:
        return "未知"
    name = (chat.name or "").strip()
    return name or str(chat.telegram_chat_id)


def _format_rule_list(session) -> str:
    rules = session.query(ForwardRule).order_by(ForwardRule.id).all()
    if not rules:
        return "还没有任何转发规则。请先在 Telegram 里用 /bind 绑定源和目标频道。"

    lines = [f"共 {len(rules)} 条转发规则：", ""]
    for rule in rules:
        flag = "✅" if rule.enable_rule else "⛔"
        mode = "机器人" if rule.use_bot else "用户账号"
        lines.append(
            f"{flag} #{rule.id}  "
            f"{_chat_name(rule.source_chat)} → {_chat_name(rule.target_chat)}"
            f"  [{mode}]"
        )
    lines.append("")
    lines.append("用 /rule <ID> 查看详情，/enable、/disable 开关规则。")
    return "\n".join(lines)


def _format_rule_detail(session, rule_id: int) -> str:
    rule = session.query(ForwardRule).get(rule_id)
    if not rule:
        return f"未找到规则 #{rule_id}"

    keyword_count = len(rule.keywords or [])
    replace_count = len(rule.replace_rules or [])

    lines = [
        f"规则 #{rule.id}",
        f"源频道：{_chat_name(rule.source_chat)}（{rule.source_chat.telegram_chat_id if rule.source_chat else '-'}）",
        f"目标频道：{_chat_name(rule.target_chat)}（{rule.target_chat.telegram_chat_id if rule.target_chat else '-'}）",
        f"状态：{'已启用' if rule.enable_rule else '已停用'}",
        f"转发方式：{'机器人' if rule.use_bot else '用户账号'}",
        f"处理模式：{'编辑原消息' if rule.handle_mode.value == 'EDIT' else '转发新消息'}",
        f"关键字：{keyword_count} 条",
        f"替换规则：{replace_count} 条",
        f"AI 处理：{'开启（' + str(rule.ai_model or '默认模型') + '）' if rule.is_ai else '关闭'}",
        f"定时总结：{'开启 ' + str(rule.summary_time) if rule.is_summary else '关闭'}",
        f"RSS：{'开启' if (rule.rss_config and rule.rss_config.enable_rss) else '关闭'}",
    ]
    return "\n".join(lines)


def _set_rule_enabled(session, rule_id: int, enabled: bool) -> str:
    rule = session.query(ForwardRule).get(rule_id)
    if not rule:
        return f"未找到规则 #{rule_id}"
    if rule.enable_rule == enabled:
        return f"规则 #{rule_id} 已经是{'启用' if enabled else '停用'}状态"
    rule.enable_rule = enabled
    session.commit()
    return f"规则 #{rule_id} 已{'启用' if enabled else '停用'}"


def handle_command(text: str) -> str:
    """解析并执行一条命令，返回要回复的文本。"""
    cleaned = _MENTION_RE.sub("", text or "").strip()
    parts = cleaned.split()
    if not parts:
        return HELP_TEXT

    command = parts[0].lstrip("/").split("@")[0].lower()
    args = parts[1:]

    session = get_session()
    try:
        if command in ("help", "h", "帮助"):
            return HELP_TEXT

        if command in ("list", "ls", "列表", "规则"):
            return _format_rule_list(session)

        if command in ("rule", "info", "详情"):
            if not args or not args[0].isdigit():
                return "用法：/rule <规则ID>"
            return _format_rule_detail(session, int(args[0]))

        if command in ("enable", "on", "启用"):
            if not args or not args[0].isdigit():
                return "用法：/enable <规则ID>"
            return _set_rule_enabled(session, int(args[0]), True)

        if command in ("disable", "off", "停用"):
            if not args or not args[0].isdigit():
                return "用法：/disable <规则ID>"
            return _set_rule_enabled(session, int(args[0]), False)

        return f"未知命令：{command}\n\n{HELP_TEXT}"
    except Exception as exc:
        logger.error(f"处理飞书命令失败: {exc}")
        return f"命令执行失败：{exc}"
    finally:
        session.close()


async def run_feishu_bot() -> None:
    """启动飞书应用机器人长连接；未配置或缺少依赖时安静退出。"""
    config = get_feishu_bot_config()
    if not config:
        _STATUS.update(connected=False, configured=False, app_id=None, error=None)
        return

    try:
        from lark_oapi.channel import FeishuChannel
    except ImportError:
        _STATUS.update(connected=False, configured=True, error="未安装 lark-oapi")
        logger.error("未安装 lark-oapi，飞书命令功能不可用（pip install lark-oapi）")
        return

    app_id, app_secret, _, platform = config
    _STATUS.update(configured=True, app_id=app_id, error=None)

    # 飞书（中国）与 Lark（国际）是两套域名，连错会报 1000040351 Incorrect domain name。
    # 自动探测时先试飞书，失败再试 Lark。
    try:
        from lark_oapi import FEISHU_DOMAIN, LARK_DOMAIN
        if platform == "lark":
            domains = [LARK_DOMAIN]
        elif platform == "feishu":
            domains = [FEISHU_DOMAIN]
        else:
            domains = [FEISHU_DOMAIN, LARK_DOMAIN]
    except Exception:
        domains = [None]

    channel = None

    async def on_message(message) -> None:
        try:
            sender_id = getattr(message, "sender_id", None)
            text = getattr(message, "content_text", "") or ""

            # 顺手记录创建者，让「直接发送」对早期建的机器人也能用
            if sender_id:
                _remember_owner(app_id, sender_id,
                                getattr(message, "sender_name", "") or "")

            if not text.strip():
                return

            # 白名单实时读取：在控制台「飞书」页保存后立即生效，不必重启
            _, _, allowed_users, _ = get_feishu_bot_config() or (None, None, set(), "feishu")

            if not allowed_users:
                logger.warning(
                    f"飞书命令未配置白名单，已忽略来自 {sender_id} 的消息；"
                    f"把你的 open_id 填到 FEISHU_ALLOWED_USERS（或控制台「飞书」页）即可启用"
                )
                return

            if sender_id not in allowed_users:
                logger.warning(
                    f"拒绝未授权的飞书用户 {sender_id} 的命令；"
                    f"如需授权，请把它加入 FEISHU_ALLOWED_USERS"
                )
                return

            logger.info(f"收到飞书命令（来自 {sender_id}）: {text[:100]}")
            reply = await asyncio.to_thread(handle_command, text)
            await channel.send(message.chat_id, {"text": reply})
        except Exception as exc:
            logger.error(f"处理飞书消息时出错: {exc}")

    logger.info(f"飞书应用机器人正在连接（app_id={app_id}）")
    last_error = None
    for idx, domain in enumerate(domains):
        try:
            channel = FeishuChannel(app_id=app_id, app_secret=app_secret, domain=domain)
        except Exception as exc:
            last_error = exc
            continue

        channel.on("message", on_message)
        try:
            await channel.connect()
        except Exception as exc:
            last_error = exc
            if idx + 1 < len(domains):
                logger.warning(
                    f"飞书长连接在 {domain or '默认域名'} 失败（{exc}），尝试另一个平台域名"
                )
                continue
            break
        else:
            _STATUS.update(connected=True, error=None)
            logger.info("飞书应用机器人已连接，等待命令")
            return

    _STATUS.update(connected=False, error=f"连接中断: {last_error}")
    logger.error(f"飞书应用机器人连接中断: {last_error}")


def _bot_main() -> None:
    """线程入口。

    ``lark_oapi`` 在模块导入时就执行 ``asyncio.get_event_loop()`` 并把结果缓存
    成模块级变量，之后 ``start()`` 用 ``loop.run_until_complete`` 启动。若在已有
    事件循环（主进程的 asyncio loop）里导入，缓存到的就是正在运行的循环，启动时
    必然抛 "This event loop is already running"。所以必须先在当前线程导入，
    再交给 asyncio.run。
    """
    try:
        import lark_oapi.channel  # noqa: F401
    except ImportError:
        _STATUS.update(connected=False, configured=True, error="未安装 lark-oapi")
        logger.error("未安装 lark-oapi，飞书命令功能不可用（pip install lark-oapi）")
        return

    try:
        asyncio.run(run_feishu_bot())
    except Exception as exc:
        _STATUS.update(connected=False, error=f"连接中断: {exc}")
        logger.error(f"飞书应用机器人线程异常退出: {exc}")


def start_feishu_bot() -> threading.Thread:
    """在独立线程里跑飞书长连接，不占用主事件循环。"""
    t = threading.Thread(target=_bot_main, name="feishu-bot", daemon=True)
    t.start()
    return t
