"""生命周期管理器 —— 统管应用启动/关闭事件"""

import time
from collections.abc import Awaitable, Callable

from ..schemas.event_schema import EventTopic
from ..utils.logger import LoggerManager
from .event_bus import EventBus

type LifecycleHandler = Callable[[], Awaitable[None]]


class LifecycleManager:
    """生命周期管理器：
    - 启动时按注册顺序执行所有 startup 回调
    - 关闭时按注册逆序执行所有 shutdown 回调
    - 通过 EventBus 发布 STARTUP/SHUTDOWN 事件
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._logger = LoggerManager.get_logger("LIFECYCLE")
        self._startup_handlers: list[LifecycleHandler] = []
        self._shutdown_handlers: list[LifecycleHandler] = []
        self._started = False
        self._stopped = False

    def on_startup(self, handler: LifecycleHandler) -> LifecycleHandler:
        """装饰器：注册启动回调"""
        self._startup_handlers.append(handler)
        return handler

    def on_shutdown(self, handler: LifecycleHandler) -> LifecycleHandler:
        """装饰器：注册关闭回调"""
        self._shutdown_handlers.append(handler)
        return handler

    async def startup(self) -> None:
        """触发所有启动回调，并发布 STARTUP 事件"""
        if self._started:
            self._logger.warning("应用已启动，忽略重复 startup")
            return
        self._started = True

        self._logger.info("生命周期：启动开始")
        start_time = time.monotonic()

        # 1. 发布 STARTUP 事件（EventBus 订阅者先处理）
        await self._bus.publish(EventBus.new(EventTopic.STARTUP, source="lifecycle"))

        # 2. 执行所有 startup 回调
        for handler in self._startup_handlers:
            try:
                await handler()
                self._logger.debug(f"启动回调完成: {handler.__name__}")
            except Exception:
                self._logger.exception(f"启动回调异常: {handler.__name__}")

        self._logger.info(f"生命周期：启动完成 耗时={time.monotonic() - start_time:.3f}s")

    async def shutdown(self) -> None:
        """触发所有关闭回调（逆序），并发布 SHUTDOWN 事件"""
        if self._stopped:
            self._logger.warning("应用已关闭，忽略重复 shutdown")
            return
        self._stopped = True

        self._logger.info("生命周期：关闭开始")
        start_time = time.monotonic()

        # 1. 执行所有 shutdown 回调（逆序，类似资源栈）
        for handler in reversed(self._shutdown_handlers):
            try:
                await handler()
                self._logger.debug(f"关闭回调完成: {handler.__name__}")
            except Exception:
                self._logger.exception(f"关闭回调异常: {handler.__name__}")

        # 2. 发布 SHUTDOWN 事件（EventBus 订阅者最后处理）
        await self._bus.publish(EventBus.new(EventTopic.SHUTDOWN, source="lifecycle"))

        self._logger.info(f"生命周期：关闭完成 耗时={time.monotonic() - start_time:.3f}s")

    def request_stop(self) -> None:
        """请求停止（供信号处理器调用）"""
        self._logger.info("收到停止请求")
