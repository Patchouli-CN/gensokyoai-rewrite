"""记忆的存储和检索 - 长期记忆（JSON 文件持久化）"""

import asyncio
from pathlib import Path

import msgspec

from ...schemas.memory_schema import MemoryItem
from ...utils.logger import LoggerManager


class LongMemoryStore:
    """长期记忆存储：基于 JSON 文件的持久化层（冷记忆归档）。

    `file_path=None` 表示**纯内存**：冷记忆只在进程内保留，完全不落盘。
    默认值也是 None —— 这样误用 `LongMemoryStore()` 不会悄悄在 CWD 下写出
    `long_memory.json`（此前默认值是相对路径 `"long_memory.json"`，正是仓库根
    多出该文件的元凶）。
    """

    def __init__(self, file_path: str | Path | None = None):
        """初始化。

        Args:
            file_path: 落盘文件路径；None 表示纯内存（不落盘）
        """
        self._logger = LoggerManager.get_logger("LONG MEMORY")
        self._file_path = Path(file_path) if file_path is not None else None
        self._items: dict[str, MemoryItem] = {}
        self._dirty = False
        if self._file_path is not None:
            self._load_from_disk()

    @property
    def path(self) -> Path | None:
        """落盘路径；纯内存模式为 None。"""
        return self._file_path

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

    async def dump(self, items: list[MemoryItem]) -> None:
        """异步批量写入（归档冷记忆），写入后标记为不脏"""
        if not items:
            return
        for item in items:
            self._items[item.memory_id] = item
        self._dirty = True

        if self._file_path is None:
            # 纯内存：仅留在进程内
            self._dirty = False
            self._logger.info(f"长期记忆归档(纯内存): {len(items)} 条")
            return

        # 将 IO 操作扔到线程池，防止阻塞 asyncio 主循环
        await asyncio.to_thread(self._sync_to_disk)
        self._logger.info(f"长期记忆归档: {len(items)} 条 -> {self._file_path.name}")

    def _sync_to_disk(self) -> None:
        """同步将缓存写入磁盘（原子写入：先写临时文件，再替换）"""
        if self._file_path is None or not self._dirty:
            return
        data = list(self._items.values())
        # msgspec 提供快速的 JSON 序列化
        content = msgspec.json.encode(data).decode("utf-8")

        temp_file = self._file_path.with_suffix(".tmp")
        try:
            # 父目录必须自己建：此前缺这一步，写 <dir>/<session_id>/ 时目录不存在，
            # FileNotFoundError 被下面的 except 静默吞掉 —— 表现为「长期记忆归档
            # 看似成功但文件始终不出现」
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            temp_file.write_text(content, encoding="utf-8")
            temp_file.replace(self._file_path)  # 原子替换，防止写一半崩溃
            self._dirty = False
        except Exception:
            self._logger.exception(f"长期记忆写入失败: {self._file_path}")

    async def retrieve_by_topic(self, topic: str, limit: int = 10) -> list[MemoryItem]:
        """按主题检索历史记忆（模拟冷记忆按需唤醒）"""
        matches = [
            item for item in self._items.values() if item.topic == topic or topic in item.content
        ]
        # 按重要性 + 访问热度 + 时间排序
        matches.sort(key=lambda x: (x.importance, x.access_count, x.last_accessed_at), reverse=True)
        return matches[:limit]

    def size(self) -> int:
        """当前长期记忆总量"""
        return len(self._items)
