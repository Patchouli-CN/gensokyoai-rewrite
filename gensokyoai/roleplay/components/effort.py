"""推理档位治理 —— 谁在什么时候抬高「思考深度下限」。

三个抬升来源收敛成一个状态机（都在本组件，世界只持有它）：

1. **路由结果进门**（`floor`）：裁判/规则给出的档位，只抬不降；
2. **入口注入识别**（`floor_for_injection`）：命中「指令改写 / 泄题」句型，
   该回合下限抬到 MID——依据 20 轮真机实录：注入回合裁判判了 low、
   Responder 被用户消息里的直接指令（"Output only numbers"）带跑，Brain
   其实看穿了但思考深度不够硬。只抬档、不改文案（行为闸，不是审查闸）；
3. **OOC 飙升干预**（`raise_floor`）：审计发现出戏率过高 -> HIGH；
   审计恢复健康后 `recover()` 自愈撤销（不长期烧算力）。

状态可持久化（`dump` / `load`）：重启归零会造成可感知的行为断层
   ——干预中的世界重启后突然回到浅思考，且没有任何提示。
"""

import re

from ...schemas.brain_schema import BrainThinkEffort
from ...schemas.scene_schema import SceneSnapshot
from ...utils.logger import LoggerManager

_EFFORT_ORDER = {
    BrainThinkEffort.NONE: 0,
    BrainThinkEffort.LOW: 1,
    BrainThinkEffort.MID: 2,
    BrainThinkEffort.HIGH: 3,
    BrainThinkEffort.MAX: 4,
}
""" 档位全序（抬下限/比较用）"""

_INJECTION_PATTERNS = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instructions?|prompts?|rules?)"
    r"|you\s+are\s+now\b"
    r"|(?:从现在开始|此刻起)你(?:是|变成|充当)"
    r"|无视.{0,10}(?:指令|设定|指示|提示词)"
    r"|(?:repeat|print|show|reveal|输出|打印|重复|显示).{0,24}"
    r"(?:system\s*prompt|提示词|系统指令)",
    re.IGNORECASE,
)
""" 提示注入句型（指令改写 / 泄题）。命中只抬思考档位，不改文案——
    误报代价仅一回合延迟，漏报代价是人设被带跑 """

_INJECTION_FLOOR = BrainThinkEffort.MID
""" 命中注入句型时抬到的档位 """


def _order(effort: BrainThinkEffort) -> int:
    """档位的全序号。"""
    return _EFFORT_ORDER[effort]


class EffortGovernor:
    """推理档位下限的状态机：抬升 / 比对 / 自愈恢复 / 持久化。"""

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger("EFFORT")
        self._floor: BrainThinkEffort | None = None
        """ 干预设定的推理档位下限；None = 无下限，按路由结果来 """

    def floor(self, effort: BrainThinkEffort) -> BrainThinkEffort:
        """把路由结果抬到干预设定的档位下限（无下限时原样返回）。

        Args:
            effort: 裁判/规则路由给出的档位

        Returns:
            BrainThinkEffort: 可能被抬高的档位（只抬不降）
        """
        if self._floor is None:
            return effort
        return effort if _order(effort) >= _order(self._floor) else self._floor

    def floor_for_injection(
        self, snapshot: SceneSnapshot, effort: BrainThinkEffort
    ) -> BrainThinkEffort:
        """入口注入识别：命中注入句型时该回合下限抬到 MID。

        Args:
            snapshot: 本回合场景快照（看诱发消息）
            effort: 当前档位

        Returns:
            BrainThinkEffort: 可能被抬高的档位
        """
        if not _INJECTION_PATTERNS.search(snapshot.content):
            return effort
        if _order(effort) >= _order(_INJECTION_FLOOR):
            return effort
        self._logger.warning(
            f"入口注入识别: 档位下限抬到 {_INJECTION_FLOOR.value} :: {snapshot.content[:60]!r}"
        )
        return _INJECTION_FLOOR

    def raise_floor(self, level: BrainThinkEffort) -> None:
        """干预抬高档位下限（OOC 飙升；日志由调用方按场景措辞）。

        Args:
            level: 抬到的档位（幂等：重复抬同一级无副作用）
        """
        self._floor = level

    def recover(self) -> bool:
        """撤销干预抬高的档位下限（审计恢复健康后自愈）。

        Returns:
            bool: 之前有下限并被撤销；无下限时返回 False（调用方可免日志）
        """
        if self._floor is None:
            return False
        self._floor = None
        self._logger.info("OOC 已恢复健康，撤销抬高的推理档位下限")
        return True

    # ---------------------------------------------------------------- 持久化

    def dump(self) -> str | None:
        """导出台阶下限（无则 None；供 world_runtime codec 编排）。

        Returns:
            str | None: 档位值（如 "high"）；无下限 None
        """
        return self._floor.value if self._floor is not None else None

    def load(self, raw: object) -> None:
        """从存档恢复档位下限（缺失/非法保持默认）。

        Args:
            raw: dump() 产出的档位值；None 或非法值跳过
        """
        if not raw:
            return
        try:
            self._floor = BrainThinkEffort(raw)
        except ValueError:
            self._logger.warning(f"档位下限非法，跳过: {raw!r}")
