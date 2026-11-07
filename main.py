"""Telegram 消息转发器入口。

启动顺序是被刻意设计的，改动前请先看这里：

1. ``bootstrap_runtime_env()`` 把数据库里保存的配置写进 ``os.environ``。
   它必须是**第一条**执行语句——``utils/constants.py``、``models/models.py``
   等模块在 import 期间就会读取环境变量，晚一步注入就会读到默认值。
2. 起内部桥 + WebUI。这一步不依赖 Telegram 凭据，因此首次部署、
   凭据填错、会话失效时控制台都仍然可用。
3. 只有凭据齐全且会话已授权，才启动 Telegram 客户端与转发链路。

配置来源优先级：数据库 > 环境变量（``load_dotenv`` 用的是 override=False）。
"""
from utils.bootstrap import bootstrap_runtime_env

# 必须早于任何业务模块 import
bootstrap_runtime_env()

from telethon import TelegramClient, types
from telethon.tl.types import BotCommand
from telethon.tl.functions.bots import SetBotCommandsRequest
from models.models import init_db, get_session
from message_listener import setup_listeners
import os
import asyncio
import logging
import uvicorn
import multiprocessing
from models.db_operations import DBOperations
from scheduler.summary_scheduler import SummaryScheduler
from scheduler.chat_updater import ChatUpdater
from handlers.bot_handler import send_welcome_message
from rss.main import app as rss_app
from utils.log_config import setup_logging
from utils.constants import WEB_HOST, WEB_PORT
from utils.feishu_bot import start_feishu_bot
from utils.main_bridge import ensure_token, start_bridge
from utils import app_config
from utils.telegram_login import STATUS_AUTHORIZED, login_manager

# 设置Docker日志的默认配置，如果docker-compose.yml中没有配置日志选项将使用这些值
os.environ.setdefault('DOCKER_LOG_MAX_SIZE', '10m')
os.environ.setdefault('DOCKER_LOG_MAX_FILE', '3')

# 设置日志配置
setup_logging()

logger = logging.getLogger(__name__)

# 创建 DBOperations 实例
db_ops = None

scheduler = None
chat_updater = None
rss_process = None

# 控制台通过内部桥读取这份状态，避免只显示笼统的“离线”
TELEGRAM_STATUS = {
    "configured": False,
    "missing": [],
    "authorized": False,
    "started": False,
    "error": None,
}

# 上一次打印过的缺失项，避免重试循环里每 5 秒刷一条同样的日志
_LAST_MISSING = []

# 客户端在拿到凭据之后才构造，模块级先占位：
# utils/common.get_user_client() 依赖 main 模块上的这两个名字存在。
user_client = None
bot_client = None


async def init_db_ops():
    """初始化 DBOperations 实例"""
    global db_ops
    if db_ops is None:
        db_ops = await DBOperations.create()
    return db_ops


# 创建文件夹
os.makedirs('./sessions', exist_ok=True)
os.makedirs('./temp', exist_ok=True)


# 清空./temp文件夹
def clear_temp_dir():
    for file in os.listdir('./temp'):
        os.remove(os.path.join('./temp', file))


# 初始化数据库（建表 + 迁移，不依赖任何凭据）
engine = init_db()


def run_rss_server(host: str, port: int):
    """在子进程中运行控制台服务"""
    uvicorn.run(
        rss_app,
        host=host,
        port=port
    )


def start_webui():
    """启动控制台子进程。

    控制台是**无条件启动**的：首次部署时数据库里还没有任何凭据，
    正是通过它来完成配置。控制台与 RSS 面板是同一个服务、同一个端口，
    宿主侧端口由 compose 映射（默认 9804 -> 8000）。
    """
    global rss_process
    host = WEB_HOST
    port = int(WEB_PORT)
    rss_process = multiprocessing.Process(
        target=run_rss_server,
        args=(host, port),
        daemon=True,
    )
    rss_process.start()
    logger.info("控制台已启动: http://%s:%s/admin（首次访问请先注册管理员账号）", host, port)
    return rss_process


def build_clients():
    """按当前配置构造 Telethon 客户端。"""
    global user_client, bot_client
    user_client = TelegramClient('./sessions/user', int(os.getenv('API_ID')), os.getenv('API_HASH'))
    bot_client = TelegramClient('./sessions/bot', int(os.getenv('API_ID')), os.getenv('API_HASH'))
    return user_client, bot_client


