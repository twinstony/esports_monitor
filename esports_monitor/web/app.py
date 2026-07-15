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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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
    static_dir = os.path.join(base_dir, "webui", "dist")
    if os.path.isdir(static_dir):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    else:
        # 前端未打包时，提供简单首页
        @app.get("/")
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
