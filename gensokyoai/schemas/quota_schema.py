"""配额与限流的数据契约"""

import msgspec


class TenantQuota(msgspec.Struct):
    """单个租户（会话 / 频道 / 用户）的资源配额与用量快照。

    设计锚点：本地单模型是串行稀缺资源，「多路」的本质是排队 + 路由而非并行，
    因此配额以「每租户」为单位保护模型，而不是追求并发。
    """

    tenant: str = ""
    """ 租户标识（如 session_id / channel_id / user）"""

    rpm: int = 0
    """ 每分钟模型调用上限；0 表示不限 """

    concurrency: int = 1
    """ 本租户并发上限 """

    calls_per_day: int = 0
    """ 每日模型调用上限；0 表示不限 """

    tokens_per_day: int = 0
    """ 每日 token 预算（输入+输出）；0 表示不限 """

    used_calls: int = 0
    """ 当前窗口已用调用数 """

    used_tokens: int = 0
    """ 当前窗口已用 token """

    window_reset_at: float = 0.0
    """ 当前窗口重置时刻（Unix 秒）"""
