"""可定制思考链流水（ThinkPipeline）—— 把「怎么想」提成一等公民。

设计要点：

- **卡片驱动**：角色卡 `think_chain: [步骤名...]` 决定思考方向，步骤提示词
  集中注册在 `prompts/manager.py`（`think.<步骤名>`），业务代码不写提示词；
- **DSL 拼装**：`ThinkPipeline("标签") >> "裸指令" >> ThinkStep(...)`，
  继承 `utils/fluent.py::FluentAPI`（同步、不可变、支持链合并）；
- **异步语义**（调用方在 async 上下文）：
  - `run()` 是 `async def`，步骤**串行 await** —— 第 N+1 步要吃第 N 步的
    digest，且本地单模型经 ResourceGate 串行，并发没有收益；
  - 每步 `asyncio.wait_for` 超时隔离，慢模型不会把整条链/主循环拖死；
  - 失败只捕 `Exception`：`asyncio.CancelledError`（BaseException）**原样穿透**，
    关闭/取消流程不被吞；
  - `run()` 内部**零 `create_task`**：不在思考链路里造 fire-and-forget
    （CHANGES 0.0.19 的 GC 教训），所有等待都内联完成。
"""

import asyncio
import time
from collections.abc import Sequence
from typing import Self

import msgspec

from ...prompts import prompt_mgr
from ...schemas.brain_schema import BrainConclusion, BrainThinkEffort, ReasoningStep
from ...schemas.memory_schema import MemoryItem
from ...schemas.model_schema import Message
from ...schemas.scene_schema import SceneSnapshot
from ...utils.fluent import FluentAPI
from ...utils.logger import LoggerManager
from ..session_manager import SessionManager

_JSON_CORRECTION = "上次输出不是合法 JSON。请只输出一个 JSON 对象，不要包含任何其他内容。"

_CONCLUSION_TIMEOUT_S = 90.0
""" 结论步骤的超时（未单独配步骤超时时的兜底） """

_DIGEST_MAX_CHARS = 400
""" 前几步结论交接给下一步 / 结论步的压缩上限（上下文经济） """

_DEEP_MAX_TOKENS_FACTOR = 2
""" MAX 深思考：每步输出预算翻倍 """

_DEEP_TEMPERATURE = 0.2
""" MAX 深思考：降温求稳 """

_logger = LoggerManager.get_logger("THINK")


class PipelineAbort(Exception):
    """思考链整体失败（必要步骤失败 / 空链 / 结论步失败）—— 由调用方降级处理。"""


class ThinkStep(msgspec.Struct, frozen=True):
    """思考链上的一步。"""

    name: str
    """ 步骤名（日志 / 轨迹 / 健康指标 owner 前缀） """

    instructions: str
    """ 本步要模型想什么（来自 prompts/manager.py 的 think.<name>） """

    max_tokens: int = 200
    """ 本步输出预算（一步只要一小段结论） """

    temperature: float = 0.4
    """ 低温求稳：思考口径不需要创造性 """

    timeout_s: float = 90.0
    """ 本步超时（wait_for）；超时按步骤失败处理，不重试。
        默认 90s 而非更小：串行单模型下这个预算要同时覆盖**排队等资源闸门**
        （后台 OOC 深审/记忆蒸馏按 FIFO 先占，20~25s 是常态）+ 生成时间，
        超时把排队也算进去是实测踩过的坑（见 CHANGES Unreleased） """

    optional: bool = True
    """ 失败是否允许跳过（True=韧性优先，跳过继续；False=必要步骤，失败即中止整链） """

    tools: bool = False
    """ 本步是否挂工具（预留：检索/搜索类步骤；v1 不接线） """


class ThinkContext(msgspec.Struct, frozen=True):
    """一步的输入材料（全链共用，构造一次）。"""

    persona: str
    scene: str
    context: str
    memory: str


