"""可定制思考链流水（ThinkPipeline）—— 把「怎么想」提成一等公民。

设计要点：

- **卡片驱动**：角色卡 `think_chain` 决定思考方向——每项可以是内置步骤名
  （提示词集中注册在 `prompts/manager.py` 的 `think.<名>`），也可以是**内联
  自定义步骤**（卡片里直接写 name/instructions，角色卡作者零代码造步骤）；
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
from ...schemas.model_schema import CompletionResult, Message, ToolCall, ToolSpec
from ...schemas.scene_schema import SceneSnapshot
from ...utils.fluent import FluentAPI
from ...utils.logger import LoggerManager
from ...utils.text import extract_json_object, try_extract_json_object
from ..session_manager import SessionManager
from ..toolkit import build_executor

_JSON_CORRECTION = "上次输出不是合法 JSON。请只输出一个 JSON 对象，不要包含任何其他内容。"

_CONCLUSION_TIMEOUT_S = 90.0
""" 结论步骤的超时（未单独配步骤超时时的兜底） """

_DIGEST_MAX_CHARS = 400
""" 前几步结论交接给下一步 / 结论步的压缩上限（上下文经济） """

_DEEP_MAX_TOKENS_FACTOR = 2
""" MAX 深思考：每步输出预算翻倍 """

_DEEP_TEMPERATURE = 0.2
""" MAX 深思考：降温求稳 """

_STEPS_WITH_TOOLS = frozenset({"time_anchor"})
""" 内置步骤里默认挂工具的（角色卡按名引用即用；其余步骤想挂工具就
    在 DSL 里显式 `ThinkStep(name=..., instructions=..., tools=True)`） """

_STEP_FIELDS = frozenset(
    {"name", "instructions", "max_tokens", "temperature", "timeout_s", "optional", "tools"}
)
""" 卡片内联步骤字典允许的字段（与 ThinkStep 对齐；未知字段启动即报错） """

_logger = LoggerManager.get_logger("THINK")


def builtin_step(name: str) -> ThinkStep:
    """内置思考步骤：元数据（tools 标志）在本模块，指令文本走 `think.<名>` 提示词。

    Args:
        name: 步骤名（对应 prompts/manager.py 注册的 `think.<name>`）

    Returns:
        ThinkStep: 步骤定义；未注册的名字由 prompt_mgr 抛 KeyError（启动即暴露）

    Raises:
        KeyError: 提示词未注册
    """
    return ThinkStep(
        name=name,
        instructions=prompt_mgr.render(f"think.{name}"),
        tools=name in _STEPS_WITH_TOOLS,
    )


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
        """卡片驱动：步骤名序列 -> 内置步骤定义（含元数据如 tools 标志）。

        指令文本仍走 `prompts/manager.py` 的 `think.<名>`（提示词集中管理铁律
        不破）；未注册的名字由 prompt_mgr 抛 KeyError，启动即暴露。
        """
        return cls(label, *(builtin_step(name) for name in names))

    @classmethod
    def from_card(cls, label: str, items: Sequence[str | dict]) -> Self:
        """卡片驱动（完整版）：混合「内置步骤名 / 内联自定义步骤」。

        `think_chain` 的每一项可以是：

        - 字符串：内置步骤名（同 `from_names`）；
        - 字典：内联自定义步骤，字段同 `ThinkStep`（`name`/`instructions` 必填，
          `max_tokens`/`temperature`/`timeout_s`/`optional`/`tools` 可选）——
          角色卡作者直接写「这一步想什么」，无需改代码。

        Args:
            label: 链的标签
            items: 角色卡 `think_chain` 原始项

        Returns:
            Self: 拼装好的思考链

        Raises:
            ValueError: 项既非名称也非合法步骤字典（未知字段 / 缺必填）
        """
        return cls(label, *(_step_from_card(item) for item in items))

    # ---------------------------------------------------------------- 执行

    async def run(
        self,
        *,
        snapshot: SceneSnapshot,
        memories: list[MemoryItem],
        sessions: SessionManager,
        persona: str,
        effort: BrainThinkEffort,
        tools: list[ToolSpec] | None = None,
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
            tools: 全链可用的工具声明；仅 `ThinkStep.tools=True` 的步骤会带上
                （本地小模型的「文本喊话」由本模块识别、执行并回填后重取一次）

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
                parsed = await self._run_step(step, context, digests, sessions, deep, tools)
            except Exception as err:  # 不含 CancelledError（BaseException，原样穿透）
                if not step.optional:
                    raise PipelineAbort(f"必要步骤 {step.name} 失败: {err}") from err
                _logger.warning(f"思考步骤 {step.name} 失败，跳过: {err}")
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
        tools: list[ToolSpec] | None = None,
    ) -> dict:
        """跑一步：无状态小调用；JSON 解析失败纠正重试一次，再失败抛 ValueError。

        超时（wait_for）不重试，直接向上冒泡给 run() 的步骤失败策略。
        `deep`（MAX 档）：每步预算翻倍 + 降温——慢而深，只给重大剧情节点。
        `tools`：仅当 `step.tools=True` 且tools 非空时随调用带上模型——
        原生 tool_calls 由 Provider 内循环自行执行；本地小模型的「文本喊话」
        在这里识别、执行、把结果回填后重取一次（与接力思考同款语义）。
        """
        call_tools = tools if (step.tools and tools) else None
        # 挂了工具的步骤：system 里注入喊话约定 + 摊开工具签名（参数格式 upfront）
        system_content = prompt_mgr.render("think.step.system") + (
            prompt_mgr.render(
                "think.step.tools_hint",
                tool_lines=[tool.signature() for tool in call_tools],
            )
            if call_tools
            else ""
        )
        base = [
            Message(role="system", content=system_content),
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
                    tools=call_tools,
                ),
                timeout=step.timeout_s,
            )
            content = result.content or ""
            # 文本喊话工具调用（原生格式已被 Provider 内循环消化，到这里的是喊话）
            if call_tools:
                normalized = sessions.normalize_tool_calls(result, try_extract_json_object(content))
                if normalized.tool_calls:
                    messages = await self._feed_tool_results(
                        base, result, normalized.tool_calls, call_tools
                    )
                    continue
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

    @staticmethod
    async def _feed_tool_results(
        base: list[Message],
        result: CompletionResult,
        calls: list[ToolCall],
        tools: list[ToolSpec],
    ) -> list[Message]:
        """执行文本喊话的工具调用，把结果回填成下一轮消息（同接力思考语义）。"""
        outcomes = await build_executor(tools).execute_many(calls)
        result_text = "\n".join(outcome.to_model_text() for outcome in outcomes)
        return [
            *base,
            Message(role="assistant", content=result.content or "", tool_calls=calls),
            Message(
                role="user",
                content=f"【工具执行结果】\n{result_text}\n请基于以上结果给出本步结论 JSON。",
            ),
        ]

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
            parsed = extract_json_object(result.content or "")
        except Exception as err:
            raise PipelineAbort(f"结论步骤失败: {err}") from err

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


def _step_from_card(item: str | dict) -> ThinkStep:
    """角色卡 `think_chain` 的一项 -> ThinkStep（内置名 / 内联字典）。

    Raises:
        ValueError: 项类型不对、缺必填字段、或含未知字段
    """
    if isinstance(item, str):
        return builtin_step(item)
    if not isinstance(item, dict):
        raise ValueError(f"思考链步骤必须是内置名或字典: {item!r}")
    unknown = set(item) - _STEP_FIELDS
    if unknown:
        raise ValueError(f"思考链步骤含未知字段 {sorted(unknown)}（允许: {sorted(_STEP_FIELDS)}）")
    if "name" not in item or "instructions" not in item:
        raise ValueError(f"自定义思考步骤必须提供 name 和 instructions: {item!r}")
    return ThinkStep(**item)


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


def _parse_step_json(text: str) -> dict:
    """步骤输出解析：JSON 且必须带 note（本步结论）。"""
    data = extract_json_object(text)
    if "note" not in data:
        raise ValueError(f"输出缺少 note 字段: {text[:80]!r}")
    return data


def _as_float(value: object, default: float) -> float:
    """宽松取浮点（布尔/非数值回落默认值）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)
