from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from rss.app.routes.auth import router as auth_router
from rss.app.routes.rss import router as rss_router
from rss.app.routes.admin import router as admin_router
from rss.app.api.endpoints import feed
import uvicorn
import logging
import sys
import os
from pathlib import Path
from utils.log_config import setup_logging
from utils.log_buffer import setup_ring_buffer



root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))


# 获取日志记录器
logger = logging.getLogger(__name__)

app = FastAPI(title="TG Forwarder RSS")

# 注册路由
app.include_router(auth_router)
app.include_router(rss_router)
app.include_router(feed.router)
app.include_router(admin_router)

# 静态资源（控制台样式与脚本）
app.mount("/static", StaticFiles(directory="rss/app/static"), name="static")

# 模板配置
templates = Jinja2Templates(directory="rss/app/templates")

# 让日志可被 /admin/logs 读取（幂等，重复导入不会重复挂载）
setup_ring_buffer()

def run_server(host: str = "0.0.0.0", port: int = 8000):
    """运行 RSS 服务器"""
    uvicorn.run(app, host=host, port=port)

# 添加直接运行支持
if __name__ == "__main__":
    # 只有在直接运行时才设置日志（而不是被导入时）
    setup_logging()
    run_server()
