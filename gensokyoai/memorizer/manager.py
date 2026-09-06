
from collections import deque
from ..utils.logger import LoggerManager
from ..schemas.memory_schema import MemoryItem

class MemoryManager:
    """ 
    记忆管理器 (Memorizer Core)
    负责：存储、检索、关联、淘汰。
    采用最早淘汰架构 (Earliest Eviction) + 重要性加权。
    """
    
    def __init__(self, max_capacity: int = 1000) -> None:
        # 核心存储：ID -> Item
        self._logger = LoggerManager.get_logger("MEMORY")
        self._store: dict[str, MemoryItem] = {}
        
        # 反向索引：用于级联查询 "谁关联了我"
        # key: memory_id, value: set of related_ids
        self._reverse_index: dict[str, set[str]] = {}
        
        # 容量限制与淘汰队列 (FIFO)
        self._max_capacity = max_capacity
        self._access_queue: deque[str] = deque()
        
    def store(self, item: MemoryItem) -> None:
        """存储一条记忆，并维护关联索引"""
        self._logger.info(f"存储记忆: {item.memory_id}: ({item.memory_type}) {item.content}")
        mid = item.memory_id
        
        # 1. 存入主表
        self._store[mid] = item
        self._access_queue.append(mid)
        
        # 2. 维护反向索引 (建立图谱边)
        for ref_id in item.relate_ids:
            if ref_id not in self._reverse_index:
                self._reverse_index[ref_id] = set()
            self._reverse_index[ref_id].add(mid)
            
        # 3. 检查容量，触发淘汰
        if len(self._store) > self._max_capacity:
            self._evict_earliest()

    def retrieve(self, memory_id: str) -> MemoryItem | None:
        """根据 ID 获取单条记忆"""
        return self._store.get(memory_id)

    def recent(self, n: int = 10) -> list[MemoryItem]:
        """按写入时间倒序取最近 n 条记忆（编排者的轻量记忆检索入口）

        Args:
            n: 返回条数上限
        """
        items = [self._store[mid] for mid in reversed(self._access_queue) if mid in self._store]
        return items[:n]

    def cascade_retrieve(
            self, 
            start_id: str, 
            depth: int = 2, 
            max_items: int = 10, 
            reverse: bool = False,
            min_importance: float = 0.0
    ) -> list[MemoryItem]:
        """
        级联检索：从某条记忆出发，顺藤摸瓜找到关联记忆。
        模拟大脑的“联想”过程。
        
        Args:
            start_id: 起始记忆 ID
            depth: 联想深度（跳数）
            max_items: 最大返回数量
            reverse: 是否开启反向检索（查找谁关联了当前记忆）
            min_importance: 最低重要性阈值，过滤掉无关紧要的碎片记忆
        """
        if start_id not in self._store:
            return []

        results: list[MemoryItem] = []
        visited: set[str] = {start_id}
        # 队列元素: (memory_id, current_depth)
        queue: deque[tuple[str, int]] = deque([(start_id, 0)])
        
        while queue and len(results) < max_items * 2: # 多取一些用于后续排序筛选
            current_id, current_depth = queue.popleft()
            
            # 1. 获取当前节点
            item = self._store.get(current_id)
            if not item:
                continue
                
            # 2. 收集结果（跳过起始节点本身，除非你需要它）
            if current_id != start_id and item.importance >= min_importance:
                results.append(item)
                
            # 3. 扩展下一层
            if current_depth < depth:
                next_ids = set()
                
                # 正向关联：我提到了谁/什么
                next_ids.update(item.relate_ids)
                
                # 反向关联：谁提到了我（如果开启）
                if reverse:
                    next_ids.update(self._reverse_index.get(current_id, set()))
                
                for next_id in next_ids:
                    if next_id not in visited and next_id in self._store:
                        visited.add(next_id)
                        queue.append((next_id, current_depth + 1))

        # 4. 最终排序：先按重要性降序，再按时间降序（最近的优先）
        # 这样即使 BFS 找到的顺序是乱的，给 Brain 的也是最精华的
        results.sort(key=lambda x: (x.importance, x.timestamp), reverse=True)
        
        return results[:max_items]

    def _evict_earliest(self) -> None:
        """
        最早淘汰逻辑：
        弹出队列最前端的 ID，如果它没有被高重要性保护，则删除。
        """
        while self._access_queue:
            oldest_id = self._access_queue.popleft()
            
            # 如果该记忆已不存在（可能被手动删了），跳过
            if oldest_id not in self._store:
                continue
                
            item = self._store[oldest_id]
            
            # 简单的重要性保护：重要性 > 0.9 的记忆不轻易淘汰
            if item.importance < 0.9:
                self._remove_from_store(oldest_id)
                break # 淘汰一个就停止

    def _remove_from_store(self, memory_id: str) -> None:
        """物理删除记忆并清理索引"""
        if memory_id in self._store:
            item = self._store.pop(memory_id)
            
            # 清理反向索引
            for ref_id in item.relate_ids:
                if ref_id in self._reverse_index:
                    self._reverse_index[ref_id].discard(memory_id)
                    
            # 清理自己作为被关联方的索引
            if memory_id in self._reverse_index:
                del self._reverse_index[memory_id]

    @property
    def size(self) -> int:
        return len(self._store)