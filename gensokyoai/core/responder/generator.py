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
        reply = apply_emotion_hint(result.content.strip(), conclusion.emotion)
        self._logger.info(
            f"生成完成: {len(reply)}字 耗时={time.monotonic() - started:.2f}s 回复={reply!r}"
        )
        return reply
