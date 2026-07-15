"""WebSocket 连接管理与事件广播。

设计：
- ConnectionManager 维护所有活跃 WebSocket 连接
- EventBus 是一个简单的发布-订阅队列，Monitor 发布事件，Manager 广播给所有客户端
- 事件格式：{"type": "match_update" | "signal_triggered" | ... , "data": {...}}
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket


class ConnectionManager:
    """WebSocket 连接管理器。"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self._active: List[WebSocket] = []
        self._logger = logger or logging.getLogger(__name__)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.append(ws)
        self._logger.debug("WebSocket 客户端连接，当前 %d 个", len(self._active))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._active:
            self._active.remove(ws)
        self._logger.debug("WebSocket 客户端断开，当前 %d 个", len(self._active))

    async def broadcast(self, event: Dict[str, Any]) -> None:
        """广播事件给所有连接的客户端。"""
        if not self._active:
            return
        payload = json.dumps(event, ensure_ascii=False, default=str)
        dead: List[WebSocket] = []
        for ws in self._active:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def active_count(self) -> int:
        return len(self._active)


class EventBus:
    """简单异步事件总线。

    Monitor 调用 publish() 发布事件，
    后台任务消费队列并广播给 WebSocket 客户端。
    """

    def __init__(
        self,
        manager: ConnectionManager,
        queue_size: int = 1000,
        logger: Optional[logging.Logger] = None,
    ):
        self._manager = manager
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._running = False
        self._logger = logger or logging.getLogger(__name__)

    def publish(self, event_type: str, data: Dict[str, Any]) -> None:
        """发布事件（线程安全，可从 Monitor 主循环调用）。

        注意：此方法需要在事件循环中调用（asyncio.run_coroutine_threadsafe 或直接 await）。
        对于从同步代码调用，使用 publish_sync()。
        """
        event = {"type": event_type, "data": data}
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._logger.warning("EventBus 队列已满，丢弃事件: %s", event_type)

    def publish_sync(self, event_type: str, data: Dict[str, Any]) -> None:
        """从同步代码发布事件（使用 call_soon_threadsafe）。"""
        event = {"type": event_type, "data": data}
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(self._queue.put_nowait, event)
            else:
                try:
                    self._queue.put_nowait(event)
                except asyncio.QueueFull:
                    self._logger.warning("EventBus 队列已满，丢弃事件: %s", event_type)
        except RuntimeError:
            # 没有事件循环，丢弃
            self._logger.debug("无事件循环，丢弃事件: %s", event_type)

    async def start(self) -> None:
        """启动消费循环。"""
        self._running = True
        self._logger.info("EventBus 消费循环启动")
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._manager.broadcast(event)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                self._logger.error("EventBus 消费异常: %s", exc)

    async def stop(self) -> None:
        """停止消费循环。"""
        self._running = False
