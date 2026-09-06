""" 记忆摘要压缩（架构文档 §3.4）"""

from ..session_manager import SessionManager
from ...schemas.memory_schema import MemoryItem
from ...schemas.model_schema import Message
from ...utils.logger import LoggerManager

_COMPRESS_SYSTEM = (
    "你是记忆压缩器。把一批对话记忆压缩成一段简短概要，"
    "只保留对角色有长期价值的关键信息（人名、承诺、事实、情感转折），"
    "丢弃寒暄与重复内容。直接输出概要文本。"
)

class Compressor:
    """ 把一批记忆压成一段概要，控制记忆模块膨胀 """

    def __init__(self, sessions: SessionManager) -> None:
        self._logger = LoggerManager.get_logger("MEMORY")
        self._sessions = sessions

    async def compress(self, entries: list[MemoryItem]) -> str:
        """ 无状态调用模型，把一批记忆压成概要文本。

        Args:
            entries: 待压缩的记忆条目

        Returns:
            str: 概要文本；条目为空或调用失败时返回空字符串

        Raises:
            模型后端异常原样上抛
        """
        if not entries:
            return ""
        joined = "\n".join(f"[{e.topic}] {e.content}" for e in entries)
        result = await self._sessions.call(
            "memorizer.compress",
            [
                Message(role="system", content=_COMPRESS_SYSTEM),
                Message(role="user", content=joined),
            ],
            stateless=True,
            temperature=0.3,
            max_new_tokens=300,
        )
        summary = result.content.strip()
        self._logger.info(f"压缩 {len(entries)} 条记忆 -> {len(summary)} 字概要")
        return summary
