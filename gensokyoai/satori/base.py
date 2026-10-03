"""satori 基类 —— 感知层（觉）：各路平台眼睛（perceiver）的统一抽象"""

from abc import ABC, abstractmethod

from ..schemas.scene_schema import SceneSnapshot
from ..utils.logger import LoggerManager


class Perceiver(ABC):
    """Satori 场景适配器：把平台差异（QQ群/私聊/频道）屏蔽成统一快照流"""

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger("SATORI")
        self._stop_requested = False

    @abstractmethod
    async def next_snapshot(self) -> SceneSnapshot | None:
        """取下一条场景快照；None 表示暂无事件，调用方可稍后重试"""
        ...

    def drain(self) -> list[SceneSnapshot]:
        """非阻塞排空积压输入（默认无积压语义，返回空列表）。

        缓冲型感知器（如 QueuePerceiver）覆盖此方法，供 world 在回合间隙
        把积压一次性取走合并处理；流式/直连型感知器无需理会。
        """
        return []

    async def close(self) -> None:
        """释放平台连接（默认无操作，子类按需覆盖）"""
        self._logger.debug("close() 默认无操作")

    def request_stop(self) -> None:
        """请求停止读取（由生命周期管理器或信号处理器调用）"""
        self._stop_requested = True

    @property
    def stopping(self) -> bool:
        """是否已请求停止（`request_stop` 的读侧）。

        主循环与外部据此优雅退出。读侧公开是刻意的：调用方（主循环 /
        频道中枢）不该隔着一层 `getattr` 摸感知器的私有字段——那既绕过类型
        检查，也让「停止」这个协议只剩写侧没有读侧。

        Returns:
            bool: True 表示已请求停止
        """
        return self._stop_requested
