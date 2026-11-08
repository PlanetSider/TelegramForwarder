import os
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    """把环境变量解析为布尔值（大小写不敏感，支持 1/true/yes/on）。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')

# 目录配置
BASE_DIR = Path(__file__).parent.parent
TEMP_DIR = os.path.join(BASE_DIR, 'temp')

# 控制台（WebUI）和 RSS 面板是同一个 FastAPI 服务、同一个端口：
# /admin（控制台）、/rss（面板）、/rss/feed（订阅输出）挂在同一个 app 上。
# 这两个常量历史上叫 RSS_HOST / RSS_PORT（因为最初只有 RSS 面板），
# 现在统一用 WEB_* 表达真实含义，RSS_* 保留为别名，避免改动大量既有引用。
WEB_HOST = os.getenv('WEB_HOST') or os.getenv('RSS_HOST') or '0.0.0.0'
WEB_PORT = os.getenv('WEB_PORT') or os.getenv('RSS_PORT') or '8000'
RSS_HOST = WEB_HOST
RSS_PORT = WEB_PORT

# RSS基础URL，如果未设置，则使用请求的URL
RSS_BASE_URL = os.environ.get('RSS_BASE_URL', None)

# RSS媒体文件的基础URL，用于生成媒体链接，如果未设置，则使用请求的URL
RSS_MEDIA_BASE_URL = os.getenv('RSS_MEDIA_BASE_URL', '')

RSS_ENABLED = env_bool('RSS_ENABLED')

RULES_PER_PAGE = int(os.getenv('RULES_PER_PAGE', 20))

PUSH_CHANNEL_PER_PAGE = int(os.getenv('PUSH_CHANNEL_PER_PAGE', 10))

DEFAULT_TIMEZONE = os.getenv('DEFAULT_TIMEZONE', 'Asia/Shanghai')
PROJECT_NAME = os.getenv('PROJECT_NAME', 'TG Forwarder RSS')
# RSS相关路径配置
RSS_MEDIA_PATH = os.getenv('RSS_MEDIA_PATH', './rss/media')

# 转换为绝对路径
RSS_MEDIA_DIR = os.path.abspath(os.path.join(BASE_DIR, RSS_MEDIA_PATH) 
                              if not os.path.isabs(RSS_MEDIA_PATH) 
                              else RSS_MEDIA_PATH)

# RSS数据路径
RSS_DATA_PATH = os.getenv('RSS_DATA_PATH', './rss/data')
RSS_DATA_DIR = os.path.abspath(os.path.join(BASE_DIR, RSS_DATA_PATH)
                            if not os.path.isabs(RSS_DATA_PATH)
                            else RSS_DATA_PATH)

# 默认AI模型
DEFAULT_AI_MODEL = os.getenv('DEFAULT_AI_MODEL', 'gpt-4o')
# 默认AI总结提示词
DEFAULT_SUMMARY_PROMPT = os.getenv('DEFAULT_SUMMARY_PROMPT', '请总结以下频道/群组24小时内的消息。')
# 默认AI提示词
DEFAULT_AI_PROMPT = os.getenv('DEFAULT_AI_PROMPT', '请尊重原意，保持原有格式不变，用简体中文重写下面的内容：')

# 分页配置
MODELS_PER_PAGE = int(os.getenv('AI_MODELS_PER_PAGE', 10))
KEYWORDS_PER_PAGE = int(os.getenv('KEYWORDS_PER_PAGE', 50))

# 按钮布局配置
SUMMARY_TIME_ROWS = int(os.getenv('SUMMARY_TIME_ROWS', 10))
SUMMARY_TIME_COLS = int(os.getenv('SUMMARY_TIME_COLS', 6))

DELAY_TIME_ROWS = int(os.getenv('DELAY_TIME_ROWS', 10))
DELAY_TIME_COLS = int(os.getenv('DELAY_TIME_COLS', 6))

MEDIA_SIZE_ROWS = int(os.getenv('MEDIA_SIZE_ROWS', 10))
MEDIA_SIZE_COLS = int(os.getenv('MEDIA_SIZE_COLS', 6))

MEDIA_EXTENSIONS_ROWS = int(os.getenv('MEDIA_EXTENSIONS_ROWS', 6))
MEDIA_EXTENSIONS_COLS = int(os.getenv('MEDIA_EXTENSIONS_COLS', 6))

LOG_MAX_SIZE_MB = 10
LOG_BACKUP_COUNT = 3

# 默认消息删除时间 (秒)
BOT_MESSAGE_DELETE_TIMEOUT = int(os.getenv("BOT_MESSAGE_DELETE_TIMEOUT", 300))

# 自动删除用户发送的指令消息
USER_MESSAGE_DELETE_ENABLE = env_bool("USER_MESSAGE_DELETE_ENABLE")

# 是否启用UFB
UFB_ENABLED = env_bool("UFB_ENABLED")

# 菜单标题
AI_SETTINGS_TEXT = """
当前AI提示词：

`{ai_prompt}`

当前总结提示词：

`{summary_prompt}`
"""

# 媒体设置文本
MEDIA_SETTINGS_TEXT = """
媒体设置：
"""
TARGET_SETTINGS_TEXT = """
转发目标：
支持 Apprise 全部格式，详见 https://github.com/caronc/apprise/wiki
如 `ntfy://ntfy.sh/你的主题名`

飞书 / Lark 自定义机器人（本程序内置支持，需先在群聊里添加"自定义机器人"并复制 webhook）：
`feishu://TOKEN?secret=SECRET`   中国版飞书 open.feishu.cn
`lark://TOKEN?secret=SECRET`     Lark 国际版 open.larksuite.com
也可直接填完整地址 `https://open.feishu.cn/open-apis/bot/v2/hook/TOKEN`
secret 仅在机器人开启了"签名校验"时才需要，没有就省略
"""


# 为每个规则生成特定的路径
def get_rule_media_dir(rule_id):
    """获取指定规则的媒体目录"""
    rule_path = os.path.join(RSS_MEDIA_DIR, str(rule_id))
    # 确保目录存在
    os.makedirs(rule_path, exist_ok=True)
    return rule_path

def get_rule_data_dir(rule_id):
    """获取指定规则的数据目录"""
    rule_path = os.path.join(RSS_DATA_DIR, str(rule_id))
    # 确保目录存在
    os.makedirs(rule_path, exist_ok=True)
    return rule_path