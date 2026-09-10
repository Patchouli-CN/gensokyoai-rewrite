import time
import uuid
from datetime import datetime
from typing import Literal

import msgspec
from msgspec import field


class MemoryItem(msgspec.Struct):
    """
    记忆单元
    支持通过 relate_ids 构建记忆图谱，实现级联查询。
    注意：已从 frozen=True 改为可变，以支持 access_count / last_accessed_at 的动态更新。
    """

    memory_id: str = field(default_factory=lambda: str(uuid.uuid7()))
    """ 记忆唯一标识 (使用 UUID v7 保证时间排序) """

    topic: str = field(default="")
    """ 主题标签，用于快速分类 (如: '人际关系', '世界观', '物品') """

    content: str = field(default="")
    """ 记忆的核心内容 (摘要或原文) """

    happen_time: datetime = field(default_factory=lambda: datetime.now())
    """ 记忆发生时间 (剧情时间, 避免UTC带来误解)"""

    timestamp: float = field(default_factory=lambda: time.time())
    """ 产生时间戳 (Unix秒) """

    importance: float = 0.5
    """ 重要性分数 (0.0 - 1.0)，决定淘汰优先级 """

    relate_ids: set[str] = field(default_factory=set)
    """ 关联记忆 ID 的集合 """

    memory_type: Literal["dialogue", "thought", "fact", "event"] = "dialogue"
    """ 记忆类型：对话原文、内心想法、客观事实、关键事件 """

    # --- 新增：动态热度字段 ---
    access_count: int = 0
    """ 访问次数（被 Brain 检索到的次数）"""

    last_accessed_at: float = field(default_factory=lambda: time.time())
    """ 最近一次被访问的时间戳 (Unix秒) """

    @property
    def is_linked(self) -> bool:
        return len(self.relate_ids) > 0

    def touch(self) -> None:
        """记录一次访问：热度+1，刷新最后访问时间"""
        self.access_count += 1
        self.last_accessed_at = time.time()
