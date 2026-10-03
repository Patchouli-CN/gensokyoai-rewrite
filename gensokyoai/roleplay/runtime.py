"""世界跨回合运行时状态 —— 主循环与后台侧链共享的三个字段。

为什么单独安家：这三个字段都会被「主循环之外」的代码读写——放进任何一个
职责组件都会造成反向依赖，继续摊在世界对象上又会把 `_` 前缀的私有状态撒成
一张谁都改得的网。它们共同的特点是**回路接缝**：

- `busy`：主循环置位 / 复位，主动发言节拍读取（避免两者并发抢同一个
  responder 会话）
- `generation`：主循环退出时 +1，蒸馏 / OOC 审计 / 主动发言在回写前校验
  （关闭后的迟到写入不许污染新会话）
- `last_activity`：主循环与主动发言更新，主动发言节拍读取（空闲判定）

主循环与各组件注入同一个 `RuntimeState` 实例，状态私有、访问走方法。
"""

import contextlib
import time
from collections.abc import Iterator


class RuntimeState:
    """世界的跨回合运行时状态（busy / generation / last_activity）。"""

    def __init__(self) -> None:
        self._busy = False
        """ 主链路生成中标志 """
        self.generation = 0
        """ 代际令牌：后台任务持发起时的代际，回写前校验 """
        self.last_activity = time.monotonic()
        """ 距上次用户互动的单调时刻 """

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
