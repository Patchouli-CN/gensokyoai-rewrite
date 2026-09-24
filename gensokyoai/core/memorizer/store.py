"""记忆的存储和检索 - 长期记忆（JSON 文件持久化）"""

from pathlib import Path

import ayafileio
import msgspec

from ...schemas.memory_schema import MemoryItem
from ...utils.logger import LoggerManager
from .embedder import Embedder, cosine_rank


class LongMemoryStore:
    """长期记忆存储：基于 JSON 文件的持久化层（冷记忆归档）。

    `file_path=None` 表示**纯内存**：冷记忆只在进程内保留，完全不落盘。
    默认值也是 None —— 这样误用 `LongMemoryStore()` 不会悄悄在 CWD 下写出
    `long_memory.json`（此前默认值是相对路径 `"long_memory.json"`，正是仓库根
    多出该文件的元凶）。

    向量（embedding）单独存 sidecar `vectors.msgpack`（与 JSON 同目录）：
    768 维 float 写成 JSON 文本每条好几 KB，混在正文里文件会迅速发胖。
    未配 embedder 时检索退回子串匹配，行为与旧版一致。
    """

    def __init__(
        self,
        file_path: str | Path | None = None,
        embedder: Embedder | None = None,
        min_score: float = 0.0,
    ):
        """初始化。

        Args:
            file_path: 落盘文件路径；None 表示纯内存（不落盘）
            embedder: 向量化器；None 时 retrieve_by_topic 退回子串匹配
            min_score: 语义检索相似度下限，低于下限的结果丢弃（0 = 不过滤）
        """
        self._logger = LoggerManager.get_logger("LONGMEM")
        self._file_path = Path(file_path) if file_path is not None else None
        self._embedder = embedder
        self._min_score = min_score
        self._items: dict[str, MemoryItem] = {}
        self._vectors: dict[str, list[float]] = {}
        self._dirty = False
        self._vectors_dirty = False
        if self._file_path is not None:
            self._load_from_disk()

    @property
    def path(self) -> Path | None:
        """落盘路径；纯内存模式为 None。"""
        return self._file_path

    @property
    def _vectors_path(self) -> Path | None:
        """向量 sidecar 路径（与 JSON 同目录）；纯内存模式为 None。"""
        if self._file_path is None:
            return None
        return self._file_path.with_name("vectors.msgpack")

    def _load_from_disk(self) -> None:
        """初始化时从磁盘加载全部记忆（纯内存模式无操作）"""
        if self._file_path is None:
            self._logger.debug("长期记忆为纯内存模式，跳过磁盘加载")
            return
        if not self._file_path.exists():
            self._logger.info("长期记忆文件不存在，初始化空存储")
            return
        try:
            raw = self._file_path.read_text(encoding="utf-8")
            # 使用 msgspec 严格校验并恢复
            self._items = {
                m.memory_id: m
                for m in msgspec.json.decode(raw.encode("utf-8"), type=list[MemoryItem])
            }
            self._logger.info(f"从磁盘加载长期记忆: {len(self._items)} 条")
        except Exception:
            self._logger.exception("长期记忆文件损坏，重置为空存储")
        self._load_vectors_from_disk()

    def _load_vectors_from_disk(self) -> None:
        """加载向量 sidecar；缺失/损坏都不算故障（检索会退回子串匹配）"""
        path = self._vectors_path
        if path is None or not path.exists():
            return
        try:
            self._vectors = msgspec.msgpack.decode(path.read_bytes(), type=dict[str, list[float]])
            self._logger.info(f"从磁盘加载记忆向量: {len(self._vectors)} 条")
        except Exception:
            self._logger.exception("向量 sidecar 损坏，忽略（下次归档时重建）")
            self._vectors = {}

    async def dump(self, items: list[MemoryItem]) -> None:
        """异步批量写入（归档冷记忆），写入后标记为不脏"""
        if not items:
            return
        for item in items:
            self._items[item.memory_id] = item
        self._dirty = True
        await self._embed_new(items)

        if self._file_path is None:
            # 纯内存：仅留在进程内
            self._dirty = False
            self._vectors_dirty = False
            self._logger.info(f"长期记忆归档(纯内存): {len(items)} 条")
            return

        # 真异步落盘（ayafileio：IOCP/io_uring/GCD 内核级完成，不占线程池）
        await self._async_to_disk()
        await self._async_vectors_to_disk()
        self._logger.info(f"长期记忆归档: {len(items)} 条 -> {self._file_path.name}")

    async def _embed_new(self, items: list[MemoryItem]) -> None:
        """给还没有向量的新条目补算 embedding；失败不阻塞归档（检索退回子串）"""
        if self._embedder is None:
            return
        pending = [i for i in items if i.memory_id not in self._vectors and i.content]
        if not pending:
            return
        try:
            vectors = await self._embedder.embed([i.content for i in pending])
        except Exception:
            self._logger.exception("记忆向量化失败（本次跳过，不影响归档）")
            return
        for item, vector in zip(pending, vectors, strict=True):
            self._vectors[item.memory_id] = vector
        self._vectors_dirty = True

    async def _async_to_disk(self) -> None:
        """真异步将缓存写入磁盘（ayafileio；原子写入：先写临时文件，再替换）"""
        if self._file_path is None or not self._dirty:
            return
        data = list(self._items.values())
        # msgspec 提供快速的 JSON 序列化（字节直写，无需过一层文本编解码）
        content = msgspec.json.encode(data)

        temp_file = self._file_path.with_suffix(".tmp")
        try:
            # 父目录必须自己建：此前缺这一步，写 <dir>/<session_id>/ 时目录不存在，
            # FileNotFoundError 被下面的 except 静默吞掉 —— 表现为「长期记忆归档
            # 看似成功但文件始终不出现」
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            async with ayafileio.open(temp_file, "wb") as handle:
                await handle.write(content)
            temp_file.replace(self._file_path)  # 原子替换，防止写一半崩溃
            self._dirty = False
        except Exception:
            self._logger.exception(f"长期记忆写入失败: {self._file_path}")

    async def _async_vectors_to_disk(self) -> None:
        """向量 sidecar 落盘（msgpack 二进制 + 原子替换，与正文同一套口径）"""
        path = self._vectors_path
        if path is None or not self._vectors_dirty:
            return
        content = msgspec.msgpack.encode(self._vectors)
        temp_file = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            async with ayafileio.open(temp_file, "wb") as handle:
                await handle.write(content)
            temp_file.replace(path)
            self._vectors_dirty = False
        except Exception:
            self._logger.exception(f"记忆向量写入失败: {path}")

    async def retrieve_by_topic(self, topic: str, limit: int = 10) -> list[MemoryItem]:
        """按主题检索历史记忆（模拟冷记忆按需唤醒）。

        配了 embedder 且有向量时走语义检索（余弦相似度排序）；
        否则退回子串匹配（按 重要性+热度+时间 排序）。
        """
        if self._embedder is not None and self._vectors:
            try:
                [query] = await self._embedder.embed([topic])
            except Exception:
                self._logger.exception("查询向量化失败，本次退回子串匹配")
            else:
                ranked = cosine_rank(query, self._vectors, limit=limit)
                hits = [
                    self._items[key]
                    for key, score in ranked
                    if key in self._items and score >= self._min_score
                ]
                if not hits and ranked:
                    self._logger.debug(f"语义检索无一过线（min_score={self._min_score}），返回空")
                return hits

        matches = [
            item for item in self._items.values() if item.topic == topic or topic in item.content
        ]
        # 按重要性 + 访问热度 + 时间排序
        matches.sort(key=lambda x: (x.importance, x.access_count, x.last_accessed_at), reverse=True)
        return matches[:limit]

    def size(self) -> int:
        """当前长期记忆总量"""
        return len(self._items)
