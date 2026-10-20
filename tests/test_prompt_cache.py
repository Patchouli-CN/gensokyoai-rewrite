"""前缀缓存管理器测试（prompts/cache.py）

覆盖：稳定段登记与漂移检测 / 排版纪律 / 尾部增删 / 撤回（回退）语义 /
渲染顺序 / 指纹 / 命中率遥测 / 只读协议。
"""

import pytest

from gensokyoai.prompts import CacheStats, PromptCache
from gensokyoai.schemas.model_schema import Message, Usage


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


# ---------- 稳定段与渲染顺序 ----------


def test_render_static_before_tail():
    """渲染序：稳定段（登记序）在前，尾部（追加序）垫底"""
    pc = PromptCache(owner="t")
    pc.static_msg("system", "人设", name="persona")
    pc.static_msg("system", "工具", name="tools")
    pc.volatile_msg("user", "每轮编译")
    pc.append(_msg("assistant", "增量"))
    assert [(m.role, m.content) for m in pc.render()] == [
        ("system", "人设"),
        ("system", "工具"),
        ("user", "每轮编译"),
        ("assistant", "增量"),
    ]


def test_static_msg_same_content_is_noop():
    """同名同内容重复登记：no-op，不计漂移"""
    pc = PromptCache(owner="t")
    pc.static_msg("system", "人设", name="persona")
    pc.static_msg("system", "人设", name="persona")
    assert len(pc.render()) == 1
    assert pc.stats().drifts == {}


def test_static_msg_drift_detected_and_counted():
    """同名不同内容：漂移计数 + 内容更新"""
    pc = PromptCache(owner="t")
    pc.static_msg("system", "人设v1", name="persona")
    pc.static_msg("system", "人设v2", name="persona")
    assert pc.render()[0].content == "人设v2"
    assert pc.stats().drifts == {"persona": 1}
    pc.static_msg("system", "人设v3", name="persona")
    assert pc.stats().drifts == {"persona": 2}


def test_static_msg_after_tail_raises():
    """排版纪律：尾部已有内容后再登记稳定段 = 调用方错误，早暴露"""
    pc = PromptCache(owner="t")
    pc.volatile_msg("user", "x")
    with pytest.raises(ValueError, match="版面纪律"):
        pc.static_msg("system", "迟到的人设", name="persona")


def test_reset_tail_keeps_static():
    """每轮重编译：reset_tail 清尾部、留稳定段"""
    pc = PromptCache(owner="t")
    pc.static_msg("system", "人设", name="persona")
    pc.volatile_msg("user", "第一轮")
    pc.reset_tail()
    pc.volatile_msg("user", "第二轮")
    assert [(m.role, m.content) for m in pc.render()] == [
        ("system", "人设"),
        ("user", "第二轮"),
    ]


# ---------- 撤回（消息回退）语义 ----------


def test_retract_rolls_back_tail_only():
    """撤回最后一问一答：弹尾部两条，截尾点之前逐字节不变"""
    pc = PromptCache(owner="t")
    pc.static_msg("system", "人设", name="persona")
    history = [
        _msg("user", "你吃饭了吗？"),
        _msg("assistant", "吃了"),
        _msg("user", "吃的什么"),
        _msg("assistant", "牛肉"),
    ]
    pc.extend(history)
    before = pc.render()

    assert pc.retract(2) == 2
    pc.volatile_msg("user", "吃的什么（重问）")
    after = pc.render()

    # 截尾点之前（含稳定段与第一问一答）逐字节一致 -> 前缀缓存继续命中
    assert [(m.role, m.content) for m in after[:3]] == [(m.role, m.content) for m in before[:3]]
    assert after[-1].content == "吃的什么（重问）"


def test_retract_clamps_to_tail_and_never_eats_static():
    """撤回得比记得的多：夹到尾部边界（稳定段不可被撤回），返回实际条数"""
    pc = PromptCache(owner="t")
    pc.static_msg("system", "人设", name="persona")
    pc.extend([_msg("user", "a"), _msg("assistant", "b")])
    assert pc.retract(10) == 2
    assert [(m.role, m.content) for m in pc.render()] == [("system", "人设")]
    assert pc.retract(0) == 0


# ---------- 指纹与遥测 ----------


def test_fingerprint_tracks_segments():
    """指纹含段名与短 hash，随漂移变化"""
    pc = PromptCache(owner="t")
    assert pc.fingerprint() == "(no-static)"
    pc.static_msg("system", "人设v1", name="persona")
    fp1 = pc.fingerprint()
    assert fp1.startswith("persona:")
    pc.static_msg("system", "人设v2", name="persona")
    assert pc.fingerprint() != fp1


def test_record_usage_and_hit_rate():
    """命中率 = cached / prompt；无调用为 0"""
    pc = PromptCache(owner="t")
    assert pc.stats().hit_rate == 0.0
    pc.record_usage(Usage(prompt_tokens=1000, completion_tokens=50, cached_tokens=800))
    pc.record_usage(Usage(prompt_tokens=1000, completion_tokens=50, cached_tokens=600))
    stats = pc.stats()
    assert stats.calls == 2
    assert stats.hit_rate == pytest.approx(0.7)


def test_log_every_does_not_raise():
    """周期性汇总日志开关（只验证不炸）"""
    pc = PromptCache(owner="t", log_every=2)
    for _ in range(3):
        pc.record_usage(Usage(prompt_tokens=100, cached_tokens=50))
    assert pc.stats().calls == 3


def test_stats_is_snapshot():
    """stats() 返回快照：改快照不影响内部账本"""
    pc = PromptCache(owner="t")
    pc.static_msg("system", "v1", name="s")
    pc.static_msg("system", "v2", name="s")
    stats = pc.stats()
    stats.drifts["s"] = 999
    assert pc.stats().drifts == {"s": 1}
    assert isinstance(stats, CacheStats)


# ---------- 只读协议 ----------


def test_len_and_iter():
    """__len__ 为渲染条数；__iter__ 按渲染序只读遍历"""
    pc = PromptCache(owner="t")
    pc.static_msg("system", "人设", name="persona")
    pc.extend([_msg("user", "a"), _msg("assistant", "b")])
    assert len(pc) == 3
    assert [m.content for m in pc] == ["人设", "a", "b"]
    assert "brain" in repr(pc) or "owner='t'" in repr(pc)
