""" 回复生成 —— 拿 Brain 结论生成最终回复（架构文档 §3.3）"""

import time

from ..session_manager import SessionManager
from ...prompts import prompt_mgr
from ...schemas.brain_schema import BrainConclusion
from ...schemas.memory_schema import MemoryItem
from ...schemas.model_schema import Message
from ...schemas.scene_schema import SceneSnapshot
from ...utils.logger import LoggerManager
from .style import apply_emotion_hint

class Responder:
    """ 表达层：只做表达，不做思考。

    使用 owner="responder" 的有状态会话（滑动窗口），system 前缀稳定
    以利用本地推理的 KV cache 前缀复用。
    """

    _OWNER = "responder"

    def __init__(self, sessions: SessionManager, persona: str = "") -> None:
        """ 初始化。

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
        """ 在有状态会话中生成最终回复。 """
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

    async def stall(self, snapshot: SceneSnapshot) -> str:
        """ 生成一句角色口吻的思考过渡语（如"唔……让我想想"）。

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
            "responder.stall", sender=snapshot.sender, content=snapshot.content,
        )
        result = await self._sessions.call(
            self._OWNER, [Message(role="user", content=user)],
            max_new_tokens=48, temperature=0.95,
        )
        line = result.content.strip().strip('"“” \n')
        self._logger.info(
            f"过渡语生成: {line!r} 耗时={time.monotonic() - started:.2f}s"
        )
        return line

    async def correct(self, bad_reply: str, reason: str) -> str:
        """ OOC 纠偏重生成：告知模型刚才哪里出戏，重新以角色身份回复。

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
            self._OWNER, [Message(role="user", content=user)],
            max_new_tokens=1024, temperature=0.8,
        )
        reply = result.content.strip()
        self._logger.info(
            f"纠偏完成: {len(reply)}字 耗时={time.monotonic() - started:.2f}s 回复={reply!r}"
        )
        return reply