class ThinkPipeline(FluentAPI[ThinkStep]):
    """角色思考链：DSL / 卡片驱动拼装，`run()` 逐步无状态调用，收尾映射 BrainConclusion。"""

    def __init__(self, label: str = "", *steps: ThinkStep | str) -> None:
        """初始化。

        Args:
            label: 链的标签（日志 / 轨迹用，如「幽幽子·思考链」）
            steps: 初始步骤；裸字符串按「step{序号}」自动命名收编为指令
        """
        super().__init__(tuple(_coerce(step, index) for index, step in enumerate(steps)))
        self.label = label

    # ---------------------------------------------------------------- 链式

    def _with(self, items: tuple[ThinkStep, ...]) -> ThinkPipeline:
        """用新步骤序列造同型新实例（保留 label）。

        返回类型收窄为具体类（基类契约是 Self，本类即最终类，协变覆写合法）。
        """
        return ThinkPipeline(self.label, *items)

    def __rshift__(self, step: ThinkStep | str | FluentAPI[ThinkStep]) -> ThinkPipeline:
        """`chain >> step`：接一步（裸字符串收编为指令）或合并另一条同元素链。

        参数相对基类**放宽**（LSP）：多收编一种裸字符串糖。
        """
        if isinstance(step, FluentAPI):
            return ThinkPipeline(self.label, *self._items, *step.items)
        return ThinkPipeline(self.label, *self._items, _coerce(step, len(self._items)))

    @classmethod
    def from_names(cls, label: str, names: Sequence[str]) -> Self:
        """卡片驱动：步骤名序列 -> `think.<名>` 提示词（未注册时 prompt_mgr 抛 KeyError，启动即暴露）。"""
        return cls(
            label,
            *(
                ThinkStep(name=name, instructions=prompt_mgr.render(f"think.{name}"))
                for name in names
            ),
        )

    # ---------------------------------------------------------------- 执行

    async def run(
        self,
        *,
        snapshot: SceneSnapshot,
        memories: list[MemoryItem],
        sessions: SessionManager,
        persona: str,
        effort: BrainThinkEffort,
    ) -> BrainConclusion:
        """按推理档位裁剪链条深度，串行执行，产出结构化结论。

        档位语义（与接力思考的轮数语义对齐，单调递进）：

        - NONE：不跑链（调用方应在快速路径拦掉；到这里是防御性中止）
        - LOW：只留**收尾步**——日常寒暄不铺垫，人设 + 场景直出结论（1 次调用）
        - MID：**首 + 尾**——一点感知 + 决策（2 次调用）
        - HIGH：**全链**（N 次调用）
        - MAX：全链 + 每步**深思考**（预算 ×2、低温）

        Args:
            snapshot: 场景快照
            memories: 检索到的相关记忆
            sessions: 会话管理器（每步一次无状态调用）
            persona: 人设摘要（每步都带着，保证「按角色的方式想」）
            effort: 本回合推理档位（决定链深）

        Returns:
            BrainConclusion: 结论（verdict=draft 时带行动指令）

        Raises:
            PipelineAbort: 空链 / 必要步骤失败 / 结论步失败（调用方应降级）
        """
        if not self._items:
            raise PipelineAbort("思考链为空")
        if effort is BrainThinkEffort.NONE:
            raise PipelineAbort("档位=NONE 不跑思考链（调用方应走快速路径）")

        steps, deep = self._select_steps(effort)
        if not steps:
            raise PipelineAbort("思考链为空")

        context = _build_context(snapshot, memories, persona)
        digests = ""
        executed: list[ReasoningStep] = []
        for index, step in enumerate(steps):
            try:
                parsed = await self._run_step(step, context, digests, sessions, deep)
            except Exception as error:  # 不含 CancelledError（BaseException，原样穿透）
                if not step.optional:
                    raise PipelineAbort(f"必要步骤 {step.name} 失败: {error}") from error
                _logger.warning(f"思考步骤 {step.name} 失败，跳过: {error}")
                continue
            note = str(parsed.get("note", "")).strip()
            executed.append(
                ReasoningStep(
                    round=index + 1,
                    thought=note,
                    intent=str(parsed.get("intent", "")),
                    emotion=str(parsed.get("emotion", "")),
                    confidence=_as_float(parsed.get("confidence"), 0.5),
                )
            )
            digests = _render_digests(executed)
            _logger.debug(f"思考步骤 {step.name} 完成: {note[:60]!r}")

        return await self._conclude(context, digests, executed, sessions, effort, deep)

    def _select_steps(self, effort: BrainThinkEffort) -> tuple[tuple[ThinkStep, ...], bool]:
        """按档位选步骤 + 是否深思考（语义见 run 的 docstring）。"""
        items = self._items
        if effort is BrainThinkEffort.LOW:
            return items[-1:], False  # 只留收尾步：直出
        if effort is BrainThinkEffort.MID:
            return (items[0], items[-1]) if len(items) > 1 else items, False  # 首 + 尾
        return items, effort is BrainThinkEffort.MAX  # HIGH 全链；MAX 全链深思考

    async def _run_step(
        self,
        step: ThinkStep,
        context: ThinkContext,
        digests: str,
        sessions: SessionManager,
        deep: bool = False,
    ) -> dict:
        """跑一步：无状态小调用；JSON 解析失败纠正重试一次，再失败抛 ValueError。

        超时（wait_for）不重试，直接向上冒泡给 run() 的步骤失败策略。
        `deep`（MAX 档）：每步预算翻倍 + 降温——慢而深，只给重大剧情节点。
        """
        base = [
            Message(role="system", content=prompt_mgr.render("think.step.system")),
            Message(
                role="user",
                content=prompt_mgr.render(
                    "think.step.user",
                    instruction=step.instructions,
                    persona=context.persona,
                    scene=context.scene,
                    context=context.context,
                    memory=context.memory,
                    digests=digests or "（这是第一步）",
                ),
            ),
        ]
        messages = list(base)
        for attempt in range(2):
            result = await asyncio.wait_for(
                sessions.call(
                    f"brain.think.{step.name}",
                    messages,
                    stateless=True,
                    max_new_tokens=(
                        step.max_tokens * _DEEP_MAX_TOKENS_FACTOR if deep else step.max_tokens
                    ),
                    temperature=_DEEP_TEMPERATURE if deep else step.temperature,
                ),
                timeout=step.timeout_s,
            )
            content = result.content or ""
            try:
                return _parse_step_json(content)
            except ValueError:
                if attempt:
                    raise
                # 纠正重试：带上坏输出 + 纠偏指令（同 system-one-adapter 的 corrective retry）
                messages = [
                    *base,
                    Message(role="assistant", content=content),
                    Message(role="user", content=_JSON_CORRECTION),
                ]

        raise AssertionError("unreachable")  # pragma: no cover - 循环必返回或抛错

    async def _conclude(
        self,
        context: ThinkContext,
        digests: str,
        steps: list[ReasoningStep],
        sessions: SessionManager,
        effort: BrainThinkEffort,
        deep: bool = False,
    ) -> BrainConclusion:
        """结论步：把各步结论汇总成 BrainConclusion（契约不变，下游零改动）。"""
        messages = [
            Message(role="system", content=prompt_mgr.render("think.conclusion.system")),
            Message(
                role="user",
                content=prompt_mgr.render(
                    "think.conclusion.user",
                    persona=context.persona,
                    scene=context.scene,
                    digests=digests or "（思考链无产出）",
                ),
            ),
        ]
        try:
            result = await asyncio.wait_for(
                sessions.call(
                    "brain.think.conclusion",
                    messages,
                    stateless=True,
                    max_new_tokens=300 * _DEEP_MAX_TOKENS_FACTOR if deep else 300,
                    temperature=_DEEP_TEMPERATURE if deep else 0.4,
                ),
                timeout=_CONCLUSION_TIMEOUT_S,
            )
            parsed = _extract_json(result.content or "")
        except Exception as error:
            raise PipelineAbort(f"结论步骤失败: {error}") from error

        draft = str(parsed.get("draft", "")).strip()
        return BrainConclusion(
            # 与接力思考同口径：有行动指令才算 draft
            verdict="draft" if draft else "pass_through",
            intent=str(parsed.get("intent", "")),
            emotion=str(parsed.get("emotion", "")),
            draft=draft or None,
            confidence=_as_float(parsed.get("confidence"), 0.5),
            effort=effort,
            _reasoning=digests or None,
            reasoning_steps=steps,
            timestamp=time.time(),
        )


