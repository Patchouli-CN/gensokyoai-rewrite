"""前缀缓存管理：把提示词切成"系统块 / 已稳定消息 / 尾巴"三区，对齐缓存粒度。

    [系统块 _fixed]  工具 + 系统提示词，永不变，compact 不碰
    [历史 _fixed_msg] stabilize 推上来的消息，compact 只动这里
    [尾巴 _volatile]  这一轮还在动的消息

设计前提：说出去的话不撤回——进了 `_fixed_msg` 就不再修改，前缀因此单调
增长，每轮只有尾巴是新的。压缩只替换历史那一段，系统块（通常最大、最贵）
仍然命中。

    cache = PromptCache(name="ds模型缓存", fixed=64)
    cache.stable_fixed("persona", 人设)        # 永不变
    cache.add("帕秋莉：借我书")                 # 尾巴
    cache.stabilize()                          # 一轮结束，按 64 token 对齐推入历史
    cache.compact("（往事概要）……", keep=3)      # 只压缩历史，系统块不动
"""

import difflib
import hashlib

_GRID = 64
""" 云端前缀缓存的粒度（DeepSeek 等按 64 token 对齐） """


def estimate_tokens(text: str) -> int:
    """粗估 token（中文约 1 字 1 token，其余约 4 字符 1 token）。

    Args:
        text: 待估算的文本

    Returns:
        int: 估算值（只用于对齐，不要求精确）
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return cjk + (len(text) - cjk) // 4


class PromptCache:
    """系统块 / 已稳定消息 / 尾巴，三区管理。"""

    def __init__(self, name: str = "prompt", fixed: int = _GRID, log=None) -> None:
        """初始化。

        Args:
            name: 进日志与统计的名字（多份缓存时区分是谁）
            fixed: 对齐粒度（token）；0 表示不对齐、每轮全推
            log: 日志器（前移、压缩、裂痕各记一行）
        """
        self.name = name
        self.fixed = max(0, fixed)
        self._log = log
        self._fixed: list[tuple[str, str]] = []
        self._fixed_index: dict[str, str] = {}
        self._fixed_msg: list[str] = []
        self._volatile: list[str] = []
        self._baseline: tuple[str, ...] | None = None
        self.stabilizes = 0
        self.epoch = 1
        self.compacts = 0

    # ---------- 系统块 ----------

    def stable_fixed(self, name: str, value: str) -> None:
        """固定一个系统块；同名不同值会被拒绝（前缀漂移主要来自这里）。

        Args:
            name: 块名（persona / protocol / tools）
            value: 块内容

        Raises:
            ValueError: 同名却给了不同的值
        """
        known = self._fixed_index.get(name)
        if known is not None:
            if known != value:
                raise ValueError(
                    f"系统块 {self.name}/{name} 已冻住，不许改成别的值"
                    "（要换就换名字，或者新建一份 PromptCache）"
                )
            return
        self._fixed.append((name, value))
        self._fixed_index[name] = value

    def clear(self) -> None:
        """整份丢掉（系统块变了、不得不重建时才用；历史也一起清）。"""
        self._fixed.clear()
        self._fixed_index.clear()
        self._fixed_msg.clear()
        self._volatile.clear()
        self._baseline = None
        self.epoch += 1
        if self._log is not None:
            self._log.warning(f"前缀 {self.name} 整份重建（进入第 {self.epoch} 代）")

    # ---------- 消息 ----------

    def add(self, value: str) -> None:
        """往尾巴追加一条（新消息、工具结果……）。

        Args:
            value: 块内容
        """
        self._volatile.append(value)

    def rollback(self, idx: int) -> None:
        """从尾巴撤回：丢掉 idx 及其之后的块。

        Args:
            idx: 尾巴下标（0 起；负数从末尾算）

        Raises:
            IndexError: 越界（已稳定的历史不可撤，它在另一个区里）
        """
        if idx < 0:
            idx += len(self._volatile)
        if not 0 <= idx <= len(self._volatile):
            raise IndexError(f"尾巴只有 {len(self._volatile)} 条，给的是 {idx}")
        del self._volatile[idx:]

    # ---------- 稳定 ----------

    def stabilize(self) -> int:
        """把尾巴尽可能多地推入历史，切点按 `fixed` 向下对齐。

        系统块不动、尾巴只移动到历史区——`build()` 永远是裸拼接。

        Returns:
            int: 这次推进去了几条（0 = 不够一个对齐格子，留给下一轮）
        """
        target = self._aligned_end()
        run = self.prefix_tokens  # 系统块也算进已固定的量
        index = 0
        while index < len(self._volatile):
            step = run + estimate_tokens(self._volatile[index])
            if step > target:
                break
            run = step
            index += 1
        if index:
            self._fixed_msg.extend(self._volatile[:index])
            del self._volatile[:index]
            self.stabilizes += 1
            if self._log is not None:
                self._log.info(
                    f"前缀 {self.name} 前移 {index} 块 → "
                    f"历史 {self.history_tokens} token"
                    f"（对齐到 {self.fixed}）"
                )
        self._baseline = tuple(self._prefix_hashes())
        return index

    def compact(self, summary: str, keep: int = 0) -> int:
        """把历史压成一段摘要，只动 `_fixed_msg`——系统块一个字都不改。

        Args:
            summary: 概要正文（空字符串 = 直接丢弃旧历史）
            keep: 保留最近几条不压

        压缩之后的那一次上传本身就是"缓存创建"：系统块照旧命中，历史部分
        从零计费。所以这里把基线清空——在新一代建立基线之前，`report()`
        返回 None 是**预期行为**，不是漏检。

        Returns:
            int: 被替换掉的历史条数（0 = 没有可压的）
        """
        keep = max(0, min(keep, len(self._fixed_msg)))
        dropped = len(self._fixed_msg) - keep
        if dropped <= 0:
            return 0
        tail = self._fixed_msg[dropped:]
        self._fixed_msg = ([summary] if summary else []) + tail
        self.epoch += 1
        self.compacts += 1
        self._baseline = None
        if self._log is not None:
            self._log.info(
                f"前缀 {self.name} 进入第 {self.epoch} 代：压缩 {dropped} 条"
                f"（系统块 {self.system_tokens} token 照旧命中）"
            )
        return dropped

    # ---------- 产出 ----------

    def transcript(self) -> str:
        """只取消息区（历史 + 尾巴），不含系统块——拼在 user 消息最前面。

        Returns:
            str: 空字符串表示还没有任何消息
        """
        texts = list(self._fixed_msg) + list(self._volatile)
        return "\n".join(texts)

    def build(self) -> str:
        """拼出完整提示词（系统块 + 历史 + 尾巴），不做任何预处理。

        Returns:
            str: 拼接结果
        """
        return "\n".join(self._texts())

    def report(self) -> str | None:
        """核对前缀是否被破坏：正常应为 None（前缀只增不改）。

        基线只由 `stabilize()` 建立。刚 `compact()` 完、还没 stabilize 时
        没有可比对象，返回 None——那时整段历史正在重建，本来就"全部无效"。

        Returns:
            str | None: None = 与基线一致或还没有基线；否则是 difflib 报告
        """
        if self._baseline is None:
            return None
        now = tuple(self._prefix_hashes())
        if now == self._baseline:
            return None
        index = self._first_difference(now)
        texts = self._prefix_texts()
        report = "\n".join(
            difflib.unified_diff(
                "\n".join(texts[:index]).splitlines(),
                "\n".join(texts[: index + 1]).splitlines(),
                fromfile=f"{self.name}/基线",
                tofile=f"{self.name}/现在",
                lineterm="",
            )
        )
        return report or f"前缀 {self.name} 在第 {index} 块处不同"

    # ---------- 读数 ----------

    @property
    def system_tokens(self) -> int:
        """系统块（工具 + 系统提示词）的 token 估算。"""
        return sum(estimate_tokens(value) for _, value in self._fixed)

    @property
    def history_tokens(self) -> int:
        """已稳定历史的 token 估算（compact 的作用范围）。"""
        return self._history_tokens()

    @property
    def tail_tokens(self) -> int:
        """尾巴（还没稳定的消息）的 token 估算。"""
        return sum(estimate_tokens(text) for text in self._volatile)

    @property
    def prefix_tokens(self) -> int:
        """系统块 + 历史的 token 估算（= 除尾巴外的全部）。"""
        return self.system_tokens + self.history_tokens

    @property
    def total_tokens(self) -> int:
        """整段提示词的 token 估算。"""
        return self.prefix_tokens + self.tail_tokens

    @property
    def aligned_tokens(self) -> int:
        """按 `fixed` 对齐后，云端实际能复用的 token 数。"""
        if self.fixed <= 0:
            return self.prefix_tokens
        return self.prefix_tokens // self.fixed * self.fixed

    @property
    def waste_tokens(self) -> int:
        """固定了却落在对齐格子外的部分。"""
        return self.prefix_tokens - self.aligned_tokens

    @property
    def fixed_names(self) -> tuple[str, ...]:
        """已冻结的系统块名（顺序即出现顺序）。"""
        return tuple(name for name, _ in self._fixed)

    @property
    def history_blocks(self) -> tuple[str, ...]:
        """已稳定历史（只读快照）。"""
        return tuple(self._fixed_msg)

    def stats(self) -> dict:
        """一眼看懂的统计（日志/面板用）。"""
        return {
            "name": self.name,
            "fixed": self.fixed,
            "epoch": self.epoch,
            "system_tokens": self.system_tokens,
            "history_tokens": self.history_tokens,
            "tail_tokens": self.tail_tokens,
            "prefix_tokens": self.prefix_tokens,
            "aligned_tokens": self.aligned_tokens,
            "waste_tokens": self.waste_tokens,
            "history_blocks": len(self._fixed_msg),
            "stabilizes": self.stabilizes,
            "compacts": self.compacts,
        }

    # ---------- 内部 ----------

    def _texts(self) -> list[str]:
        return self._prefix_texts() + list(self._volatile)

    def _prefix_texts(self) -> list[str]:
        return [value for _, value in self._fixed] + list(self._fixed_msg)

    def _prefix_hashes(self) -> list[str]:
        return [
            hashlib.sha256(text.encode()).hexdigest()[:16]
            for text in self._prefix_texts()
        ]

    def _history_tokens(self) -> int:
        return sum(estimate_tokens(text) for text in self._fixed_msg)

    def _aligned_end(self) -> int:
        """对齐后的可固定上限（整段提示词向下取整到 fixed 的倍数）。"""
        total = self.total_tokens
        if self.fixed <= 0:
            return total
        return total // self.fixed * self.fixed

    def _first_difference(self, hashes: tuple[str, ...]) -> int:
        if self._baseline is None:
            return 0
        for index, (left, right) in enumerate(
            zip(self._baseline, hashes, strict=False)
        ):
            if left != right:
                return index
        return min(len(self._baseline), len(hashes))


__all__ = ["PromptCache", "estimate_tokens"]