async def user_session_authorized() -> bool:
    """用一个短连接检查会话文件是否已授权。

    直接调用 ``client.start(phone=...)`` 在未授权时会阻塞等待终端输入验证码，
    在容器里就是一个永不返回的挂起。先检查，避免这种情况。
    """
    try:
        probe = TelegramClient('./sessions/user', int(os.getenv('API_ID')), os.getenv('API_HASH'))
    except (TypeError, ValueError):
        return False
    try:
        await probe.connect()
        return await probe.is_user_authorized()
    except Exception as e:
        logger.debug("检查 Telegram 会话状态失败: %s", e)
        return False
    finally:
        try:
            await probe.disconnect()
        except Exception:
            pass


async def start_telegram() -> bool:
    """启动 Telegram 相关的一切；条件不满足时安静返回 False，由外层重试。"""
    global db_ops, scheduler, chat_updater, user_client, bot_client

    if TELEGRAM_STATUS.get("started"):
        return True

    db_ops = db_ops or await DBOperations.create()

    session = get_session()
    try:
        creds = app_config.read_credentials(session)
    finally:
        session.close()

    missing = [name for name, value in creds.items() if not value]
    TELEGRAM_STATUS["configured"] = not missing
    TELEGRAM_STATUS["missing"] = missing
    if missing:
        global _LAST_MISSING

        TELEGRAM_STATUS["error"] = "缺少配置：" + "、".join(missing)
        if missing != _LAST_MISSING:
            _LAST_MISSING = list(missing)
            logger.info("尚未配置 %s，Telegram 链路待配置", '、'.join(missing))
        return False

    authorized = await user_session_authorized()
    TELEGRAM_STATUS["authorized"] = authorized
    if not authorized:
        TELEGRAM_STATUS["error"] = "用户会话未授权，请在控制台完成验证码登录"
        logger.info("Telegram 用户会话未授权，等待在控制台完成登录")
        return False

    try:
        build_clients()

        await user_client.start(phone=creds.get('PHONE_NUMBER'))
        me_user = await user_client.get_me()
        logger.info("用户客户端已启动: %s (@%s)", me_user.first_name, me_user.username)

        await bot_client.start(bot_token=creds.get('BOT_TOKEN'))
        me_bot = await bot_client.get_me()
        logger.info("机器人客户端已启动: %s (@%s)", me_bot.first_name, me_bot.username)

        await setup_listeners(user_client, bot_client)
        await register_bot_commands(bot_client)

        scheduler = SummaryScheduler(user_client, bot_client)
        await scheduler.start()

        chat_updater = ChatUpdater(user_client)
        await chat_updater.start()

        await send_welcome_message(bot_client)

        TELEGRAM_STATUS.update(started=True, error=None)
        return True
    except Exception as e:
        TELEGRAM_STATUS["error"] = f"启动失败：{e}"
        logger.error("启动 Telegram 链路失败: %s", e)
        logger.exception(e)
        return False


async def start_clients():
    """总编排：先让控制台可用，再按配置决定是否连 Telegram。"""
    global db_ops
    db_ops = await DBOperations.create()

    # 内部桥的 token 必须在 fork 子进程之前生成，子进程才能读到同一份
    ensure_token()
    try:
        await start_bridge()
    except Exception as e:
        logger.warning("启动内部桥失败（控制台将无法显示 Telegram 状态）: %s", e)

    # 控制台无条件启动
    start_webui()

    # 飞书长连接跑在独立线程里：lark_oapi 在导入时就绑定事件循环，放进主循环会崩
    start_feishu_bot()

    try:
        notified = False
        while not await start_telegram():
            if not notified:
                logger.warning("=" * 60)
                logger.warning("Telegram 尚未就绪：%s", TELEGRAM_STATUS.get("error"))
                logger.warning("请打开控制台 /admin 注册账号，按向导填写凭据并完成验证码登录。")
                logger.warning("补齐后会自动接上，无需重启容器。")
                logger.warning("=" * 60)
                notified = True
            await asyncio.sleep(5)

        logger.info("Telegram 链路已就绪，开始监听消息")
        await asyncio.gather(
            user_client.run_until_disconnected(),
            bot_client.run_until_disconnected()
        )
    finally:
        if db_ops and hasattr(db_ops, 'close'):
            await db_ops.close()
        if scheduler:
            scheduler.stop()
        if chat_updater:
            chat_updater.stop()
        if rss_process and rss_process.is_alive():
            rss_process.terminate()
            rss_process.join()


