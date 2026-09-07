""" 输入感知和场景打包 —— 场景适配器协议（架构文档 §3.1）"""

import aioconsole
import time
from abc import ABC, abstractmethod

from ..schemas.scene_schema import SceneSnapshot
from ..utils.logger import LoggerManager

class Perceiver(ABC):
    """ Eyes 场景适配器：把平台差异（QQ群/私聊/频道）屏蔽成统一快照流 """

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger("EYES")

    @abstractmethod
    async def next_snapshot(self) -> SceneSnapshot | None:
        """ 取下一条场景快照；None 表示暂无事件，调用方可稍后重试 """
        ...

    async def close(self) -> None:
        """ 释放平台连接，默认无操作 """

class ConsolePerceiver(Perceiver):
    """ 控制台场景适配器：本地调试用，stdin 一行 = 一条私聊消息 """

    def __init__(self, sender: str = "用户") -> None:
        super().__init__()
        self._sender = sender

    async def next_snapshot(self) -> SceneSnapshot | None:
        """ 阻塞读一行控制台输入，打包为快照。

        Returns:
            SceneSnapshot: 非空行打包成私聊快照；空行返回 None

        Raises:
            (EOFError / KeyboardInterrupt): 控制台关闭时由 input 抛出
        """
        text: str = await aioconsole.ainput("输入你的消息：")
        text = text.strip()
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
