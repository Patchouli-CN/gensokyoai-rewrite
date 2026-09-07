""" 事件总线信封与主题常量 """

import msgspec

class EventTopic:
    """ 事件主题常量（Phase 1 主链路）"""

    SNAPSHOT = "eyes.snapshot"
    """ Eyes -> 编排者：场景快照 """
    BRAIN_CONCLUSION = "brain.conclusion"
    """ Brain -> Responder：结构化结论 """
    REPLY = "responder.reply"
    """ Responder -> 输出/记忆：最终回复 """
    MEMORY_WRITE = "memory.write"
    """ 异步记忆写入 """
    HEALTH_ALERT = "health.alert"
    """ HealthCenter 干预通知 """
    STARTUP = "app.startup"
    """ 启动 """
    SHUTDOWN = "app.shutdown"
    """ 关闭 """
    STOP_REQUESTED = "app.stop_requested"  # 用户按 Ctrl+C 时触发
    """ 停止 """

class BaseEvent(msgspec.Struct, frozen=True):
    """ 事件信封：payload 按 topic 对应 schemas 中的具体类型 """
    topic: str
    """ 主题，取 Topic 常量 """
    source: str = ""
    """ 发布方标识，如 "brain" """
    payload: object = None
    """ 载荷对象 """
    timestamp: float = 0.0
    """ 发布时间 """
