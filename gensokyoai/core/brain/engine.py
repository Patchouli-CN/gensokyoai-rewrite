"""推理引擎主循环"""

import time

import msgspec

from ...prompts import prompt_mgr
from ...schemas.brain_schema import BrainConclusion, BrainThinkEffort
from ...schemas.memory_schema import MemoryItem
from ...schemas.model_schema import Message, ToolCall, ToolSpec
from ...schemas.scene_schema import SceneSnapshot
from ...utils.logger import LoggerManager
from ..session_manager import SessionManager
from .ooc_detector import OOCDetector

_route_logger = LoggerManager.get_logger("BRAIN")

_EMOTION_WORDS = ("难过", "开心", "生气", "伤心", "喜欢", "讨厌", "害怕", "哭", "笑", "感动")
_PLOT_WORDS = ("世界", "本质", "为什么", "记得", "过去", "未来", "剧情", "故事", "命运", "秘密")


def route(snapshot: SceneSnapshot) -> BrainThinkEffort:
    """档位路由（架构文档 §6.4）：规则启发式打分，零模型调用。"""
    text = snapshot.content
    if not text:
        return BrainThinkEffort.OFF

    score = 0
    if len(text) >= 50:
        score += 1
    if len(text) >= 150:
        score += 1
    if len(snapshot.participants) >= 3:
        score += 1
    score += sum(1 for w in _EMOTION_WORDS if w in text)
    score += 2 * sum(1 for w in _PLOT_WORDS if w in text)

    if score == 0:
        effort = BrainThinkEffort.LOW
    elif score <= 2:
        effort = BrainThinkEffort.MID
    elif score <= 4:
        effort = BrainThinkEffort.HIGH
    else:
        effort = BrainThinkEffort.MAX
    _route_logger.debug(
        f"档位路由: score={score} 档位={effort.value} 输入={snapshot.content[:50]!r} "
        f"长度={len(text)} 参与者={len(snapshot.participants)}"
    )
    return effort


