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


async def test_pure_memory_mode_never_touches_disk(tmp_path, monkeypatch):
    """纯内存模式（不传 storage_dir/session_id）绝不落盘

    回归：此前它把长期记忆写到 **CWD 下的 `long_memory.json`** ——
    与「None 表示不落盘」的说明相悖，也是测试跑完仓库根多出该文件的元凶
    （`LongMemoryStore` 构造时还会读回旧文件，于是污染跨次累积）。
    """
    monkeypatch.chdir(tmp_path)
    mgr = MemoryManager()
    assert mgr._long_mem_store.path is None, "纯内存模式不应有落盘路径"

    # importance >= 0.6 会触发长期归档 —— 正是会写盘的那条路径
    await mgr.store(MemoryItem(content="核心设定", importance=0.95))

    assert mgr.long_mem_size == 1, "内存归档仍应生效"
    assert not (tmp_path / "long_memory.json").exists(), "纯内存模式不应创建文件"


async def test_file_backed_mode_writes_under_storage_dir(tmp_path, monkeypatch):
    """给了 storage_dir 时才落盘，且落在 <dir>/<session_id>/ 下（不是 CWD）"""
    monkeypatch.chdir(tmp_path)
    mgr = MemoryManager(storage_dir=tmp_path, session_id="s1")

    assert mgr._long_mem_store.path == tmp_path / "s1" / "long_memory.json"
    await mgr.store(MemoryItem(content="核心设定", importance=0.95))

    assert (tmp_path / "s1" / "long_memory.json").exists()
    assert not (tmp_path / "long_memory.json").exists(), "不应落到 CWD"
