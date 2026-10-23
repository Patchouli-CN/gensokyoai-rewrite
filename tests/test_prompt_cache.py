"""PromptCache：三区分离、对齐、撤回、压缩、报告、整份重建。"""

import pytest

from gensokyoai.llm.prompt_cache import PromptCache, estimate_tokens


def _talk(cache: PromptCache, count: int, text: str = "帕秋莉：借我书" * 4) -> None:
    """往尾巴塞 count 条消息。"""
    for _ in range(count):
        cache.add(text)


def test_fixed_block_rejects_conflict():
    """同名不同值直接拒绝"""
    cache = PromptCache()
    cache.stable_fixed("persona", "觉")

    with pytest.raises(ValueError):
        cache.stable_fixed("persona", "芙兰")


def test_fixed_block_same_value_is_noop():
    """同名同值重复设置无害"""
    cache = PromptCache()
    cache.stable_fixed("persona", "觉")
    cache.stable_fixed("persona", "觉")

    assert cache.fixed_names == ("persona",)


def test_build_is_plain_concatenation():
    """拼接不加任何预处理"""
    cache = PromptCache()
    cache.stable_fixed("a", "第一段")
    cache.add("第二段")

    assert cache.build() == "第一段\n第二段"


def test_stabilize_moves_messages_into_history():
    """稳定把消息推进历史区，系统块不动"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人" * 60)
    _talk(cache, 20)

    moved = cache.stabilize()

    assert moved > 0
    assert len(cache.history_blocks) == moved
    assert cache.system_tokens == 60
    assert cache.tail_tokens > 0


def test_stabilize_aligns_to_grid():
    """切点对齐到 64：已固定部分不超出格子"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人设" * 30)
    _talk(cache, 15)

    cache.stabilize()

    assert cache.waste_tokens < cache.align
    assert cache.aligned_tokens <= cache.prefix_tokens


def test_stabilize_keeps_tail_when_not_enough():
    """不够一格就不硬塞"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人" * 64)
    cache.add("尾")

    assert cache.stabilize() == 0
    assert cache.tail_tokens == 1


def test_rollback_removes_tail():
    """撤回把尾巴整段丢掉"""
    cache = PromptCache()
    cache.add("a")
    cache.add("b")
    cache.add("c")

    cache.rollback(1)

    assert cache.build() == "a"


def test_rollback_cannot_touch_history():
    """撤不到已稳定的历史（它在另一个区，尾巴下标够不着）"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人" * 60)
    _talk(cache, 20)
    moved = cache.stabilize()
    assert moved > 0

    with pytest.raises(IndexError):
        cache.rollback(len(cache.history_blocks))
    with pytest.raises(IndexError):
        cache.rollback(999)


def test_compact_only_touches_history():
    """压缩只动历史，系统块逐字节不变"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人" * 60)
    cache.stable_fixed("tools", "工具清单" * 20)
    _talk(cache, 30)
    cache.stabilize()
    system_before = cache.system_tokens
    history_before = cache.history_tokens

    dropped = cache.compact("（往事概要）今天借了三本书", keep=2)

    assert dropped > 0
    assert cache.system_tokens == system_before, "系统块不许动"
    assert cache.history_tokens < history_before, "历史该变小"
    assert cache.history_blocks[0].startswith("（往事概要）")


def test_compact_bumps_epoch_and_resets_baseline():
    """压缩进入新一代，报告基线清空"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人" * 60)
    _talk(cache, 30)
    cache.stabilize()

    cache.compact("概要", keep=1)

    assert cache.epoch == 2
    assert cache.report() is None, "新基线还没建立，不该报裂痕"


def test_compact_without_history_is_noop():
    """没历史可压时什么都不做"""
    cache = PromptCache()
    cache.stable_fixed("persona", "人" * 10)

    assert cache.compact("概要") == 0
    assert cache.epoch == 1


def test_compact_drop_all_when_keep_zero():
    """keep=0 时旧历史全丢，只留摘要"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人" * 60)
    _talk(cache, 30)
    cache.stabilize()

    cache.compact("完全概要")

    assert len(cache.history_blocks) == 1


def test_report_is_none_when_prefix_intact():
    """前缀只增不改时报告为空"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人" * 60)
    _talk(cache, 20)
    cache.stabilize()
    cache.add("新消息")

    assert cache.report() is None


def test_report_catches_broken_prefix():
    """固定块被绕过 API 改掉时，报告指出位置"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人" * 60)
    _talk(cache, 20)
    cache.stabilize()

    cache._fixed[0] = ("persona", "换成别人了" * 20)
    report = cache.report()

    assert report is not None


def test_stats_splits_three_regions():
    """统计能分别看到系统/历史/尾巴"""
    cache = PromptCache(name="ds模型缓存", align=64)
    cache.stable_fixed("persona", "人" * 100)

    stats = cache.stats()
    assert stats["name"] == "ds模型缓存"
    assert stats["align"] == 64
    assert stats["system_tokens"] == 100
    assert stats["history_tokens"] == 0
    assert stats["epoch"] == 1


def test_estimate_tokens_mixed_text():
    """中英混排的粗估"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abcd") == 1


def test_clear_rebuilds_everything():
    """系统块变了要能整份重建（人设热更新）"""
    cache = PromptCache(align=64, name="qq")
    cache.stable_fixed("persona", "旧人设" * 10)
    cache.add("一条消息")

    cache.clear()

    assert cache.fixed_names == ()
    assert cache.build() == ""
    assert cache.epoch == 2


def test_transcript_excludes_system_blocks():
    """transcript 只给消息区（拼在 user 前面），不含系统块"""
    cache = PromptCache(align=64)
    cache.stable_fixed("persona", "人设正文")
    cache.add("甲：你好")
    cache.add("觉：你也好")

    text = cache.transcript()

    assert "甲：你好" in text
    assert "人设正文" not in text


def test_transcript_empty_when_no_messages():
    """没消息时 transcript 是空串"""
    cache = PromptCache()
    cache.stable_fixed("persona", "人设")

    assert cache.transcript() == ""
