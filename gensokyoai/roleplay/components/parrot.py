"""文风防复读守门 —— 治小模型「重复自己的模板/口头禅」的有状态组件。

两道防线的有状态部分都在这里：

1. **预防性提示**（流式/缓冲路径都生效）：`avoid_hint()` 把「你刚才怎么说
   的、哪些收尾用腻了」交给生成器——零额外模型调用；
2. **兜底守卫**（缓冲路径）：`guard()` 相似度超阈值花一次纠偏重写；
   `dedup_ending()` 同一收尾近期用够次数就剥掉（确定性规则，零 token）。

纯函数（相似度 / 收尾指纹 / 提示拼装）在 `core/responder/anti_parrot.py`，
本组件持有窗口状态，负责把它们接成一条可独立测试的管线。
"""

from collections import deque

from ...core.config import StyleSettings
from ...core.responder.anti_parrot import (
    PARROT_REASON,
    avoid_hint,
    ending_key,
    should_strip_ending,
    similarity,
    strip_ending,
)
from ...core.responder.generator import Responder
from ...utils.logger import LoggerManager


class ParrotGuard:
    """防复读守门：预防提示 + 相似度过线纠偏 + 收尾去重 + 状态回填。"""

    def __init__(self, style: StyleSettings, responder: Responder) -> None:
        """初始化。

        Args:
            style: 文风防复读配置（窗口大小 / 阈值 / 开关）
            responder: 表达层；guard() 命中相似度阈值时花一次 correct() 纠偏
        """
        self._logger = LoggerManager.get_logger("PARROT")
        self._style = style
        self._responder = responder
        self._last_reply = ""
        """ 上一轮回复原文（防复读提示/相似度检测的比对基准） """
        self._recent_replies: deque[str] = deque(maxlen=style.reply_window)
        """ 近期回复原文窗口（隔轮复读判定用，见 StyleSettings.reply_window） """
        self._recent_endings: deque[str] = deque(maxlen=style.ending_window)
        """ 近期收尾指纹窗口（ported qqbot「不复读结尾」规则） """

    def avoid_hint(self) -> str:
        """防复读预防性提示（responder.user 的 [自我克制] 段）。

        Returns:
            str: 提示文本；上一轮无发言且无重复收尾时为空串（不注入）
        """
        return avoid_hint(self._last_reply, self._recent_endings, list(self._recent_replies))

    async def guard(self, reply: str) -> str:
        """近期窗口内相似度过阈值 -> 一次防复读纠偏重写（缓冲路径）。

        流式路径文本已在投递中、无从改起，只做事前提示（见 `avoid_hint`）；
        这里兜底覆盖缓冲投递（CLI / 非流式口层）。比对基准是近期回复窗口
        （reply_window），不只是相邻轮——隔一轮原句复读（A→B→A）也能照出来。

        Args:
            reply: 本轮新生成的回复

        Returns:
            str: 守门后的回复（重写更优返回新文本，否则原样）
        """
        if self._style.similarity_retry <= 0:
            return reply
        candidates = [r for r in self._recent_replies if r]
        if self._last_reply and self._last_reply not in candidates:
            candidates.append(self._last_reply)
        if not candidates:
            return reply
        ratio, baseline = max((similarity(reply, r), r) for r in candidates)
        if ratio < self._style.similarity_retry:
            return reply
        self._logger.warning(f"防复读守门: 与近轮相似度 {ratio:.0%} 超阈值，发起一次重写")
        try:
            rewritten = await self._responder.correct(reply, PARROT_REASON)
        except Exception:
            self._logger.exception("防复读重写失败，保留原句")
            return reply
        if rewritten and similarity(rewritten, baseline) < ratio:
            return rewritten
        self._logger.info("防复读重写后仍高于原相似度，保留原句")
        return reply

    def dedup_ending(self, reply: str) -> str:
        """收尾去重（ported qqbot「不复读结尾」规则）：同一收尾在近期窗口
        已出现 ≥2 次就剥掉它。确定性规则，零 token；一句话的回复不剥。

        Args:
            reply: 待处理的回复

        Returns:
            str: 剥掉反复收尾的文本（或原样）
        """
        if not self._style.dedup_endings:
            return reply
        if should_strip_ending(reply, list(self._recent_endings)):
            stripped = strip_ending(reply)
            if stripped:
                self._logger.info(f"收尾去重: 剥掉反复使用的收尾 {ending_key(reply)!r}")
                return stripped
        return reply

    def note_reply(self, reply: str) -> None:
        """更新防复读状态：比对基准 + 收尾指纹窗口（记原始收尾——倾向追踪，
        模型想用什么梗是它的本能，剥不剥是我们的事）。

        Args:
            reply: 本轮最终回复原文
        """
        self._last_reply = reply
        self._recent_replies.append(reply)
        if self._style.dedup_endings:
            self._recent_endings.append(ending_key(reply))
