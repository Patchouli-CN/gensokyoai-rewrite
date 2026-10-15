"""MemoryManager 单元测试：存储 / 最近检索 / 蒸馏选取与遗忘"""

import asyncio

import ayafileio

from gensokyoai.core.memorizer.manager import MemoryManager
from gensokyoai.core.memorizer.store import LongMemoryStore
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


async def test_recent_reserves_slots_for_long_term_hits(tmp_path):
    """工作记忆已满 n 条时，长期命中仍占预留名额进结果（此前被二次截断全部丢掉）"""
    mgr = MemoryManager(storage_dir=tmp_path, session_id="recall")
    await mgr._long_mem_store.dump([MemoryItem(topic="往事", content="去年冬至一起吃了饺子")])
    for i in range(10):
        await mgr.store(MemoryItem(content=f"闲聊{i}"))

    recalled = await mgr.recent(5, search_term="冬至")
    contents = [m.content for m in recalled]
    assert len(recalled) == 5
    assert "去年冬至一起吃了饺子" in contents, f"长期命中应进结果: {contents}"
    assert contents[0] == "闲聊9", "近况仍按新到旧排在前面"


async def test_long_store_dump_during_write_is_not_lost(tmp_path, monkeypatch):
    """A 正在写盘时 B 加入新条目、且 B 的 embedding 比 A 的写盘慢：B 的条目也要落盘

    （布尔脏标下 A 写完清脏，B 醒来看到「不脏」直接返回，B 的条目只在内存里）
    """
    real_open = ayafileio.open

    class _SlowWrite:
        def __init__(self, *args, **kwargs) -> None:
            self._ctx = real_open(*args, **kwargs)

        async def __aenter__(self):
            self._handle = await self._ctx.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._ctx.__aexit__(*exc)

        async def write(self, data):
            await asyncio.sleep(0.02)
            return await self._handle.write(data)

        async def flush(self):
            return await self._handle.flush()

    class _Embedder:
        async def embed(self, texts):
            if "后到" in texts[0]:
                await asyncio.sleep(0.06)
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(ayafileio, "open", _SlowWrite)
    path = tmp_path / "long_memory.json"
    store = LongMemoryStore(path, embedder=_Embedder())

    first = asyncio.create_task(store.dump([MemoryItem(content="先到的那条")]))
    await asyncio.sleep(0.005)  # A 已在写盘
    await asyncio.gather(first, store.dump([MemoryItem(content="后到的那条")]))

    reloaded = LongMemoryStore(path)
    assert {m.content for m in reloaded._items.values()} == {"先到的那条", "后到的那条"}


async def test_long_store_quarantines_corrupt_file(tmp_path):
    """长期记忆文件损坏：先挪走留证，不能被下一次 dump 静默覆盖"""
    path = tmp_path / "long_memory.json"
    path.write_text("{这不是合法 JSON", encoding="utf-8")

    store = LongMemoryStore(path)
    await store.dump([MemoryItem(content="新记忆")])

    quarantined = list(tmp_path.glob("long_memory.json.corrupt-*"))
    assert len(quarantined) == 1, "坏文件应被隔离留证"
    assert quarantined[0].read_text(encoding="utf-8") == "{这不是合法 JSON"


async def test_important_memory_embedded_once():
    """高重要性记忆：关联时算过的向量直接交给长期记忆，不再为同一段文本 embed 第二次"""
    embedder = _FakeEmbedder({})
    mgr = MemoryManager(embedder=embedder)
    await mgr.store(MemoryItem(content="灵梦答应冬至一起吃饺子", importance=0.8))
    embedded = [t for call in embedder.calls for t in call]
    assert embedded.count("灵梦答应冬至一起吃饺子") == 1, embedder.calls


async def test_restore_rebuild_is_windowed_and_single_flight():
    """restore 后的重建只覆盖关联窗口，且并发写入只重建一次"""
    embedder = _FakeEmbedder({})
    mgr = MemoryManager(embedder=embedder)
    mgr.restore([MemoryItem(content=f"恢复出来的旧记忆第{i}条") for i in range(300)])
    await asyncio.gather(
        mgr.store(MemoryItem(content="新写入的第一条记忆内容")),
        mgr.store(MemoryItem(content="新写入的第二条记忆内容")),
    )
    rebuild_batches = [call for call in embedder.calls if len(call) > 1]
    assert len(rebuild_batches) == 1, "并发写入只应重建一次"
    assert len(rebuild_batches[0]) <= 50, "只重建关联候选窗口"


async def test_recent_does_not_inflate_access_count():
    """读近况不结算热度（一回合读 3~4 次，淘汰分不该被「展示次数」主导）"""
    mgr = MemoryManager()
    item = MemoryItem(content="一条普通的记忆")
    await mgr.store(item)
    for _ in range(10):
        await mgr.recent(5)
    assert item.access_count == 0


async def test_eviction_removes_id_from_queue():
    """淘汰时队列同步清理：满容量后持续写入，队列长度不随写入增长"""
    mgr = MemoryManager(max_capacity=5)
    for i in range(50):
        await mgr.store(MemoryItem(content=f"第{i}句"))
    assert len(mgr._work_mem_queue) == mgr.work_mem_size == 5
