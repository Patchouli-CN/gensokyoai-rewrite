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


# ---------- 写入时语义自动关联 ----------


class _FakeEmbedder:
    """按内容映射返回预设向量的假向量化器（未映射内容给默认向量）"""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._map = vectors
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._map.get(t, [0.0, 1.0]) for t in texts]


async def test_semantic_association_links_similar():
    """语义相近的记忆写入时自动建双向边；不相近的不建"""
    vectors = {
        "灵梦: 离冬至还有几天啊？": [1.0, 0.0],
        "幽幽子: 冬至倒计时我来算算": [0.95, 0.05],
        "魔理沙: 今天午饭吃什么好呢": [0.0, 1.0],
    }
    mgr = MemoryManager(embedder=_FakeEmbedder(vectors))
    a = MemoryItem(content="灵梦: 离冬至还有几天啊？")
    b = MemoryItem(content="幽幽子: 冬至倒计时我来算算")
    c = MemoryItem(content="魔理沙: 今天午饭吃什么好呢")
    await mgr.store(a)
    await mgr.store(b)
    await mgr.store(c)

    assert b.memory_id in a.relate_ids, "B 应连到 A"
    assert a.memory_id in b.relate_ids, "A 应连到 B（双向）"
    assert not c.relate_ids, "不相干的 C 不应建边"
    assert c.memory_id not in a.relate_ids and c.memory_id not in b.relate_ids


async def test_association_skips_short_content():
    """内容太短的碎片（「哈哈」类）不上图，也不花 embedding 调用"""
    embedder = _FakeEmbedder({})
    mgr = MemoryManager(embedder=embedder)
    await mgr.store(MemoryItem(content="哈哈"))
    assert not embedder.calls, "短内容不应触发向量化"


async def test_association_without_embedder_is_noop():
    """未配置 embedder：不建边、不报错（行为同旧版）"""
    mgr = MemoryManager()
    a = MemoryItem(content="这是一条足够长的记忆内容")
    await mgr.store(a)
    assert not a.relate_ids


async def test_association_embedder_failure_degrades():
    """embedding 服务挂了：写入照常，跳过关联（不影响主链路）"""

    class _BoomEmbedder:
        async def embed(self, texts):
            raise ConnectionError("embedding server down")

    mgr = MemoryManager(embedder=_BoomEmbedder())
    item = MemoryItem(content="这是一条足够长的记忆内容")
    await mgr.store(item)
    assert mgr.work_mem_size == 1
    assert not item.relate_ids


async def test_restore_then_rebuild_vectors_and_associate():
    """restore 恢复的旧记忆没有向量：下次写入先批量重建，再正常关联"""
    old = MemoryItem(content="灵梦: 上次说的冬至倒计时")
    mgr = MemoryManager(
        embedder=_FakeEmbedder(
            {
                "灵梦: 上次说的冬至倒计时": [1.0, 0.0],
                "灵梦: 冬至还有几天来着": [0.95, 0.05],
            }
        )
    )
    mgr.restore([old])
    assert not mgr._work_vectors, "restore 后不立即建向量"

    new = MemoryItem(content="灵梦: 冬至还有几天来着")
    await mgr.store(new)
    embedder_calls = mgr._embedder.calls
    assert embedder_calls == [["灵梦: 上次说的冬至倒计时", "灵梦: 冬至还有几天来着"]], (
        "应一次批量覆盖旧记忆与新记忆，不重复 embed"
    )
    assert new.memory_id in old.relate_ids and old.memory_id in new.relate_ids


async def test_forget_cleans_vectors():
    """forget 删除记忆时同步清理向量（不留孤儿进候选集）"""
    mgr = MemoryManager(embedder=_FakeEmbedder({}))
    item = MemoryItem(content="这是一条足够长的记忆内容")
    await mgr.store(item)
    assert item.memory_id in mgr._work_vectors
    mgr.forget([item.memory_id])
    assert item.memory_id not in mgr._work_vectors


async def test_eviction_cleans_vectors():
    """淘汰时同步清理向量"""
    mgr = MemoryManager(max_capacity=2, embedder=_FakeEmbedder({}))
    items = [MemoryItem(content=f"足够长的记忆内容{i}号") for i in range(3)]
    for item in items:
        await mgr.store(item)
    assert mgr.work_mem_size == 2
    evicted = items[0]  # 同分最早者被淘汰
    assert evicted.memory_id not in mgr._work_vectors
