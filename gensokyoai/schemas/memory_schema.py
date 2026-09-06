
import uuid
import msgspec
from datetime import datetime, timezone
from msgspec import field
from typing import Literal

class MemoryItem(msgspec.Struct, frozen=True):
    """ 
    记忆单元 
    支持通过 relate_ids 构建记忆图谱，实现级联查询。
    """
    
    memory_id: str = field(default_factory=lambda: str(uuid.uuid7()))
    """ 记忆唯一标识 (使用 UUID v7 保证时间排序) """
    
    topic: str = field(default="")
    """ 主题标签，用于快速分类 (如: '人际关系', '世界观', '物品') """
    
    content: str = field(default="")
    """ 记忆的核心内容 (摘要或原文) """
    
    happen_time: datetime = field(default_factory=lambda: datetime.now())
    """ 记忆发生时间 (剧情时间, 避免UTC带来误解)"""
    
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    """ 产生时间 (UTC) """
    
    importance: float = 0.5
    """ 重要性分数 (0.0 - 1.0)，决定淘汰优先级 """
    
    relate_ids: set[str] = field(default_factory=set)
    """ 
    关联记忆 ID 的集合。
    例如：['mem_001', 'mem_002'] 
    通过此字段可以实现 Cascade Query：查 A -> 找到 B, C -> 再查 B, C 的关联...
    """
    memory_type: Literal["dialogue", "thought", "fact", "event"] = "dialogue"
    """ 记忆类型：对话原文、内心想法、客观事实、关键事件 """

    @property
    def is_linked(self) -> bool:
        return len(self.relate_ids) > 0