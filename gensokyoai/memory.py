"""记忆（Memory）—— 写入按注意力强度，召回注记进房间，窗口压力下压缩。

存储与房间同一哲学：每房间一个 JSONL 文件，只追加。召回先用子串匹配 +
新近度（向量召回等 embedder 端口以后再开旋钮——不为以后预留）。
"""

import asyncio
import os
import time
from pathlib import Path

import msgspec

from .model import ChatModel
from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("MEMORY")

_MAX_ITEMS = 60
""" 窗口压力线：条数超了就该整理了（DMN 调 consolidate） """


class MemoryItem(msgspec.Struct, frozen=True):
    """一条记忆。"""

    text: str
    importance: float = 0.5
    ts: float = msgspec.field(default_factory=time.time)


class Memory:
    """一个房间的记忆库（JSONL 只追加）。"""

    def __init__(self, path: Path | None) -> None:
        """初始化。

        Args:
            path: JSONL 落盘路径；None = 纯内存（测试/控制台用）
        """
        self._path = path
        self._items: list[MemoryItem] = []
        if path is not None and path.exists():
            self._load(path)

    def _load(self, path: Path) -> None:
        """逐行加载，坏行跳过。"""
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            text = line.strip()
            if not text:
                continue
            try:
                self._items.append(msgspec.json.decode(text, type=MemoryItem))
            except msgspec.DecodeError:
                _logger.warning(f"记忆文件坏行已跳过: {path}:{number}")

    @property
    def size(self) -> int:
        """当前条数。"""
        return len(self._items)

    def needs_consolidation(self) -> bool:
        """窗口压力：条数超线（DMN 据此排整理）。"""
        return len(self._items) > _MAX_ITEMS

    async def note(self, text: str, importance: float) -> None:
        """记一笔（低强度的附和直接不记）。"""
        if importance < 0.3 or not text.strip():
            return
        item = MemoryItem(text=text.strip(), importance=importance)
        self._items.append(item)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = msgspec.json.encode(item).decode() + "\n"
            await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        path = self._path
        if path is None:
            return
        with path.open("a", encoding="utf-8") as file:
            file.write(line)

    async def recall(self, query: str, limit: int = 3) -> list[str]:
        """按当前话题召回：bigram 重合打分，同分重要性/新者优先。

        CJK 无空格分词，就用两字滑窗——「帕秋莉最近借了什么书」拆出
        「帕秋/秋莉/…/借了」，命中记忆里的同名片段即相关。
        """
        grams = _bigrams(query)
        if not grams:
            return []
        hits = [
            (len(grams & _bigrams(item.text)), item.importance, item.ts, item.text)
            for item in self._items
        ]
        hits = [hit for hit in hits if hit[0] > 0]
        hits.sort(key=lambda hit: (hit[0], hit[1], hit[2]), reverse=True)
        return [text for _, _, _, text in hits[:limit]]

    async def consolidate(self, model: ChatModel, keep: int = 20) -> None:
        """窗口整理：让模型把旧记忆压缩成一段概要，文件重写（保留最近 keep 条）。

        Args:
            model: 压缩用的模型端点
            keep: 原样保留的最近条数
        """
        if len(self._items) <= keep:
            return
        old, recent = self._items[:-keep], self._items[-keep:]
        digest_source = "\n".join(f"- {item.text}" for item in old)
        reply = await model.chat(
            "你是记忆整理员。把零散的对话记忆压缩成一段连贯概要，"
            "保留人名、约定、事实与情感转折，丢弃寒暄与重复。直接输出概要。",
            digest_source,
            max_tokens=600,
            temperature=0.3,
        )
        self._items = [
            MemoryItem(text=f"（往事概要）{reply.text}", importance=0.8),
            *recent,
        ]
        if self._path is not None:
            payload = "".join(
                msgspec.json.encode(item).decode() + "\n" for item in self._items
            )
            await asyncio.to_thread(self._rewrite_atomic, payload)

    def _rewrite_atomic(self, payload: str) -> None:
        """写临时文件再原子替换，失败时旧文件保留。"""
        path = self._path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def _bigrams(text: str) -> set[str]:
    """两字滑窗集合（CJK 友好的相关性度量单位）。"""
    return {text[i : i + 2] for i in range(len(text) - 1)}
