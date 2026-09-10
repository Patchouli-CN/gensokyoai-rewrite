"""记忆的存储和检索 - 长期记忆（JSON 文件持久化）"""

import asyncio
from pathlib import Path

import msgspec

from ...schemas.memory_schema import MemoryItem
from ...utils.logger import LoggerManager


class LongMemoryStore:
    """长期记忆存储：基于 JSON 文件的持久化层（冷记忆归档）"""

    def __init__(self, file_path: str | Path = "long_memory.json"):
        self._logger = LoggerManager.get_logger("LONG MEMORY")
        self._file_path = Path(file_path)
        self._items: dict[str, MemoryItem] = {}
        self._dirty = False
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """初始化时从磁盘加载全部记忆"""
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
        # 将 IO 操作扔到线程池，防止阻塞 asyncio 主循环
        await asyncio.to_thread(self._sync_to_disk)
        self._logger.info(f"长期记忆归档: {len(items)} 条 -> {self._file_path.name}")

    def _sync_to_disk(self) -> None:
        """同步将缓存写入磁盘（原子写入：先写临时文件，再替换）"""
        if not self._dirty:
            return
        data = list(self._items.values())
        # msgspec 提供快速的 JSON 序列化
        content = msgspec.json.encode(data).decode("utf-8")

        temp_file = self._file_path.with_suffix(".tmp")
        try:
            temp_file.write_text(content, encoding="utf-8")
            temp_file.replace(self._file_path)  # 原子替换，防止写一半崩溃
            self._dirty = False
        except Exception:
            self._logger.exception("长期记忆写入失败")

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
