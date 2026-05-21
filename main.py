from dotenv import load_dotenv
load_dotenv()
import logging
import os

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes.agent import router as agent_router
from app.routes.chat_settings import router as chat_settings_router
from app.routes.chat_history import router as memory_router
from app.routes.screenshot import router as screenshot_router

# 控制台日志基础配置：让 Agent 的收发日志在本地启动时可见
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# FastAPI 应用入口：仅负责启动和挂载路由
app = FastAPI(title="Ayaya server", version="0.1.0")
# 挂载 Agent 相关 API
app.include_router(agent_router)
app.include_router(chat_settings_router)
app.include_router(memory_router)
app.include_router(screenshot_router)

# 静态文件服务：用于访问保存的图片
SCREENSHOT_DIR = os.path.join("memory", "screenshot")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=SCREENSHOT_DIR), name="images")


@app.get("/")
async def root():
    # 根路径用于快速确认服务是否启动
    return {"message": "Ayaya server is running"}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_excludes=["agent_workspace/*"]
    )