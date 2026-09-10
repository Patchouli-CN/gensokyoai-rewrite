"""MemoryManager 单元测试：存储 / 最近检索 / 蒸馏选取与遗忘"""

from gensokyoai.core.memorizer.manager import MemoryManager
from gensokyoai.schemas.memory_schema import MemoryItem


async def test_store_and_recent():
    """存储后按时间倒序取最近"""
    mgr = MemoryManager()
    for i in range(5):
        await mgr.store(MemoryItem(topic="对话", content=f"第{i}句"))
    recent = await mgr.recent(3)
    assert [m.content for m in recent] == ["第4句", "第3句", "第2句"]


async def test_oldest_returns_fifo_and_skips_core():
    """oldest 按写入顺序返回，且跳过核心保护项（importance>=0.9）"""
    mgr = MemoryManager()
    await mgr.store(MemoryItem(content="普通1"))
    await mgr.store(MemoryItem(content="核心设定", importance=0.95))
    await mgr.store(MemoryItem(content="普通2"))
    oldest = mgr.oldest(8)
    assert [m.content for m in oldest] == ["普通1", "普通2"]


async def test_forget_removes_items():
    """forget 删除指定记忆并清理队列"""
    mgr = MemoryManager()
    items = []
    for i in range(4):
        item = MemoryItem(content=f"记忆{i}")
        await mgr.store(item)
        items.append(item)

    removed = mgr.forget([items[0].memory_id, items[1].memory_id, "不存在的"])
    assert removed == 2
    assert mgr.work_mem_size == 2
    recent = await mgr.recent(10)
    assert {m.content for m in recent} == {"记忆2", "记忆3"}


async def test_distill_flow():
    """蒸馏全流程：取最早 -> 存摘要 -> 遗忘原文"""
    mgr = MemoryManager()
    for i in range(6):
        await mgr.store(MemoryItem(content=f"旧记忆{i}"))
    await mgr.store(MemoryItem(content="新记忆", importance=0.3))

    old = mgr.oldest(4)
    assert len(old) == 4
    summary = MemoryItem(
        topic="对话摘要", content="前4条的概要", memory_type="fact", importance=0.7
    )
    await mgr.store(summary)
    mgr.forget([m.memory_id for m in old])

    assert mgr.work_mem_size == 4, "应为 2 条剩余旧记忆 + 1 条新记忆 + 1 条摘要"
    recent = await mgr.recent(1)
    assert recent[0].content == "前4条的概要"
