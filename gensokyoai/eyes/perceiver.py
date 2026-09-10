"""输入感知和场景打包 —— 场景适配器协议（架构文档 §3.1）"""

import asyncio
import contextlib
import signal
import time

import aioconsole

from ..schemas.scene_schema import SceneSnapshot
from .base import Perceiver


class ConsolePerceiver(Perceiver):
    """控制台场景适配器：本地调试用，stdin 一行 = 一条私聊消息"""

    def __init__(self, sender: str = "用户") -> None:
        super().__init__()
        self._sender = sender

    async def next_snapshot(self) -> SceneSnapshot | None:
        """阻塞读一行控制台输入，支持优雅关闭。

        Returns:
            SceneSnapshot: 非空行打包成私聊快照；None 表示停止或超时
        """
        if self._stop_requested:
            return None

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        # 注册信号处理（只注册 SIGINT，SIGTERM 在 Windows 上不支持）
        with contextlib.suppress(NotImplementedError):
            # Windows 不支持 add_signal_handler
            loop.add_signal_handler(signal.SIGINT, stop_event.set)

        input_task = None
        stop_task = None

        try:
            # 用 asyncio.wait 让 ainput 和停止信号赛跑
            input_task = asyncio.create_task(aioconsole.ainput("输入你的消息："))
            stop_task = asyncio.create_task(stop_event.wait())

            done, pending = await asyncio.wait(
                {input_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )

            # 如果停止信号先到
            if stop_task in done:
                input_task.cancel()
                # 等待 input_task 真正结束，避免异常泄漏
                with contextlib.suppress(asyncio.CancelledError, EOFError):
                    await input_task
                self._stop_requested = True
                self._logger.info("收到停止信号，感知器停止")
                return None

            # 如果输入先到
            try:
                text = input_task.result().strip()
            except asyncio.CancelledError, EOFError:
                self._logger.info("输入任务被取消或关闭")
                self._stop_requested = True
                return None

            if not text:
                return None

            self._logger.debug(f"控制台输入: {text}")
            return SceneSnapshot(
                scene_type="private_chat",
                sender=self._sender,
                content=text,
                is_direct=True,
                timestamp=time.time(),
            )

        except asyncio.CancelledError:
            # 感知器被取消（Ctrl+C 或其他取消信号），吞掉异常，返回 None 让主循环退出
            self._logger.info("感知器被取消，返回 None")
            self._stop_requested = True
            return None
        finally:
            # 确保清理所有任务
            if input_task and not input_task.done():
                input_task.cancel()
            if stop_task and not stop_task.done():
                stop_task.cancel()

            if not self._stop_requested:
                with contextlib.suppress(NotImplementedError, ValueError):
                    loop.remove_signal_handler(signal.SIGINT)
