"""FastAPI 应用主入口。

提供：
- REST API 路由挂载
- WebSocket 端点
- 静态文件托管（前端打包产物）
- CORS 支持（开发模式）
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .dependencies import WebDependencies
from .ws.manager import ConnectionManager, EventBus


logger = logging.getLogger(__name__)

# 全局实例（在 create_app 中初始化）
_ws_manager: Optional[ConnectionManager] = None
_event_bus: Optional[EventBus] = None


def get_ws_manager() -> ConnectionManager:
    assert _ws_manager is not None, "WebSocket manager not initialized"
    return _ws_manager


def get_event_bus() -> EventBus:
    assert _event_bus is not None, "EventBus not initialized"
    return _event_bus


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。"""
    global _ws_manager, _event_bus

    # 启动
    base_dir = app.state.base_dir
    deps = WebDependencies(base_dir=base_dir)
    app.state.deps = deps

    _ws_manager = ConnectionManager(logger=logger)
    _event_bus = EventBus(_ws_manager, logger=logger)

    # 启动 EventBus 消费循环
    event_bus_task = asyncio.create_task(_event_bus.start())

    # 启动 Monitor（带 event_bus）
    import threading
    from ..config.manager import load_config
    from ..core.monitor import Monitor

    config = load_config(base_dir)
    monitor = Monitor(config=config, base_dir=base_dir, event_bus=_event_bus)
    app.state.monitor = monitor

    monitor_thread = threading.Thread(target=monitor.run_continuous, daemon=True)
    monitor_thread.start()
    app.state.monitor_thread = monitor_thread

    logger.info("FastAPI 应用启动完成（Monitor + EventBus 已启动）")

    yield

    # 关闭
    monitor.stop()
    if _event_bus:
        await _event_bus.stop()
    event_bus_task.cancel()
    try:
        await event_bus_task
    except asyncio.CancelledError:
        pass
    logger.info("FastAPI 应用已关闭")


def create_app(base_dir: str) -> FastAPI:
    """创建 FastAPI 应用实例。"""
    app = FastAPI(
        title="Esports Monitor Web UI",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.base_dir = base_dir

    # CORS（开发模式允许所有来源）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册 API 路由
    from .api.matches import router as matches_router
    from .api.trades import router as trades_router
    from .api.signals import router as signals_router
    from .api.system import router as system_router

    app.include_router(matches_router, prefix="/api")
    app.include_router(trades_router, prefix="/api")
    app.include_router(signals_router, prefix="/api")
    app.include_router(system_router, prefix="/api")

    # WebSocket 端点
    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        manager = get_ws_manager()
        await manager.connect(ws)
        try:
            while True:
                # 保持连接，接收客户端心跳/指令
                data = await ws.receive_text()
                # 目前不处理客户端消息，仅保持连接
        except WebSocketDisconnect:
            manager.disconnect(ws)

    # 静态文件托管（前端打包产物）
    # 实现 SPA history 路由回退：未匹配的路径返回 index.html，让前端路由处理
    static_dir = os.path.join(base_dir, "webui", "dist")
    index_file = os.path.join(static_dir, "index.html")

    if os.path.isdir(static_dir):
        # 静态资源（js/css/图片等）由 StaticFiles 提供
        app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="assets")

        # 根路径返回 index.html
        @app.get("/", include_in_schema=False)
        async def root_index():
            if os.path.isfile(index_file):
                return FileResponse(index_file)
            return {"message": "Esports Monitor Web UI", "note": "index.html 不存在"}

        # SPA history 路由回退：除 /api、/ws、/assets、/docs 等之外的路径都返回 index.html
        # 这样直接刷新 /trades、/morphology、/system 等路由不会被 FastAPI 视为 404
        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(path: str, request: Request):
            # 已注册的 API 路由会优先匹配，这里只处理静态资源或 SPA 路由
            # 先尝试在静态目录中查找匹配的文件（如 favicon.ico、vite.svg 等）
            candidate = os.path.join(static_dir, path)
            if path and os.path.isfile(candidate) and not path.startswith(("api/", "ws")):
                # 安全检查：禁止路径穿越
                real_static = os.path.realpath(static_dir)
                real_candidate = os.path.realpath(candidate)
                if real_candidate.startswith(real_static + os.sep):
                    return FileResponse(real_candidate)
            # 否则返回 index.html 让前端路由处理
            if os.path.isfile(index_file):
                return FileResponse(index_file)
            raise HTTPException(status_code=404, detail="index.html not found")
    else:
        # 前端未打包时，提供简单首页
        @app.get("/", include_in_schema=False)
        async def root():
            return {
                "message": "Esports Monitor Web UI",
                "api_docs": "/docs",
                "ws_endpoint": "/ws",
                "note": "前端未打包，请先 cd webui && npm run build",
            }

    return app


def run_web(base_dir: str, host: str = "0.0.0.0", port: int = 8000) -> None:
    """启动 Web 服务。"""
    import uvicorn

    app = create_app(base_dir)
    uvicorn.run(app, host=host, port=port, log_level="info")
