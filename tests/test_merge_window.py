"""输入合并窗口测试：merge_snapshots 合并语义 + QueuePerceiver 攒批行为"""

import asyncio

from gensokyoai.satori.queue import QueuePerceiver, merge_snapshots
from gensokyoai.schemas.scene_schema import SceneSnapshot


def _snap(sender: str, content: str, direct: bool = False, ts: float = 0.0) -> SceneSnapshot:
    return SceneSnapshot(
        scene_type="group_chat",
        sender=sender,
        content=content,
        is_direct=direct,
        timestamp=ts,
    )


# ---------- merge_snapshots 语义 ----------


def test_merge_single_passthrough():
    s = _snap("甲", "你好")
    assert merge_snapshots([s]) is s


def test_merge_same_sender_joins_lines():
    """同一发送者「话没说完」：内容按行拼接，sender 不变"""
    merged = merge_snapshots(
        [_snap("甲", "等下", ts=1.0), _snap("甲", "我草", ts=2.0), _snap("甲", "看到了吗", ts=3.0)]
    )
    assert merged.sender == "甲"
    assert merged.content == "等下\n我草\n看到了吗"
    assert merged.participants == ["甲"]
    assert len(merged.event_queue) == 3
    assert merged.timestamp == 3.0


def test_merge_multi_sender_prefixes_and_primary():
    """多人同时 @：每行自带归属前缀，sender 取最后被 direct 的人"""
    merged = merge_snapshots(
        [
            _snap("甲", "在吗", direct=True, ts=1.0),
            _snap("乙", "也在吗", direct=True, ts=2.0),
            _snap("丙", "围观", ts=3.0),
        ]
    )
    assert merged.content == "甲: 在吗\n乙: 也在吗\n丙: 围观"
    assert merged.sender == "乙", "回复主要回应最后被 direct 的人"
    assert merged.is_direct is True
    assert merged.participants == ["甲", "乙", "丙"]


def test_merge_direct_is_or():
    """任何一条针对角色，整批都算 direct"""
    merged = merge_snapshots([_snap("甲", "闲聊"), _snap("乙", "@纯狐 在吗", direct=True)])
    assert merged.is_direct is True
    merged2 = merge_snapshots([_snap("甲", "闲聊"), _snap("乙", "还是闲聊")])
    assert merged2.is_direct is False


# ---------- QueuePerceiver 攒批行为 ----------


async def test_push_merges_within_window():
    """窗内三条消息 → 世界只收到一条合并快照"""
    perceiver = QueuePerceiver(merge_window=0.05)
    perceiver.push(_snap("甲", "在吗", direct=True))
    perceiver.push(_snap("乙", "也在", direct=True))
    perceiver.push(_snap("甲", "说话呀"))
    merged = await asyncio.wait_for(perceiver.next_snapshot(), timeout=1.0)
    assert merged is not None
    assert merged.participants == ["甲", "乙"]
    assert merged.is_direct is True
    assert "乙: 也在" in merged.content
    assert perceiver.queue.empty(), "批次应合成一条，没有残留"
    perceiver.request_stop()


async def test_push_zero_window_passthrough():
    """窗口关闭（0）时逐条直接进队（旧行为）"""
    perceiver = QueuePerceiver()
    perceiver.push(_snap("甲", "一"))
    perceiver.push(_snap("甲", "二"))
    first = await perceiver.next_snapshot()
    assert first is not None and first.content == "一"
    second = await perceiver.next_snapshot()
    assert second is not None and second.content == "二"
    perceiver.request_stop()


async def test_batches_flush_separately():
    """两个窗口期的消息分成两批"""
    perceiver = QueuePerceiver(merge_window=0.05)
    perceiver.push(_snap("甲", "第一批一"))
    perceiver.push(_snap("甲", "第一批二"))
    first = await asyncio.wait_for(perceiver.next_snapshot(), timeout=1.0)
    assert first is not None and first.content == "第一批一\n第一批二"

    perceiver.push(_snap("甲", "第二批"))
    second = await asyncio.wait_for(perceiver.next_snapshot(), timeout=1.0)
    assert second is not None and second.content == "第二批"
    perceiver.request_stop()


async def test_stop_discards_pending_batch():
    """停止时丢弃未合并批次，不泄漏到下一轮"""
    perceiver = QueuePerceiver(merge_window=60.0)
    perceiver.push(_snap("甲", "被丢弃"))
    perceiver.request_stop()
    assert await perceiver.next_snapshot() is None
    assert perceiver.queue.empty()


# ---------- 忙时攒批：嵌套拍平 + drain ----------


def test_merge_flattens_nested_batches():
    """已合并过的批次快照再合并：按 event_queue 展开，不出现套娃前缀"""
    batch1 = merge_snapshots(
        [_snap("甲", "在吗", direct=True, ts=1.0), _snap("乙", "也在", ts=2.0)]
    )
    batch2 = merge_snapshots([_snap("丙", "围观", ts=3.0), _snap("甲", "人呢", ts=4.0)])
    merged = merge_snapshots([batch1, batch2])
    assert merged.content == "甲: 在吗\n乙: 也在\n丙: 围观\n甲: 人呢"
    assert merged.participants == ["甲", "乙", "丙"]
    assert merged.is_direct is True
    assert len(merged.event_queue) == 4


def test_merge_nested_same_sender_joins_lines():
    """嵌套批里只有一个人：行拼接不带前缀（「话没说完」跨批）"""
    batch1 = merge_snapshots([_snap("甲", "等下", ts=1.0), _snap("甲", "我草", ts=2.0)])
    merged = merge_snapshots([batch1, _snap("甲", "看到了吗", ts=3.0)])
    assert merged.sender == "甲"
    assert merged.content == "等下\n我草\n看到了吗"


async def test_drain_empty_by_default():
    perceiver = QueuePerceiver()
    assert perceiver.drain() == []
    perceiver.request_stop()


async def test_drain_returns_backlog_in_order():
    """回合间隙一次性排空：按到达序返回，队列清空"""
    perceiver = QueuePerceiver()
    perceiver.push(_snap("甲", "一", ts=1.0))
    perceiver.push(_snap("乙", "二", ts=2.0))
    perceiver.push(_snap("甲", "三", ts=3.0))
    backlog = perceiver.drain()
    assert [s.content for s in backlog] == ["一", "二", "三"]
    assert perceiver.queue.empty()
    # 排空后合并语义照常
    merged = merge_snapshots(backlog)
    assert merged.content == "甲: 一\n乙: 二\n甲: 三"
    perceiver.request_stop()


async def test_drain_flushes_pending_window_batch():
    """合并窗口未落盘的批次也能被 drain 冲刷出来，且窗口定时器被取消"""
    perceiver = QueuePerceiver(merge_window=60.0)  # 窗口远大于测试时长
    perceiver.push(_snap("甲", "窗口里一", ts=1.0))
    perceiver.push(_snap("乙", "窗口里二", ts=2.0))
    backlog = perceiver.drain()
    assert [s.content for s in backlog] == ["窗口里一", "窗口里二"]
    assert perceiver.queue.empty()
    # 定时器已取消：之后不会再来一条重复的合并快照
    assert perceiver._flush_handle is None
    perceiver.request_stop()