async def register_bot_commands(bot):
    """注册机器人命令"""
    # # 先清空现有命令
    # try:
    #     await bot(SetBotCommandsRequest(
    #         scope=types.BotCommandScopeDefault(),
    #         lang_code='',
    #         commands=[]  # 空列表清空所有命令
    #     ))
    #     logger.info('已清空现有机器人命令')
    # except Exception as e:
    #     logger.error(f'清空机器人命令时出错: {str(e)}')

    commands = [
        # 基础命令
        BotCommand(
            command='start',
            description='开始使用'
        ),
        BotCommand(
            command='help',
            description='查看帮助'
        ),
        # 绑定和设置
        BotCommand(
            command='bind',
            description='绑定源聊天'
        ),
        BotCommand(
            command='settings',
            description='管理转发规则'
        ),
        BotCommand(
            command='switch',
            description='切换当前需要设置的聊天规则'
        ),
        # 关键字管理
        BotCommand(
            command='add',
            description='添加关键字'
        ),
        BotCommand(
            command='add_regex',
            description='添加正则关键字'
        ),
        BotCommand(
            command='add_all',
            description='添加普通关键字到所有规则'
        ),
        BotCommand(
            command='add_regex_all',
            description='添加正则表达式到所有规则'
        ),
        BotCommand(
            command='list_keyword',
            description='列出所有关键字'
        ),
        BotCommand(
            command='remove_keyword',
            description='删除关键字'
        ),
        BotCommand(
            command='remove_keyword_by_id',
            description='按ID删除关键字'
        ),
        BotCommand(
            command='remove_all_keyword',
            description='删除当前频道绑定的所有规则的指定关键字'
        ),
        # 替换规则管理
        BotCommand(
            command='replace',
            description='添加替换规则'
        ),
        BotCommand(
            command='replace_all',
            description='添加替换规则到所有规则'
        ),
        BotCommand(
            command='list_replace',
            description='列出所有替换规则'
        ),
        BotCommand(
            command='remove_replace',
            description='删除替换规则'
        ),
        # 导入导出功能
        BotCommand(
            command='export_keyword',
            description='导出当前规则的关键字'
        ),
        BotCommand(
            command='export_replace',
            description='导出当前规则的替换规则'
        ),
        BotCommand(
            command='import_keyword',
            description='导入普通关键字'
        ),
        BotCommand(
            command='import_regex_keyword',
            description='导入正则表达式关键字'
        ),
        BotCommand(
            command='import_replace',
            description='导入替换规则'
        ),
        # UFB相关功能
        BotCommand(
            command='ufb_bind',
            description='绑定ufb域名'
        ),
        BotCommand(
            command='ufb_unbind',
            description='解绑ufb域名'
        ),
        BotCommand(
            command='ufb_item_change',
            description='切换ufb同步配置类型'
        ),
        BotCommand(
            command='clear_all_keywords',
            description='清除当前规则的所有关键字'
        ),
        BotCommand(
            command='clear_all_keywords_regex',
            description='清除当前规则的所有正则关键字'
        ),
        BotCommand(
            command='clear_all_replace',
            description='清除当前规则的所有替换规则'
        ),
        BotCommand(
            command='copy_keywords',
            description='复制参数规则的关键字到当前规则'
        ),
        BotCommand(
            command='copy_keywords_regex',
            description='复制参数规则的正则关键字到当前规则'
        ),
        BotCommand(
            command='copy_replace',
            description='复制参数规则的替换规则到当前规则'
        ),
        BotCommand(
            command='copy_rule',
            description='复制参数规则到当前规则'
        ),
        BotCommand(
            command='changelog',
            description='查看更新日志'
        ),
        BotCommand(
            command='list_rule',
            description='列出所有转发规则'
        ),
        BotCommand(
            command='delete_rule',
            description='删除转发规则'
        ),
        BotCommand(
            command='delete_rss_user',
            description='删除RSS用户'
        ),


        # BotCommand(
        #     command='clear_all',
        #     description='慎用！清空所有数据'
        # ),
    ]

    try:
        result = await bot(SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code='',  # 空字符串表示默认语言
            commands=commands
        ))
        if result:
            logger.info('已成功注册机器人命令')
        else:
            logger.error('注册机器人命令失败')
    except Exception as e:
        logger.error(f'注册机器人命令时出错: {str(e)}')


if __name__ == '__main__':
    # 运行事件循环
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(start_clients())
    except KeyboardInterrupt:
        print("正在关闭客户端...")
    finally:
        loop.close()