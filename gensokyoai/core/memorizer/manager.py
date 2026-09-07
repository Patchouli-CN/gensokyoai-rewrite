"""记忆管理器 (Memorizer Core)
负责：存储、检索、关联、遗忘。
融合了工作记忆 (Work Mem) + 长期记忆 (Long Mem)，
并用遗忘曲线 (重要性 + 访问次数 + 时间衰减) 决定淘汰。
"""

import asyncio
import time
from collections import deque

from ...schemas.memory_schema import MemoryItem
from ...utils.logger import LoggerManager
from .store import LongMemoryStore

# 核心设定，不可遗忘
_CORE_IMPORTANCE_THRESHOLD = 0.9
# 如果重要程度超过这个值，直接进入长期记忆，不参与短期淘汰
_DUMP_TO_LONG_TERM_THRESHOLD = 0.6

class MemoryManager:
    """ 
    记忆管理器
    负责：存储、检索、关联、淘汰。
    采用最早淘汰架构 (Earliest Eviction) + 重要性加权 + 遗忘曲线。
    """
    
    def __init__(self, max_capacity: int = 1000) -> None:
        self._logger = LoggerManager.get_logger("MEMORY")
        
        # --- 工作记忆（短期）：当前活跃的对话 ---
        self._work_mem_store: dict[str, MemoryItem] = {}
        self._work_mem_queue: deque[str] = deque()
        self._work_max = max_capacity  # 工作记忆上限（超出淘汰）
        
        # --- 长期记忆（冷）：JSON 落盘 ---
        self._long_mem_store = LongMemoryStore()
        
    async def store(self, item: MemoryItem) -> None:
        """存储一条记忆，并维护关联索引"""
        self._logger.info(f"存储记忆: {item.memory_id}: ({item.memory_type}) {item.content}")
        mid = item.memory_id
        
        # 1. 存入工作记忆
        self._work_mem_store[mid] = item
        self._work_mem_queue.append(mid)
        
        # 2. 如果记忆至关重要，直接归档到长期记忆，保障不丢失
        if item.importance >= _DUMP_TO_LONG_TERM_THRESHOLD:
            await self._long_mem_store.dump([item])
        
        # 3. 检查容量，触发遗忘
        if len(self._work_mem_store) > self._work_max:
            self._evict_earliest()

    def retrieve(self, memory_id: str) -> MemoryItem | None:
        """根据 ID 获取单条记忆，并增加热度"""
        item = self._work_mem_store.get(memory_id)
        if item:
            item.touch()
        return item

    async def recent(self, n: int = 10, *, search_term: str | None = None) -> list[MemoryItem]:
        """按写入时间倒序取最近 n 条记忆。
        如果提供 search_term，也会去长期记忆中捞取相关旧事。

        Args:
            n: 返回条数上限
            search_term: 可选，关联检索词
        """
        # 1. 热记忆：直接取最近的
        items = [self._work_mem_store[mid] for mid in reversed(self._work_mem_queue) if mid in self._work_mem_store]
        items = items[:n]

        # 2. 冷记忆：如果有关联词，去长期记忆里补全
        if search_term:
            long_items = await self._long_mem_store.retrieve_by_topic(search_term, limit=n)
            # 合并去重
            ids = {i.memory_id for i in items}
            items.extend([i for i in long_items if i.memory_id not in ids])
            items = items[:n]  # 再次截断
        
        # 3. 结算访问热度
        for item in items:
            item.touch()
        
        return items

    async def cascade_retrieve(
            self, 
            start_id: str, 
            depth: int = 2, 
            max_items: int = 10, 
            reverse: bool = False,
            min_importance: float = 0.0
    ) -> list[MemoryItem]:
        """
        级联检索：从某条记忆出发，顺藤摸瓜找到关联记忆。
        模拟大脑的“联想”过程。同时融合工作记忆与长期记忆。
        
        Args:
            start_id: 起始记忆 ID
            depth: 联想深度（跳数）
            max_items: 最大返回数量
            reverse: 是否开启反向检索（查找谁关联了当前记忆）
            min_importance: 最低重要性阈值，过滤掉无关紧要的碎片记忆
        """
        if start_id not in self._work_mem_store:
            # 尝试去长期记忆找起点
            all_items = {**self._work_mem_store, **self._long_mem_store._items}
        else:
            all_items = self._work_mem_store

        if start_id not in all_items:
            return []

        results: list[MemoryItem] = []
        visited: set[str] = {start_id}
        queue: deque[tuple[str, int]] = deque([(start_id, 0)])
        
        while queue and len(results) < max_items * 2:
            current_id, current_depth = queue.popleft()
            
            item = all_items.get(current_id)
            if not item:
                continue
                
            if current_id != start_id and item.importance >= min_importance:
                results.append(item)
                item.touch()  # 提取被检索时增加热度
            
            if current_depth < depth:
                next_ids = set()
                next_ids.update(item.relate_ids)
                if reverse:
                    # 简单的反向扫描（不构建索引，直接遍历）
                    for other in all_items.values():
                        if current_id in other.relate_ids:
                            next_ids.add(other.memory_id)
                
                for next_id in next_ids:
                    if next_id not in visited and next_id in all_items:
                        visited.add(next_id)
                        queue.append((next_id, current_depth + 1))

        results.sort(key=lambda x: (x.importance, x.access_count, x.timestamp), reverse=True)
        
        return results[:max_items]

    def _evict_earliest(self) -> None:
        """
        智能遗忘：根据 重要性+访问次数-时间衰减 计算存活分值
        重要核心永不忘记，不重要的琐事直接物理删除 (fire and forget)。
        """
        if len(self._work_mem_store) <= self._work_max:
            return

        now = time.time()
        min_score = float('inf')
        min_id = None
        min_item = None

        for mid, item in self._work_mem_store.items():
            # 1. 核心记忆保护：重要性极高（如角色设定、重大剧情），不参与淘汰
            if item.importance >= _CORE_IMPORTANCE_THRESHOLD:
                continue

            # 2. 访问增益：被检索过 5 次，说明是高频话题，多给分，使其不易忘
            access_gain = item.access_count * 0.1

            # 3. 时间衰减：超过 72 小时没被碰过，扣分（指数衰减，模拟艾宾浩斯）
            hours_since_access = (now - item.last_accessed_at) / 3600.0
            # 24小时内不衰减，之后线性减少，72小时彻底衰减完
            time_decay = max(0.0, 1.0 - (hours_since_access / 72.0))

            # 综合存活分值 = 基础重要性 + 访问增益 + 时间衰减
            # 分数越低，越容易忘
            score = (item.importance * 1.0) + access_gain + time_decay

            if score < min_score:
                min_score = score
                min_id = mid
                min_item = item

        # 弹出去：如果是无关紧要的琐事（分值极低），直接物理删除
        # 如果有一定价值（中等分值），扔给长期记忆 JSON 落盘
        if min_id:
            self._work_mem_store.pop(min_id)
            # 从队列里清理（FIFO 里可能有残留，但不影响正确性，访问时若不存在直接跳过）
            if min_item: # 空检查
                if min_item.importance > 0.3:
                    # 有价值，转入长期记忆，异步落盘
                    asyncio.create_task(self._long_mem_store.dump([min_item]))
                    self._logger.debug(f"记忆转存为长期记忆: {min_item.content[:30]}")
                else:
                    self._logger.debug(f"记忆已彻底遗忘: {min_item.content[:30]} (fire and forget)")

    @property
    def work_mem_size(self) -> int:
        """ 工作记忆数量 """
        return len(self._work_mem_store)

    @property
    def long_mem_size(self) -> int:
        """ 长期记忆数量 """
        return self._long_mem_store.size()