class BrainEngine:
    """大脑引擎。实现"接力思考"循环，显式控制推理深度。"""

    def __init__(
        self,
        sessions: SessionManager,
        persona: str = "",
        ooc: OOCDetector | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> None:
        self._logger = LoggerManager.get_logger("BRAIN")
        self._sessions = sessions
        self._persona = persona
        self._ooc = ooc
        self._tools = tools or []

    async def think(
        self,
        snapshot: SceneSnapshot,
        memories: list[MemoryItem],
        effort: BrainThinkEffort = BrainThinkEffort.OFF,
    ) -> BrainConclusion:
        """执行接力推理链，产出结构化结论。"""
        started = time.monotonic()
        self._logger.info(
            f"思考开始: 档位={effort.value} 输入={snapshot.sender}: {snapshot.content!r} "
            f"记忆={len(memories)}条 上下文={len(snapshot.context_snippet)}条"
        )
        if effort is BrainThinkEffort.OFF:
            conclusion = self._fast_path(snapshot)
        else:
            conclusion = await self._relay_think(snapshot, memories, effort)
            if (
                self._ooc is not None
                and conclusion.draft
                and self._ooc.pre_filter(conclusion.draft).is_ooc
            ):
                self._logger.warning(f"初稿被 OOC 规则拦截，丢弃初稿: {conclusion.draft!r}")
                conclusion = BrainConclusion(
                    verdict="pass_through",
                    intent=conclusion.intent,
                    emotion=conclusion.emotion,
                    confidence=conclusion.confidence,
                    effort=effort,
                    ooc_flag=True,
                    timestamp=conclusion.timestamp,
                )
        self._logger.info(
            f"思考完成: 档位={effort.value} 结论={conclusion.verdict} 意图={conclusion.intent} "
            f"情绪={conclusion.emotion} 置信度={conclusion.confidence:.2f} "
            f"耗时={time.monotonic() - started:.2f}s"
        )
        if conclusion.draft:
            self._logger.debug(f"行动指令: {conclusion.draft}")
        return conclusion

    async def _relay_think(
        self,
        snapshot: SceneSnapshot,
        memories: list[MemoryItem],
        effort: BrainThinkEffort,
    ) -> BrainConclusion:
        """接力思考主循环：每轮询问模型是否需要继续。"""
        max_rounds = self._get_max_rounds(effort)
        memory_text = "\n".join(f"- [{m.topic}] {m.content}" for m in memories) or "（无相关记忆）"
        context_text = "\n".join(snapshot.context_snippet[-5:]) or "（无上下文）"

        base_messages = [
            Message(role="system", content=prompt_mgr.render("brain.think")),
            Message(
                role="user",
                content=prompt_mgr.render(
                    "brain.think.user",
                    persona=self._persona or "（未提供）",
                    sender=snapshot.sender,
                    content=snapshot.content,
                    context=context_text,
                    memory=memory_text,
                ),
            ),
        ]

        current_round = 0
        previous_thought = ""
        current_action_hint = ""
        current_intent = ""
        current_emotion = ""
        current_confidence = 0.5
        final_reasoning = ""
        tool_result_cache = ""
        tool_used = False

        while True:
            current_round += 1
            self._logger.debug(f"思考接力第 {current_round}/{max_rounds} 轮")

            # 拼接当前 Prompt
            messages = list(base_messages)
            if previous_thought:
                messages.append(
                    Message(
                        role="assistant",
                        content=f"【第{current_round - 1}轮思考】\n{previous_thought}",
                    )
                )
                messages.append(
                    Message(
                        role="user",
                        content="请继续推理，无需重复已得结论，给出进一步推演或最终结论。",
                    )
                )

            # 如果有工具结果，补充给模型
            if tool_result_cache:
                messages.append(
                    Message(
                        role="user",
                        content=f"【工具执行结果】\n{tool_result_cache}\n请基于以上结果继续思考。",
                    )
                )
                tool_result_cache = ""

            try:
                result = await self._sessions.call(
                    "brain.think",
                    messages,
                    stateless=True,
                    temperature=0.4,
                    max_new_tokens=400,
                    tools=self._tools,
                )
            except Exception:
                self._logger.exception("推理调用失败，降级为快速路径")
                return self._fast_path(snapshot, effort)

            # 【调试】打印模型原始输出
            self._logger.debug(f"模型原始输出: {result.content}")

            parsed = self._parse_json(result.content)
            if not parsed:
                self._logger.warning(f"推理输出 JSON 解析失败: {result.content!r}")
                if current_round < max_rounds:
                    continue
                return self._fast_path(snapshot, effort)

            # 【统一工具调用处理】由 Provider 负责标准化格式
            result = self._sessions.normalize_tool_calls(result, parsed)

            # 执行工具调用（只负责执行，不负责解析格式）
            tool_results = []
            if result.tool_calls:
                tool_results = await self._execute_tool_calls(result.tool_calls)

            if tool_results:
                tool_result_cache = "\n".join(tool_results)
                tool_used = True
                self._logger.info(f"工具调用结果: {tool_result_cache[:200]!r}")

                # 强制模型进入下一轮思考
                need_continue = True
                previous_thought = str(parsed.get("thought", previous_thought))
                current_action_hint = str(parsed.get("action_hint", current_action_hint))
                current_intent = str(parsed.get("intent", current_intent))
                current_emotion = str(parsed.get("emotion", current_emotion))
                current_confidence = float(parsed.get("confidence", 0.5))
                final_reasoning = previous_thought

                # 如果是最后一轮，直接使用工具结果
                if current_round >= max_rounds:
                    self._logger.warning(
                        f"工具调用发生在最后一轮 ({current_round}/{max_rounds})，直接使用工具结果"
                    )
                    current_action_hint = (
                        f"{current_action_hint or ''}\n【工具结果】{tool_result_cache}"
                    )
                    break

                continue

            # 正常解析和处理
            previous_thought = str(parsed.get("thought", previous_thought))
            current_action_hint = str(parsed.get("action_hint", current_action_hint))
            current_intent = str(parsed.get("intent", current_intent))
            current_emotion = str(parsed.get("emotion", current_emotion))
            current_confidence = float(parsed.get("confidence", 0.5))
            need_continue = bool(parsed.get("need_continue_think", False))

            final_reasoning = previous_thought

            # 如果使用了工具但模型还没消化就结束，强制再思考一轮
            if tool_used and not need_continue and current_round < max_rounds:
                self._logger.info("工具结果尚未消化，强制追加一轮思考")
                need_continue = True

            if not need_continue:
                break
            if current_round >= max_rounds:
                self._logger.warning(f"思考接力达到最大轮数 {max_rounds}，强制结束")
                break

        # 最终保障：如果还有工具结果没被消化，直接嵌入结论
        if tool_result_cache:
            self._logger.info("工具结果未消化，嵌入行动指令")
            current_action_hint = f"{current_action_hint or ''}\n【工具结果】{tool_result_cache}"

        return BrainConclusion(
            verdict="draft" if current_action_hint else "pass_through",
            intent=current_intent,
            emotion=current_emotion,
            draft=current_action_hint or None,
            memory_refs=list(memories),
            confidence=current_confidence,
            effort=effort,
            reasoning=final_reasoning,
            timestamp=time.time(),
        )

    async def _execute_tool_calls(self, tool_calls: list[ToolCall]) -> list[str]:
        """执行工具调用，返回结果列表（只负责执行，不负责解析格式）"""
        results = []
        by_name = {t.tool_func.__name__: t for t in self._tools}

        for tc in tool_calls:
            tool = by_name.get(tc.name)
            if tool is None:
                results.append(f"错误: 未知工具 {tc.name}")
                self._logger.warning(f"未知工具调用: {tc.name}")
                continue

            # 解析参数
            try:
                args = msgspec.json.decode(tc.arguments or "{}", type=dict)
            except Exception:
                self._logger.warning(f"工具参数 JSON 解析失败: {tc.name}({tc.arguments!r})")
                args = {}

            # 执行工具
            try:
                if tool.is_async:
                    output = await tool.ainvoke(**args)
                else:
                    output = tool.invoke(**args)
                results.append(str(output))
                self._logger.info(f"工具调用: {tc.name}({args}) -> {str(output)[:80]!r}")
            except Exception as e:
                self._logger.exception(f"工具调用失败: {tc.name}({args}) -> {e}")
                results.append(f"错误: {type(e).__name__}: {str(e)}")

        return results

    def _get_max_rounds(self, effort: BrainThinkEffort) -> int:
        """将档位映射为最大思考轮数（收紧上限避免打转；仍留工具调用消化空间）。

        依据：小模型接力思考，轮次越多延迟越高；简单档多轮无收益。
        LOW 至少 2（给工具调用留一轮消化），复杂档给足但封闭上限，绝不 999 打转。
        """
        match effort:
            case BrainThinkEffort.LOW:
                return 2
            case BrainThinkEffort.MID:
                return 3
            case BrainThinkEffort.HIGH:
                return 5
            case BrainThinkEffort.MAX:
                return 8
            case _:
                return 0

    def _fast_path(
        self,
        snapshot: SceneSnapshot,
        effort: BrainThinkEffort = BrainThinkEffort.OFF,
    ) -> BrainConclusion:
        """零 token 快速路径：关键词判断意图，直接透传（保留请求档位标记）"""
        text = snapshot.content
        if any(g in text for g in ("你好", "早上好", "晚上好", "在吗", "hi", "hello")):
            intent, emotion = "回应打招呼", "友好"
        else:
            intent, emotion = "日常闲聊", "平淡"
        return BrainConclusion(
            verdict="pass_through",
            intent=intent,
            emotion=emotion,
            effort=effort,
            timestamp=time.time(),
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        """容错解析模型输出中的 JSON 对象，失败返回空 dict"""
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            return msgspec.json.decode(text[start : end + 1], type=dict)
        except Exception:
            return {}
