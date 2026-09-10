"""eyes基类"""

from abc import ABC, abstractmethod

from ..schemas.scene_schema import SceneSnapshot
from ..utils.logger import LoggerManager


class Perceiver(ABC):
    """Eyes 场景适配器：把平台差异（QQ群/私聊/频道）屏蔽成统一快照流"""

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger("EYES")
        self._stop_requested = False

    @abstractmethod
    async def next_snapshot(self) -> SceneSnapshot | None:
        """取下一条场景快照；None 表示暂无事件，调用方可稍后重试"""
        ...

    async def close(self) -> None:
        """释放平台连接（默认无操作，子类按需覆盖）"""
        self._logger.debug("close() 默认无操作")

    def request_stop(self) -> None:
        """请求停止读取（由生命周期管理器或信号处理器调用）"""
        self._stop_requested = True
