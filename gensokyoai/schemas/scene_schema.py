"""Eyes 感知层的数据契约"""

from typing import Literal

import msgspec

type SceneType = Literal["group_chat", "private_chat", "channel"]
""" 场景类型 """


class SceneEvent(msgspec.Struct, frozen=True):
    """场景中的一条原始事件"""

    kind: Literal["message", "join", "at", "recall"] = "message"
    """ 事件种类 """
    sender: str = ""
    """ 发送者标识 """
    content: str = ""
    """ 文本内容 """
    timestamp: float = 0.0
    """ Unix 时间戳 """


class SceneSnapshot(msgspec.Struct, frozen=True):
    """标准化场景快照 —— Eyes 对外的唯一输出（架构文档 §3.1）"""

    scene_type: SceneType = "group_chat"
    """ 场景类型 """
    sender: str = ""
    """ 本次触发回复的发送者 """
    content: str = ""
    """ 触发内容 """
    is_direct: bool = False
    """ 是否直接针对角色（@ 或私聊）"""
    context_snippet: list[str] = []
    """ 最近的上下文消息（旧 -> 新）"""
    participants: list[str] = []
    """ 参与者列表 """
    event_queue: list[SceneEvent] = []
    """ 未处理的待处理事件队列 """
    timestamp: float = 0.0
    """ 快照时间 """
