"""消息解析：平台原始消息 -> SceneEvent / SceneSnapshot"""

import time

from ..schemas.scene_schema import SceneEvent, SceneSnapshot, SceneType


def parse_message(raw: dict) -> SceneEvent:
    """解析平台原始消息为标准事件。

    支持 OneBot11 事件（含 post_type/raw_message 字段）与
    通用字典（{"sender", "content", "time"?}）两种形态。

    Args:
        raw: 平台原始消息字典

    Returns:
        SceneEvent: 标准化事件

    Raises:
        ValueError: 消息缺少内容字段
    """
    sender_obj = raw.get("sender") or {}
    if "raw_message" in raw or "post_type" in raw:  # OneBot11
        sender = str(sender_obj.get("nickname") or sender_obj.get("user_id") or "unknown")
        content = str(raw.get("raw_message") or raw.get("message") or "")
    else:  # 通用字典
        sender = str(raw.get("sender", "unknown"))
        content = str(raw.get("content", ""))

    if not content:
        raise ValueError(f"消息缺少内容字段: {raw!r}")

    return SceneEvent(sender=sender, content=content, timestamp=float(raw.get("time", 0) or 0))


def build_snapshot(
    scene_type: SceneType,
    events: list[SceneEvent],
    *,
    sender: str = "",
    content: str = "",
    is_direct: bool = False,
) -> SceneSnapshot:
    """把一组事件打包成场景快照。

    Args:
        scene_type: 场景类型
        events: 待打包事件（按时间升序）
        sender: 触发回复的发送者，缺省取最后一条事件的发送者
        content: 触发内容，缺省取最后一条事件的内容
        is_direct: 是否直接针对角色

    Returns:
        SceneSnapshot: 标准化场景快照
    """
    last = events[-1] if events else SceneEvent()
    return SceneSnapshot(
        scene_type=scene_type,
        sender=sender or last.sender,
        content=content or last.content,
        is_direct=is_direct,
        context_snippet=[e.content for e in events][-8:],
        participants=sorted({e.sender for e in events if e.sender}),
        event_queue=list(events),
        timestamp=last.timestamp or time.time(),
    )