def _coerce(step: ThinkStep | str, index: int) -> ThinkStep:
    """裸字符串糖：自动编号命名，指令即字符串本体。"""
    if isinstance(step, str):
        return ThinkStep(name=f"step{index + 1}", instructions=step)
    return step


def _build_context(
    snapshot: SceneSnapshot, memories: list[MemoryItem], persona: str
) -> ThinkContext:
    """全链共用材料（格式与接力思考一致：省 token 的纯文本）。"""
    memory_text = "\n".join(f"- [{m.topic}] {m.content}" for m in memories) or "（无相关记忆）"
    context_text = "\n".join(snapshot.context_snippet[-5:]) or "（无上下文）"
    return ThinkContext(
        persona=persona or "（未提供）",
        scene=f"{snapshot.sender}: {snapshot.content}",
        context=context_text,
        memory=memory_text,
    )


def _render_digests(steps: list[ReasoningStep]) -> str:
    """前几步结论的压缩交接（保尾截断，越近的结论越重要）。"""
    lines = [f"- {step.thought}" for step in steps if step.thought]
    return "\n".join(lines)[-_DIGEST_MAX_CHARS:]


def _extract_json(text: str) -> dict:
    """容错提取 JSON 对象（容忍 ```json 包裹 / 前后噪声）。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"输出不是 JSON: {text[:80]!r}")
    try:
        return msgspec.json.decode(text[start : end + 1], type=dict)
    except msgspec.DecodeError as error:
        raise ValueError(f"JSON 解析失败: {text[:80]!r}") from error


def _parse_step_json(text: str) -> dict:
    """步骤输出解析：JSON 且必须带 note（本步结论）。"""
    data = _extract_json(text)
    if "note" not in data:
        raise ValueError(f"输出缺少 note 字段: {text[:80]!r}")
    return data


def _as_float(value: object, default: float) -> float:
    """宽松取浮点（布尔/非数值回落默认值）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)
