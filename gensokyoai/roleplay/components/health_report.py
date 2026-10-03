"""回合健康指标喂食 —— 把一回合的「代价」逐条记成 HealthMonitor 的单条指标。

有状态的部分只有两个差值快照（上次累计 token / 上次累计费用）：回合
增量 = 当前总量 - 上次快照。为什么从主循环拆出：它与「健康监控」本来就是
一本账，摊在主循环里只会让回合序列越来越长。

历史坑（保留注释）：`HealthMonitor.record()` 只收**单条**指标
（`{"name", "value"}`），曾经把整本计数器 dict 塞进去，每条都被「指标缺少
name」静默丢弃——档位分布与 memory.* 阈值因此从未生效。逐条 record_metric。
"""

import time

from ...core.health import HealthMonitor
from ...core.memorizer.manager import MemoryManager
from ...core.session_manager import SessionManager
from ...schemas.brain_schema import BrainThinkEffort
from ...utils.logger import LoggerManager

_RESPONDER_OWNER = "responder"
""" 上下文占用率看哪个 owner 的会话（投递层）"""


class HealthReporter:
    """回合粒度的健康指标喂食：档位分布 / 记忆规模 / 延迟 / token / 费用 / 上下文占用。"""

    def __init__(
        self, sessions: SessionManager, memory: MemoryManager, health: HealthMonitor
    ) -> None:
        """初始化。

        Args:
            sessions: 会话管理器（token / 费用 / 上下文占用率的来源）
            memory: 记忆管理器（工作/长期记忆规模）
            health: 健康监控器（逐条 record_metric 的落点）
        """
        self._logger = LoggerManager.get_logger("HEALTH")
        self._sessions = sessions
        self._memory = memory
        self._health = health
        self._last_total_tokens = 0
        """ 上次采集的累计 token，用于算回合增量 """
        self._last_cost: dict[str, float] = {}
        """ 上次采集的累计费用（币种 -> 金额），用于算回合增量 """

    async def record(self, effort: BrainThinkEffort, t_start: float) -> dict[str, float]:
        """喂一个回合的健康指标（回合末调用一次）。

        Args:
            effort: 本回合推理档位（落 effort.<档位> 计数，供分布聚合）
            t_start: 本回合起始的单调时刻（算延迟）

        Returns:
            dict: 本回合新增费用（币种 -> 金额；本地模型为空）
        """
        total = self._sessions.total_usage()
        turn_tokens = (total.prompt_tokens + total.completion_tokens) - self._last_total_tokens
        self._last_total_tokens = total.prompt_tokens + total.completion_tokens
        turn_cost = self._turn_cost()

        await self._health.record_metric("turn.count", 1.0)
        await self._health.record_metric(f"effort.{effort.value}", 1.0)
        await self._health.record_metric(
            "memory.work_size", float(self._memory.work_mem_size), unit="条"
        )
        await self._health.record_metric(
            "memory.long_size", float(self._memory.long_mem_size), unit="条"
        )
        await self._health.record_metric("turn.latency_s", time.monotonic() - t_start, unit="s")
        await self._health.record_metric("turn.tokens", float(turn_tokens), unit="tok")
        for currency, amount in turn_cost.items():
            await self._health.record_metric(f"turn.cost_{currency.lower()}", amount, unit=currency)
        await self._health.record_metric(
            "session.context_usage",
            self._sessions.context_usage(_RESPONDER_OWNER),
            unit="ratio",
        )
        return turn_cost

    def _turn_cost(self) -> dict[str, float]:
        """本回合新增费用（币种 -> 金额；本地模型全回合为空 dict）。
        与 token 计量同款差值法：拿当前总量减上次快照。"""
        now = self._sessions.total_cost()
        delta = {
            currency: round(amount - self._last_cost.get(currency, 0.0), 8)
            for currency, amount in now.items()
            if abs(amount - self._last_cost.get(currency, 0.0)) > 1e-9
        }
        self._last_cost = now
        return delta
