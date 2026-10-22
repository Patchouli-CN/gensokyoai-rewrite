"""前缀缓存 —— 把提示词冻成"字节级稳定"的一段，别让它被悄悄改掉。

云端的前缀缓存（DeepSeek 的 context cache 之类）只认**逐字节相同的前缀**：
只要开头有一个字符变了，后面再一样也全部按未命中计价。

提示词本来每轮重算一次——人设、场景规格、工具清单都是现拼的。拼出来的
内容通常一样，但"通常"不等于"一定"：工具顺序、空白、某个字段偶尔为空，
都会让前缀漂移，而漂移是**静默**的（账单才会告诉你）。

这个类就干两件事：
  1. 首次拼好就冻住，之后原样返回（**保证命中**）；
  2. 发现有人想改时记一次 drift 并报警——不改，只告诉你前缀已经不稳了。
"""

import hashlib
from collections.abc import Callable

_MARKS_PER_KEY = 4
""" 每个 key 最多留几次漂移样本（排查用） """


def _normalize(text: str) -> str:
    """统一换行与尾部空白：这些差异对模型没意义，对缓存是致命的。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def estimate_tokens(text: str) -> int:
    """粗估 token 数（中文约 1 字 1 token，英文约 4 字符 1 token）。

    Args:
        text: 提示词前缀

    Returns:
        int: 估算值（用于判断够不够 64 token 的缓存门槛）
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


class PromptCache:
    """按 key 冻结前缀文本，并报告漂移。"""

    def __init__(self, *, log=None, max_entries: int = 8) -> None:
        """初始化。

        Args:
            log: 日志器（漂移与冻结各记一行）
            max_entries: 最多冻几份前缀（房间多了也不用无限涨）
        """
        self._log = log
        self._max = max(1, max_entries)
        self._texts: dict[str, str] = {}
        self._digests: dict[str, str] = {}
        self._drifts: dict[str, list[str]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str, build: Callable[[], str]) -> str:
        """取前缀：第一次用 build() 的结果并冻住，之后原样返回缓存。

        Args:
            key: 前缀的身份（例如场景名；同 key 视为同一段前缀）
            build: 拼前缀的函数（只会在第一次、以及每次核对时调用）

        Returns:
            str: 字节级稳定的前缀
        """
        frozen = self._texts.get(key)
        if frozen is not None:
            self.hits += 1
            self._check(key, build)
            return frozen
        self.misses += 1
        text = _normalize(build())
        self._store(key, text)
        if self._log is not None:
            self._log.info(
                f"前缀已冻结 {key}: {len(text)} 字符 ≈ {estimate_tokens(text)} token"
                f"（{self._digests[key][:12]}）"
            )
        return text

    def invalidate(self, key: str | None = None) -> None:
        """丢掉缓存（改了人设或工具、确实想重建时才用）。

        Args:
            key: 只丢这一份；None = 全丢
        """
        if key is None:
            self._texts.clear()
            self._digests.clear()
            return
        self._texts.pop(key, None)
        self._digests.pop(key, None)

    def digest(self, key: str) -> str:
        """某个前缀的指纹（日志/面板上比对用）。"""
        return self._digests.get(key, "")

    def drifts(self, key: str) -> list[str]:
        """某个前缀最近的漂移样本（每次是"原本拼出来会是什么"的指纹）。"""
        return list(self._drifts.get(key, ()))

    @property
    def stats(self) -> dict:
        """一眼看懂的统计。"""
        return {
            "keys": len(self._texts),
            "hits": self.hits,
            "misses": self.misses,
            "drifts": sum(len(items) for items in self._drifts.values()),
            "tokens": sum(estimate_tokens(text) for text in self._texts.values()),
        }

    # ---------- 内部 ----------

    def _store(self, key: str, text: str) -> None:
        if len(self._texts) >= self._max and key not in self._texts:
            oldest = next(iter(self._texts))
            self._texts.pop(oldest, None)
            self._digests.pop(oldest, None)
        self._texts[key] = text
        self._digests[key] = hashlib.sha256(text.encode()).hexdigest()

    def _check(self, key: str, build: Callable[[], str]) -> None:
        """核对一次：拼出来跟冻住的不一样就记一次漂移（不换掉冻住的那份）。"""
        try:
            fresh = _normalize(build())
        except Exception:  # noqa: BLE001 - 核对失败不该影响对话
            return
        digest = hashlib.sha256(fresh.encode()).hexdigest()
        if digest == self._digests.get(key):
            return
        marks = self._drifts.setdefault(key, [])
        if digest not in marks:
            marks.append(digest)
            del marks[:-_MARKS_PER_KEY]
            if self._log is not None:
                self._log.warning(
                    f"前缀漂移 {key}: 现在会拼出 {digest[:12]}，"
                    f"但缓存用的是 {self._digests.get(key, '')[:12]}"
                    "（前缀一变，缓存全部重建）"
                )


class NullPromptCache(PromptCache):
    """不冻结的版本：每次都用最新拼出来的（改配置后立刻生效、放弃缓存）。"""

    def get(self, key: str, build: Callable[[], str]) -> str:
        """直接返回 build() 的结果。

        Args:
            key: 前缀身份（这里只用于统计）
            build: 拼前缀的函数

        Returns:
            str: 每次新拼的前缀
        """
        del key
        self.misses += 1
        return _normalize(build())


__all__ = ["NullPromptCache", "PromptCache", "estimate_tokens"]
