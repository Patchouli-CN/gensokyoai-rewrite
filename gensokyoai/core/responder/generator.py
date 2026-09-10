"""回复生成 —— 拿 Brain 结论生成最终回复（架构文档 §3.3）"""

import time

from ...prompts import prompt_mgr
from ...schemas.brain_schema import BrainConclusion
from ...schemas.memory_schema import MemoryItem
from ...schemas.model_schema import Message
from ...schemas.scene_schema import SceneSnapshot
from ...utils.logger import LoggerManager
from ..session_manager import SessionManager
from .style import apply_emotion_hint


class Responder:
    """表达层：只做表达，不做思考。

    使用 owner="responder" 的有状态会话（滑动窗口），system 前缀稳定
    以利用本地推理的 KV cache 前缀复用。
    """

    _OWNER = "responder"

    def __init__(self, sessions: SessionManager, persona: str = "") -> None:
        """初始化。

        Args:
            sessions: 上下文隔离层（由 L4 注入）
            persona: 角色人设 system prompt（由 L4 从 roleplay 加载后传入）
        """
        self._logger = LoggerManager.get_logger("RESPONDER")
        self._sessions = sessions
        self._persona = persona
        self._system_ready = False

    async def respond(
        self,
        conclusion: BrainConclusion,
        snapshot: SceneSnapshot,
        memories: list[MemoryItem],
    ) -> str:
        """在有状态会话中生成最终回复。"""
        started = time.monotonic()
        self._logger.info(
            f"生成开始: 意图={conclusion.intent} 情绪={conclusion.emotion} "
            f"初稿={'有' if conclusion.draft else '无'} 记忆={len(memories)}条 "
            f"档位={conclusion.effort.value}"
        )
        if not self._system_ready and self._persona:
            self._sessions.set_system(self._OWNER, self._persona)
            self._system_ready = True

        memory_text = "\n".join(f"- {m.content}" for m in memories) or "（无）"

        # 【修复】过滤掉假的工具调用
        draft_hint = f"初稿参考（可改写润色）: {conclusion.draft}\n" if conclusion.draft else ""
        if conclusion.draft and '"tool"' in conclusion.draft:
            self._logger.warning(f"检测到伪工具调用文本，丢弃: {conclusion.draft}")
            draft_hint = ""

        user = prompt_mgr.render(
            "responder.user",
            sender=snapshot.sender,
            content=snapshot.content,
            intent=conclusion.intent,
            emotion=conclusion.emotion,
            draft_hint=draft_hint,
            memory=memory_text,
        )
        result = await self._sessions.call(
            self._OWNER,
            [Message(role="user", content=user)],
            max_new_tokens=1024,
            temperature=0.8,
        )
        content = result.content.strip()

        # 半截续写：小模型长回复常见被 max_tokens 截断，续写一次拼回完整文本
        if result.finish_reason == "length" and content:
            self._logger.info(f"回复被截断（{len(content)}字），发起续写")
            cont = await self._sessions.call(
                self._OWNER,
                [Message(role="user", content="继续，从中断处直接接续写完。不要重复已写内容。")],
                max_new_tokens=1024,
                temperature=0.8,
            )
            content = f"{content}{cont.content.strip()}".strip()

        reply = apply_emotion_hint(content, conclusion.emotion)
        self._logger.info(
            f"生成完成: {len(reply)}字 耗时={time.monotonic() - started:.2f}s 回复={reply!r}"
        )
        return reply

    async def respond_stream(
        self,
        conclusion: BrainConclusion,
        snapshot: SceneSnapshot,
        memories: list[MemoryItem],
    ):
        """流式生成最终回复：逐块 yield 文本 delta（供 mouth.begin/delta/end 投递）。

        与 `respond()` 走同一套 prompt / 有状态会话，但经 backend.chat_stream 逐块产出。
        支持半截续写（finish_reason=length 时继续流式拼接）与情绪润色尾缀（末块补标点）。

        Yields:
            str: 文本增量
        """
        started = time.monotonic()
        self._logger.info(
            f"流式生成开始: 意图={conclusion.intent} 情绪={conclusion.emotion} "
            f"初稿={'有' if conclusion.draft else '无'} 记忆={len(memories)}条 档位={conclusion.effort.value}"
        )
        if not self._system_ready and self._persona:
            self._sessions.set_system(self._OWNER, self._persona)
            self._system_ready = True

        memory_text = "\n".join(f"- {m.content}" for m in memories) or "（无）"
        draft_hint = f"初稿参考（可改写润色）: {conclusion.draft}\n" if conclusion.draft else ""
        if conclusion.draft and '"tool"' in conclusion.draft:
            self._logger.warning(f"检测到伪工具调用文本，丢弃: {conclusion.draft}")
            draft_hint = ""

        user = prompt_mgr.render(
            "responder.user",
            sender=snapshot.sender,
            content=snapshot.content,
            intent=conclusion.intent,
            emotion=conclusion.emotion,
            draft_hint=draft_hint,
            memory=memory_text,
        )

        parts: list[str] = []
        finish = "stop"

        async for ev in self._sessions.call_stream(
            self._OWNER,
            [Message(role="user", content=user)],
            max_new_tokens=1024,
            temperature=0.8,
        ):
            if ev.delta:
                parts.append(ev.delta)
                yield ev.delta
            if ev.finish_reason:
                finish = ev.finish_reason

        # 半截续写：小模型长回复被 max_tokens 截断时发起一次流式接续
        if finish == "length":
            self._logger.info(f"回复被截断（{len(''.join(parts))}字），发起流式续写")
            async for ev in self._sessions.call_stream(
                self._OWNER,
                [Message(role="user", content="继续，从中断处直接接续写完。不要重复已写内容。")],
                max_new_tokens=1024,
                temperature=0.8,
            ):
                if ev.delta:
                    parts.append(ev.delta)
                    yield ev.delta
                if ev.finish_reason:
                    finish = ev.finish_reason

        # 情绪润色尾缀：如愤怒补"！"、疑问补"？"，仅结尾追加，不打断流式
        content = "".join(parts).strip()
        reply = apply_emotion_hint(content, conclusion.emotion)
        if len(reply) > len(content) and reply.endswith("！") and not content.endswith("！"):
            yield reply[len(content) :]
        self._logger.info(
            f"流式生成完成: {len(reply)}字 耗时={time.monotonic() - started:.2f}s 回复={reply!r}"
        )

    async def stall(self, snapshot: SceneSnapshot) -> str:
        """生成一句角色口吻的思考过渡语（如"唔……让我想想"）。

        与正式回复共用同一个有状态会话：过渡语先入历史，正式回复
        能看到自己说过它，自然承接而不重复。小 token + 高温度，秒回。

        Args:
            snapshot: 触发深度思考的场景快照

        Returns:
            过渡语文本；生成结果为空时返回空字符串
        """
        started = time.monotonic()
        if not self._system_ready and self._persona:
            self._sessions.set_system(self._OWNER, self._persona)
            self._system_ready = True
        user = prompt_mgr.render(
            "responder.stall",
            sender=snapshot.sender,
            content=snapshot.content,
        )
        result = await self._sessions.call(
            self._OWNER,
            [Message(role="user", content=user)],
            max_new_tokens=48,
            temperature=0.95,
        )
        line = result.content.strip().strip('"“” \n')
        self._logger.info(f"过渡语生成: {line!r} 耗时={time.monotonic() - started:.2f}s")
        return line

    async def correct(self, bad_reply: str, reason: str) -> str:
        """OOC 纠偏重生成：告知模型刚才哪里出戏，重新以角色身份回复。

        有状态会话里"坏回复"已在历史中，这里只需追加纠偏指令。

        Args:
            bad_reply: 被拦截的出戏回复
            reason: OOC 判定理由

        Returns:
            重新生成的回复文本
        """
        started = time.monotonic()
        self._logger.warning(f"OOC 纠偏重生成: 原因={reason!r} 原回复={bad_reply[:60]!r}")
        user = prompt_mgr.render("responder.correct", bad_reply=bad_reply, reason=reason)
        result = await self._sessions.call(
            self._OWNER,
            [Message(role="user", content=user)],
            max_new_tokens=1024,
            temperature=0.8,
        )
        reply = result.content.strip()
        self._logger.info(
            f"纠偏完成: {len(reply)}字 耗时={time.monotonic() - started:.2f}s 回复={reply!r}"
        )
        return reply
