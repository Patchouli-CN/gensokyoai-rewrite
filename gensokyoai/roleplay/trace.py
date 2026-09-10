"""思考轨迹留档 —— 把接力思考的每轮步骤与结论写成 JSONL，供事后复盘。

**只写不改行为**：记录失败一律吞掉并记异常，绝不影响主链路。

落点：`<storage_dir>/traces/<session_id>.jsonl`，append-only、按大小轮转
（超出上限即把当前文件改名为 `.1`，只保留上一代）。

每回合一行，形如：

```json
{"ts": 1740000000.0, "turn": 3, "effort": "high", "intent": "追问",
 "emotion": "好奇", "steps": [{"round": 1, "thought": "...", "need_continue_think": true, ...}],
 "reasoning": "<工程实现>", "raw_reasoning": "<模型原生>", "reply": "..."}
```
"""

import asyncio
import time
from pathlib import Path

import msgspec

from ..schemas.brain_schema import BrainConclusion
from ..utils.logger import LoggerManager

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
""" 单文件大小上限（5MB），超出即轮转 """


class ReasoningTrace:
    """推理轨迹记录器（append-only JSONL，按大小轮转）。"""

    def __init__(
        self,
        storage_dir: str | Path,
        session_id: str,
        *,
        enabled: bool = True,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """初始化。

        Args:
            storage_dir: 持久化根目录（轨迹落在其 traces/ 子目录）
            session_id: 会话标识（决定文件名，天然按会话隔离）
            enabled: 是否启用；False 时 record() 直接返回
            max_bytes: 单文件大小上限，超出轮转为 .1
        """
        self._logger = LoggerManager.get_logger("TRACE")
        self._enabled = enabled
        self._max_bytes = max_bytes
        self.path = Path(storage_dir) / "traces" / f"{session_id}.jsonl"
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """是否启用留档。"""
        return self._enabled

    async def record(
        self,
        *,
        turn: int,
        effort: str,
        conclusion: BrainConclusion,
        reply: str = "",
    ) -> None:
        """记录一个回合的思考轨迹（异步侧链，不阻塞主链路）。

        Args:
            turn: 回合号
            effort: 推理档位值（如 "high"）
            conclusion: Brain 结论（含逐轮 ReasoningStep 与两份 reasoning）
            reply: 本回合最终回复文本
        """
        if not self._enabled:
            return
        try:
            line = msgspec.json.encode(
                self._entry(turn=turn, effort=effort, conclusion=conclusion, reply=reply)
            )
            async with self._lock:
                await asyncio.to_thread(self._append_sync, line)
        except Exception:
            self._logger.exception("思考轨迹留档失败（不影响主链路）")

    @staticmethod
    def _entry(*, turn: int, effort: str, conclusion: BrainConclusion, reply: str) -> dict:
        """组装一行轨迹。

        Returns:
            dict: 可直接 JSON 序列化的轨迹条目
        """
        return {
            "ts": time.time(),
            "turn": turn,
            "effort": effort,
            "verdict": conclusion.verdict,
            "intent": conclusion.intent,
            "emotion": conclusion.emotion,
            "confidence": conclusion.confidence,
            "draft": conclusion.draft,
            "reasoning": conclusion.reasoning,
            "raw_reasoning": conclusion.raw_reasoning,
            "steps": [msgspec.to_builtins(s) for s in conclusion.reasoning_steps],
            "reply": reply,
        }

    def _append_sync(self, line: bytes) -> None:
        """同步追加一行（在线程池中执行），必要时先轮转。

        Args:
            line: 已序列化的 JSON 行（不含换行）
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size + len(line) > self._max_bytes:
            self.path.replace(self.path.with_suffix(".jsonl.1"))
            self._logger.info(f"思考轨迹已轮转: {self.path.name} -> {self.path.name}.1")
        with self.path.open("ab") as handle:
            handle.write(line + b"\n")
