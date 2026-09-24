"""Embedder 与长期记忆语义检索单元测试：余弦排序 / 语义检索 / 降级 / sidecar 持久化"""

import pytest

from gensokyoai.core.memorizer.embedder import cosine_rank
from gensokyoai.core.memorizer.store import LongMemoryStore
from gensokyoai.schemas.memory_schema import MemoryItem


class FakeEmbedder:
    """查表式假向量化器：查不到键给零向量，并记录每次调用的入参"""

    def __init__(self, table: dict[str, list[float]] | None = None):
        self.table = table or {}
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.table.get(t, [0.0, 0.0]) for t in texts]


class RaisingEmbedder:
    """永远失败的向量化器（测降级路径）"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise ConnectionError("embedding 服务不可用")


def test_cosine_rank_orders_by_similarity():
    """余弦排序：近者在前"""
    ranked = cosine_rank(
        [1.0, 0.0],
        {"正交": [0.0, 1.0], "同向": [2.0, 0.0], "夹角": [0.7, 0.7]},
    )
    assert [key for key, _ in ranked] == ["同向", "夹角", "正交"]
    assert ranked[0][1] == pytest.approx(1.0)


def test_cosine_rank_edge_cases():
    """空候选 / 零查询向量 / 零候选向量不炸"""
    assert cosine_rank([1.0], {}) == []
    assert cosine_rank([0.0, 0.0], {"a": [1.0, 0.0]}) == []
    ranked = cosine_rank([1.0, 0.0], {"零向量": [0.0, 0.0], "同向": [1.0, 0.0]})
    assert ranked[0][0] == "同向"
    assert ranked[1] == ("零向量", 0.0)


async def test_semantic_retrieve_ranks_by_vector():
    """语义检索：子串不匹配也能按向量相似度捞到"""
    embedder = FakeEmbedder(
        {
            "寺庙香火很旺": [1.0, 0.0],
            "晚饭吃了咖喱": [0.0, 1.0],
            "参拜神社": [0.9, 0.1],
        }
    )
    store = LongMemoryStore(embedder=embedder)
    await store.dump([MemoryItem(content="寺庙香火很旺"), MemoryItem(content="晚饭吃了咖喱")])

    results = await store.retrieve_by_topic("参拜神社")
    # 子串匹配一条都捞不到（无共同字串），语义检索把向量近的排最前
    assert results[0].content == "寺庙香火很旺"


async def test_retrieve_falls_back_to_substring_without_embedder():
    """无 embedder：退回子串匹配（旧行为）"""
    store = LongMemoryStore()
    await store.dump([MemoryItem(content="寺庙香火很旺"), MemoryItem(content="晚饭吃了咖喱")])
    results = await store.retrieve_by_topic("寺庙")
    assert [item.content for item in results] == ["寺庙香火很旺"]
    assert await store.retrieve_by_topic("神社") == []


async def test_embed_failure_does_not_break_dump():
    """归档时向量化失败：条目照常入库，检索退回子串"""
    store = LongMemoryStore(embedder=RaisingEmbedder())
    await store.dump([MemoryItem(content="寺庙香火很旺")])
    assert store.size() == 1
    results = await store.retrieve_by_topic("寺庙")
    assert [item.content for item in results] == ["寺庙香火很旺"]


async def test_query_embed_failure_falls_back_to_substring():
    """检索时查询向量化失败：本次退回子串匹配"""
    embedder = FakeEmbedder({"寺庙香火很旺": [1.0, 0.0]})
    store = LongMemoryStore(embedder=embedder)
    await store.dump([MemoryItem(content="寺庙香火很旺")])

    class _Raising(FakeEmbedder):
        async def embed(self, texts):
            raise ConnectionError("炸了")

    store._embedder = _Raising()
    results = await store.retrieve_by_topic("寺庙")
    assert [item.content for item in results] == ["寺庙香火很旺"]


async def test_vectors_persist_to_sidecar(tmp_path):
    """向量 sidecar：重启后无需重新 embed 即可语义检索"""
    path = tmp_path / "long_memory.json"
    embedder = FakeEmbedder(
        {"寺庙香火很旺": [1.0, 0.0], "晚饭吃了咖喱": [0.0, 1.0], "参拜神社": [0.9, 0.1]}
    )
    store = LongMemoryStore(path, embedder=embedder)
    await store.dump([MemoryItem(content="寺庙香火很旺"), MemoryItem(content="晚饭吃了咖喱")])
    assert (tmp_path / "vectors.msgpack").exists()
    assert embedder.calls == [["寺庙香火很旺", "晚饭吃了咖喱"]]

    # 模拟重启：新实例从磁盘恢复，正文向量不重新 embed（embed 只被查询调用一次）
    embedder2 = FakeEmbedder({"参拜神社": [0.9, 0.1]})
    store2 = LongMemoryStore(path, embedder=embedder2)
    assert store2.size() == 2
    results = await store2.retrieve_by_topic("参拜神社")
    assert results[0].content == "寺庙香火很旺"
    assert embedder2.calls == [["参拜神社"]]


async def test_min_score_filters_noise():
    """相似度下限：全是低分噪声时返回空，而不是硬塞 top-N"""
    embedder = FakeEmbedder(
        {
            "寺庙香火很旺": [1.0, 0.0],
            "晚饭吃了咖喱": [0.0, 1.0],
            # 与两个候选都近乎垂直/反向：最高分 ~0.1，模拟真机噪声带
            "毫不相关的查询": [-0.5, 0.05],
        }
    )
    store = LongMemoryStore(embedder=embedder, min_score=0.5)
    await store.dump([MemoryItem(content="寺庙香火很旺"), MemoryItem(content="晚饭吃了咖喱")])
    assert await store.retrieve_by_topic("毫不相关的查询") == []

    # 下限为 0 时不过滤（非负即收），保持旧行为
    store_open = LongMemoryStore(embedder=embedder, min_score=0.0)
    await store_open.dump([MemoryItem(content="寺庙香火很旺"), MemoryItem(content="晚饭吃了咖喱")])
    assert len(await store_open.retrieve_by_topic("毫不相关的查询")) == 1
