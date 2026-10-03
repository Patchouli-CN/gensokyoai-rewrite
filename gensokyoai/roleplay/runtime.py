"""世界跨回合运行时状态 —— 主循环与后台侧链共享的状态字段。

为什么单独安家：这些字段都会被「主循环之外」的代码读写——放进任何一个
职责组件都会造成反向依赖，继续摊在世界对象上又会把 `_` 前缀的私有状态撒成
一张谁都改得的网。它们共同的特点是**回路接缝**：

- `busy`：主循环置位 / 复位，主动发言节拍读取（避免两者并发抢同一个
  responder 会话）
- `generation`：主循环退出时 +1，蒸馏 / OOC 审计 / 主动发言在回写前校验
  （关闭后的迟到写入不许污染新会话）
- `last_activity`：主循环与主动发言更新，主动发言节拍读取（空闲判定）
- `distill_counter`：主循环每回合自增，到点触发蒸馏；重启归零会造成
  可感知的节奏断层（刚落过盘又立刻蒸馏 / 该蒸馏时迟迟不触发）

主循环与各组件注入同一个 `RuntimeState` 实例，状态私有、访问走方法。
"""

import contextlib
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .components.effort import EffortGovernor
    from .components.stall import StallSpeaker


class RuntimeState:
    """世界的跨回合运行时状态（busy / generation / last_activity / 蒸馏计数）。"""

    def __init__(self) -> None:
        self._busy = False
        """ 主链路生成中标志 """
        self.generation = 0
        """ 代际令牌：后台任务持发起时的代际，回写前校验 """
        self.last_activity = time.monotonic()
        """ 距上次用户互动的单调时刻 """
        self.distill_counter = 0
        """ 距下次记忆蒸馏的回合计数（可持久化，见 WorldRuntimeCodec） """

    @property
    def busy(self) -> bool:
        """主链路是否正在生成（主动发言节拍据此避让）。

        Returns:
            bool: True 表示主循环正在本回合内生成，主动发言应跳过
        """
        return self._busy

    @contextlib.contextmanager
    def begin_busy(self) -> Iterator[None]:
        """把主链路标记为忙，退出时自动复位（异常安全）。

        Yields:
            None
        """
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    def note_activity(self) -> None:
        """记一次用户互动（刷新空闲判定的起点）。"""
        self.last_activity = time.monotonic()

    def idle_for(self) -> float:
        """距上次互动的空闲秒数。

        Returns:
            float: 单调时钟测量的空闲秒数
        """
        return time.monotonic() - self.last_activity

    def bump_generation(self) -> None:
        """代际 +1（在途后台任务的回写全部作废）。"""
        self.generation += 1


class WorldRuntimeCodec:
    """世界**跨回合**运行时状态的持久化编解码（只做编排）。

    状态的所有权在各处：档位下限在 EffortGovernor、过渡语回合号在
    StallSpeaker、蒸馏计数在 RuntimeState 自己。本 codec 向它们各要一份 /
    还一份，**不摸任何私有字段**——StateCodec 协议（见 roleplay/persistence.py）
    本就要求「持久化层不必知道世界的内部字段」，早先对着世界私有字段伸手的
    实现已在此收敛。

    两个 monotonic 时刻（如过渡语的上次时刻）不持久化：跨进程没有意义，
    恢复时由所有者重置为「现在」，比存一个会误导的数值正确。
    """

    key = "world_runtime"

    def __init__(
        self,
        runtime: RuntimeState,
        effort: EffortGovernor,
        stall: StallSpeaker,
    ) -> None:
        """初始化。

        Args:
            runtime: 跨回合运行时状态（蒸馏计数的主人）
            effort: 档位治理（干预下限的主人）
            stall: 过渡语播报（上次垫话回合号的主人）
        """
        self._runtime = runtime
        self._effort = effort
        self._stall = stall

    def dump(self) -> dict:
        """导出跨回合运行时状态。

        Returns:
            dict: 档位下限（无则 None）+ 过渡语回合号 + 蒸馏计数
        """
        return {
            "effort_floor": self._effort.dump(),
            "stall_last_turn": self._stall.dump(),
            "distill_counter": self._runtime.distill_counter,
        }

    def load(self, data) -> None:
        """恢复跨回合运行时状态（字段缺失或非法时保持默认）。

        Args:
            data: dump() 产出的字典；None 时跳过
        """
        if not isinstance(data, dict):
            return
        self._effort.load(data.get("effort_floor"))
        self._stall.load(data.get("stall_last_turn"))
        counter = data.get("distill_counter")
        if isinstance(counter, int):
            self._runtime.distill_counter = counter
