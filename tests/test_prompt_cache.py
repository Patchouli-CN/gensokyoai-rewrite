"""PromptCache 的测试：冻住、命中、漂移告警、失效、统计。"""

import pytest

from gensokyoai.llm.prompt_cache import (
    NullPromptCache,
    PromptCache,
    estimate_tokens,
)


def test_freezes_first_build():
    """第一次拼好就冻住，之后原样返回"""
    cache = PromptCache()
    calls: list[int] = []

    def build() -> str:
        calls.append(1)
        return f"人设 v{len(calls)}"

    first = cache.get("qq", build)
    second = cache.get("qq", build)

    assert first == second == "人设 v1\n"
    assert cache.stats["hits"] == 1
    assert cache.stats["misses"] == 1


def test_normalizes_whitespace():
    """换行与尾部空白的差异被抹平（对模型没意义，对缓存是致命的）"""
    cache = PromptCache()

    text = cache.get("k", lambda: "a\r\nb\r\n\r\n")

    assert text == "a\nb\n"


def test_detects_drift_without_changing_prefix():
    """有人想改前缀：只记漂移，冻住的那份不动（保住命中）"""
    cache = PromptCache()
    version = {"value": "v1"}

    cache.get("qq", lambda: version["value"])
    version["value"] = "v2"
    again = cache.get("qq", lambda: version["value"])

    assert again == "v1\n", "必须继续返回冻住的那份"
    assert cache.stats["drifts"] == 1
    assert cache.drifts("qq")


def test_drift_recorded_once_per_variant():
    """同一变体反复出现只记一次，别刷屏"""
    cache = PromptCache()
    state = {"v": "a"}
    cache.get("qq", lambda: state["v"])

    for _ in range(5):
        state["v"] = "b"
        cache.get("qq", lambda: state["v"])

    assert cache.stats["drifts"] == 1


def test_build_failure_during_check_is_ignored():
    """核对时拼前缀抛异常：不影响对话"""
    cache = PromptCache()
    cache.get("qq", lambda: "稳定")

    def boom() -> str:
        raise RuntimeError("炸了")

    assert cache.get("qq", boom) == "稳定\n"


def test_invalidate_allows_rebuild():
    """确实要改前缀时，invalidate 之后重建"""
    cache = PromptCache()
    cache.get("qq", lambda: "v1")
    cache.invalidate("qq")

    assert cache.get("qq", lambda: "v2") == "v2\n"


def test_eviction_when_too_many_keys():
    """冻的前缀有上限（房间多了不会无限涨）"""
    cache = PromptCache(max_entries=2)
    for name in ("a", "b", "c"):
        cache.get(name, lambda n=name: n)

    assert cache.stats["keys"] == 2


def test_null_cache_always_rebuilds():
    """NullPromptCache 不冻结：改配置立刻生效"""
    cache = NullPromptCache()
    assert cache.get("qq", lambda: "v1") == "v1\n"
    assert cache.get("qq", lambda: "v2") == "v2\n"


def test_estimate_tokens_covers_cache_threshold():
    """估算够不够 64 token 的缓存门槛"""
    short = estimate_tokens("你好")
    long = estimate_tokens("你好" * 200)

    assert short < 64
    assert long >= 400
    assert estimate_tokens("") == 0


def test_stats_reports_size():
    """统计里有 key 数、字符近似 token、命中次数"""
    cache = PromptCache()
    cache.get("qq", lambda: "人设正文" * 50)
    cache.get("qq", lambda: "人设正文" * 50)

    stats = cache.stats
    assert stats["keys"] == 1
    assert stats["hits"] == 1
    assert stats["tokens"] > 64


@pytest.mark.parametrize("key", ["qq", "console"])
def test_same_prefix_byte_identical(key):
    """同样的输入永远得到逐字节相同的输出（缓存的前提）"""
    cache = PromptCache()

    first = cache.get(key, lambda: "人设\n\n场景\n\n机制")
    second = cache.get(key, lambda: "人设\n\n场景\n\n机制")

    assert first.encode() == second.encode()
