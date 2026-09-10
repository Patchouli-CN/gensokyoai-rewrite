"""OOC 检测 —— 前置规则快筛 + 后置模型深审（架构文档 §3.2）"""

import time

import msgspec

from ...prompts import prompt_mgr
from ...schemas.brain_schema import OOCVerdict
from ...schemas.model_schema import Message
from ...utils.logger import LoggerManager
from ..session_manager import SessionManager

_OOC_PATTERNS = ("作为一个AI", "作为一个 AI", "语言模型", "人工智能助手", "抱歉，我不能")
""" 出现即判定 OOC 的模型自曝式话术 """


class OOCDetector:
    """OOC Verdict：检测回复是否偏离角色人设"""

    def __init__(self, sessions: SessionManager) -> None:
        self._logger = LoggerManager.get_logger("OOC")
        self._sessions = sessions

    def pre_filter(self, text: str) -> OOCVerdict:
        """规则快筛，零 token；命中才值得进 audit。

        Args:
            text: 待检测文本

        Returns:
            OOCVerdict: 命中自曝式话术则 is_ooc=True
        """
        for pattern in _OOC_PATTERNS:
            if pattern in text:
                self._logger.warning(f"OOC 规则命中: {pattern}")
                return OOCVerdict(is_ooc=True, confidence=0.9, reason=f"命中规则: {pattern}")
        self._logger.debug(f"OOC 快筛放行: {text[:50]!r}")
        return OOCVerdict()

    async def audit(self, draft: str, persona: str) -> OOCVerdict:
        """独立 VirtualSession 的模型深审（owner="brain.ooc" 无状态调用）。

        Args:
            draft: 待审计的回复文本
            persona: 角色人设

        Returns:
            OOCVerdict: 审计结论；调用或解析失败返回未触发 OOC 的默认值
        """
        started = time.monotonic()
        self._logger.info(f"OOC 深审开始: 待审文本={draft[:60]!r}")
        try:
            result = await self._sessions.call(
                "brain.ooc",
                [
                    Message(role="system", content=prompt_mgr.render("ooc.audit")),
                    Message(
                        role="user",
                        content=prompt_mgr.render(
                            "ooc.audit.user",
                            persona=persona,
                            draft=draft,
                        ),
                    ),
                ],
                stateless=True,
                temperature=0.1,
                max_new_tokens=150,
            )
        except Exception:
            self._logger.exception("OOC 深审调用失败，按未触发处理")
            return OOCVerdict()

        start, end = result.content.find("{"), result.content.rfind("}")
        if start == -1 or end <= start:
            return OOCVerdict()
        try:
            parsed = msgspec.json.decode(result.content[start : end + 1], type=dict)
        except Exception:
            return OOCVerdict()

        verdict = OOCVerdict(
            is_ooc=bool(parsed.get("is_ooc", False)),
            confidence=float(parsed.get("confidence", 0.5)),
            reason=str(parsed.get("reason", "")),
        )
        if verdict.is_ooc:
            self._logger.warning(f"OOC 深审判定出戏: {verdict.reason}")
        self._logger.info(
            f"OOC 深审完成: 出戏={verdict.is_ooc} 置信度={verdict.confidence:.2f} "
            f"耗时={time.monotonic() - started:.2f}s"
        )
        return verdict
