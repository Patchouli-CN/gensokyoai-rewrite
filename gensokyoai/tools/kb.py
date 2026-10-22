"""知识库 —— 查过的东西存下来，下次先翻这里。

一条记录 = 一次查询 + 当时搜到的几条结果。按字符 bigram 重合度找相近的旧查询，
所以「觉的能力」和「觉有什么能力」能对上。

作用域两种：`global`（所有群共用）或 `room`（一个群一个文件）。
"""

import json
import time
from pathlib import Path

from ..paths import storage_key

DEFAULT_ROOT = Path("data/knowledge")
""" 默认放在数据目录下的 knowledge/ """

_MAX_ENTRIES = 2000
_TTL_S = 30 * 86400.0

_NOISE = (
    "什么",
    "怎么",
    "如何",
    "为什么",
    "多少",
    "哪些",
    "是不",
    "不是",
)
""" 问句里的口水词：去掉再比，免得「觉的能力」和「觉有什么能力」对不上 """
_PARTICLES = "的了吗呢吧啊呀是有在和与我你他它这那"


def _normalize(text: str) -> str:
    """去掉问句口水词与虚词，只留有信息量的部分。"""
    cleaned = text.lower()
    for word in _NOISE:
        cleaned = cleaned.replace(word, "")
    kept = "".join(ch for ch in cleaned if ch.isalnum() and ch not in _PARTICLES)
    return kept or cleaned


def _bigrams(text: str) -> set[str]:
    """字符二元组（中文不需要分词）。"""
    cleaned = _normalize(text)
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)} or {cleaned}


def _score(left: str, right: str) -> float:
    """两个查询的相似度（bigram 重合 / 较短的集合大小）。"""
    one, two = _bigrams(left), _bigrams(right)
    if not one or not two:
        return 0.0
    return len(one & two) / min(len(one), len(two))


class KnowledgeBase:
    """一个作用域的只追加知识库。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        scope: str = "global",
        room: str = "",
        max_entries: int = _MAX_ENTRIES,
        clock=time.time,
    ) -> None:
        """初始化。

        Args:
            root: 根目录（None = data/knowledge）
            scope: global（所有群共用）或 room（一个群一个文件）
            room: 作用域为 room 时的房间名
            max_entries: 每份文件最多留多少条（超了从老到新丢）
            clock: 时间源（测试注入）
        """
        self.scope = scope if scope in ("global", "room") else "global"
        base = Path(root) if root is not None else DEFAULT_ROOT
        name = "global" if self.scope == "global" or not room else storage_key(room)
        self.path = base / f"{name}.jsonl"
        self._clock = clock
        self._max = max(1, max_entries)

    def recall(self, query: str, limit: int = 2, *, threshold: float = 0.45) -> list:
        """找相近的旧查询，返回当时的结果（新的优先）。

        Args:
            query: 现在的查询
            limit: 最多返回几次查询的结果
            threshold: 相似度门槛（0 太松，1 只认完全相同）

        Returns:
            list[dict]: 旧结果，带 from_query 标记
        """
        scored = []
        for entry in self._entries():
            if self._clock() - entry.get("ts", 0) > _TTL_S:
                continue
            similarity = _score(query, str(entry.get("query", "")))
            if similarity >= threshold:
                scored.append((similarity, entry.get("ts", 0), entry))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        found = []
        for _, _, entry in scored[:limit]:
            for hit in entry.get("hits", []):
                found.append({**hit, "from_query": entry.get("query", "")})
        return found

    def note(self, query: str, hits: list[dict]) -> None:
        """记下这次查询与结果（坏的写不进去只当没写）。"""
        if not hits:
            return
        entry = {"query": query, "ts": self._clock(), "hits": hits}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return
        self._trim()

    def _entries(self) -> list[dict]:
        """读全部记录（坏行跳过）。"""
        if not self.path.exists():
            return []
        found = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and isinstance(item.get("hits"), list):
                    found.append(item)
        except OSError:
            return []
        return found

    def _trim(self) -> None:
        """条目太多时按时间留最近的（一次性重写）。"""
        entries = self._entries()
        if len(entries) <= self._max:
            return
        kept = sorted(entries, key=lambda item: item.get("ts", 0))[-self._max :]
        try:
            payload = "".join(
                json.dumps(item, ensure_ascii=False) + "\n" for item in kept
            )
            temp = self.path.with_suffix(".jsonl.tmp")
            temp.write_text(payload, encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            return

    @property
    def size(self) -> int:
        """库里有多少条查询。"""
        return len(self._entries())
