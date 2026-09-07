""" 事件总线 —— L3 业务模块间唯一的通信通道（架构文档 §7.3 铁律 2）"""

import time
from typing import Awaitable, Callable

from ..schemas.event_schema import BaseEvent, EventTopic
from ..utils.logger import LoggerManager

type EventHandler = Callable[[BaseEvent], Awaitable[None]]

class EventBus:
    """ 异步发布/订阅；单个订阅者异常只记日志，不阻塞主链路 """

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger("EVENTBUS")
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, topic: str, handler: EventHandler) -> Callable[[], None]:
        """ 订阅主题，返回取消订阅函数 """
        self._handlers.setdefault(topic, []).append(handler)

        def _unsubscribe() -> None:
            self._handlers[topic].remove(handler)

        return _unsubscribe

    def on(self, topic: str) -> Callable[[EventHandler], EventHandler]:
        """ 装饰器：订阅事件
        
        用法：
        @bus.on(EventTopic.STARTUP)
        async def on_startup(event: BaseEvent):
            ...
        """
        def decorator(handler: EventHandler) -> EventHandler:
            self.subscribe(topic, handler)
            return handler
        return decorator

    async def publish(self, event: BaseEvent) -> None:
        """ 发布事件（依次 await 全部处理器，异常隔离）"""
        for handler in self._handlers.get(event.topic, []):
            try:
                await handler(event)
            except Exception:
                self._logger.exception(f"事件处理器异常 topic={event.topic} source={event.source}")

    @staticmethod
    def new(topic: str, source: str = "", payload: object = None) -> BaseEvent:
        """ 便捷构造：自动盖时间戳 """
        return BaseEvent(topic=topic, source=source, payload=payload, timestamp=time.time())
