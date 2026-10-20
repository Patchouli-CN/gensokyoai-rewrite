"""显著性分层过滤测试（丘脑门控，core/brain/salience.py）

覆盖：打分器 / 精力调制阈值 / importance 分级 / 合并折叠 / 记忆关联与淘汰 /
世界装配接线。复读与已答判定不在这里——钦定平台侧插件（nb2_plugin）。
"""

import asyncio
import types

from gensokyoai.core.brain.salience import (
    NOISE_SCORE,
    SalienceScorer,
    importance_of,
    score_event,
)
from gensokyoai.core.config import AttentionSettings, EnergySettings
from gensokyoai.core.memorizer.manager import MemoryManager
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.satori.queue import QueuePerceiver, merge_snapshots
from gensokyoai.schemas.memory_schema import MemoryItem
from gensokyoai.schemas.model_schema import CompletionResult
from gensokyoai.schemas.scene_schema import SceneSnapshot


def _snap(sender: str, content: str, direct: bool = False, ts: float = 0.0) -> SceneSnapshot:
    return SceneSnapshot(
        scene_type="group_chat",
        sender=sender,
        content=content,
        is_direct=direct,
        timestamp=ts,
    )


def _scorer(**overrides) -> SalienceScorer:
    return SalienceScorer(AttentionSettings(enabled=True, **overrides))


# ---------- score_event 打分 ----------


def test_score_direct_is_one():
    """直连（被 @ / 被回复）恒 1.0，任何内容"""
    assert score_event("哈哈", is_direct=True) == 1.0


def test_score_bare_reaction_is_noise():
    """收尾语 / 纯反应恒为噪音分（低于默认折叠阈值）"""
    for text in ("哈哈", "草", "好的", "666", "嗯嗯", "谢谢"):
        assert score_event(text) == NOISE_SCORE, text
        assert score_event(text) < 0.3


def test_score_normal_message_stays_verbatim():
    """普通对话高于默认折叠阈值（不折叠）"""
    assert score_event("今天去哪玩") >= 0.3


def test_score_longer_and_question_score_higher():
    """长文与问句显著性更高"""
    short = score_event("今天天气不错")
    long = score_event("今天天气不错" * 30)
    question = score_event("离冬至还有几天啊？")
    assert long > short
    assert question > short


# ---------- SalienceScorer 阈值与精力调制 ----------


def test_fold_threshold_unbound_energy_is_base():
    """未绑定精力回调：阈值不调制"""
    scorer = _scorer(fold_threshold=0.3)
    assert scorer.fold_threshold() == 0.3


def test_fold_threshold_rises_when_tired():
    """精力归零：阈值上浮 energy_span（只抬不压）"""
    scorer = _scorer(fold_threshold=0.3, energy_span=0.2)
    scorer.bind_energy(lambda: 0.0)
    assert scorer.fold_threshold() == 0.5
    scorer.bind_energy(lambda: 1.0)
    assert scorer.fold_threshold() == 0.3


def test_fold_threshold_energy_modulate_off():
    """联动关闭：绑了精力也不调制"""
    scorer = _scorer(fold_threshold=0.3, energy_modulate=False)
    scorer.bind_energy(lambda: 0.0)
    assert scorer.fold_threshold() == 0.3


# ---------- importance 分级 ----------


def test_importance_tiers():
    """直连 0.6（写入即归档长期）；低分 0.2（淘汰即物理删除）；其余 0.5（旧默认）"""
    assert importance_of(1.0, is_direct=True, fold_threshold=0.3) == 0.6
    assert importance_of(0.05, is_direct=False, fold_threshold=0.3) == 0.2
    assert importance_of(0.4, is_direct=False, fold_threshold=0.3) == 0.5


def test_scorer_importance_uses_modulated_threshold():
    """精力低时更多消息被判为低分记忆"""
    scorer = _scorer(fold_threshold=0.3, energy_span=0.2)
    scorer.bind_energy(lambda: 0.0)  # 阈值抬到 0.5：普通对话（0.4）也低分
    assert scorer.importance("今天去哪玩") == 0.2
    assert scorer.importance("哈哈") == 0.2
    assert scorer.importance("在吗", is_direct=True) == 0.6


# ---------- 合并折叠 ----------


def test_merge_folds_bare_reaction_runs():
    """30 条各异附和折叠成计数标记；末尾 keep_verbatim 条保留全文"""
    snaps = [_snap(f"群友{i}", "哈哈", ts=float(i)) for i in range(30)]
    merged = merge_snapshots(snaps, scorer=_scorer(keep_verbatim=5))
    assert "（25 条附和）" in merged.content
    assert merged.content.count("哈哈") == 5, "末尾 5 条保留全文"
    assert len(merged.event_queue) == 30, "留痕不受影响"


def test_merge_fold_never_folds_direct():
    """直连事件永不折叠，且打断附和 run"""
    snaps = [
        _snap("甲", "哈哈", ts=1.0),
        _snap("乙", "草", ts=2.0),
        _snap("丙", "@幽幽子 在吗", direct=True, ts=3.0),
        _snap("丁", "好的", ts=4.0),
        _snap("戊", "今天去哪玩", ts=5.0),
    ]
    merged = merge_snapshots(snaps, scorer=_scorer(keep_verbatim=1))
    assert "@幽幽子 在吗" in merged.content
    assert "（2 条附和）" in merged.content
    assert "今天去哪玩" in merged.content, "末尾 keep_verbatim 保留"
    assert merged.sender == "丙", "sender 仍取最后被 direct 的人"


def test_merge_without_scorer_unchanged():
    """scorer=None：逐字节旧行为（无折叠标记）"""
    snaps = [_snap("甲", "哈哈", ts=1.0), _snap("乙", "草", ts=2.0)]
    merged = merge_snapshots(snaps)
    assert merged.content == "甲: 哈哈\n乙: 草"


def test_perceiver_flush_uses_scorer():
    """QueuePerceiver 攒批冲刷走同一个 scorer"""

    async def _run():
        perceiver = QueuePerceiver(merge_window=0.01, scorer=_scorer(keep_verbatim=1))
        perceiver.push(_snap("甲", "哈哈", ts=1.0))
        perceiver.push(_snap("乙", "草", ts=2.0))
        perceiver.push(_snap("丙", "今天去哪玩", ts=3.0))
        await asyncio.sleep(0.05)
        return await perceiver.next_snapshot()

    merged = asyncio.run(_run())
    assert merged is not None
    assert "（2 条附和）" in merged.content
    assert "今天去哪玩" in merged.content


# ---------- 记忆：关联门槛与淘汰 ----------


class _SpyEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.0, 1.0] for _ in texts]


async def test_association_skips_low_importance():
    """低 importance 条目不 embed、不上关联图"""
    embedder = _SpyEmbedder()
    mgr = MemoryManager(embedder=embedder, assoc_min_importance=0.3)
    await mgr.store(MemoryItem(content="甲: 哈哈哈哈哈哈哈", importance=0.2))
    assert not embedder.calls, "低分碎片不应触发向量化"
    await mgr.store(MemoryItem(content="乙: 离冬至还有几天啊？", importance=0.5))
    assert len(embedder.calls) == 1, "正常消息照常关联"


async def test_association_threshold_zero_keeps_old_behavior():
    """assoc_min_importance=0（默认）：importance 不过滤，行为同旧版"""
    embedder = _SpyEmbedder()
    mgr = MemoryManager(embedder=embedder, assoc_min_importance=0.0)
    await mgr.store(MemoryItem(content="这是一条足够长的低分记忆", importance=0.2))
    assert embedder.calls, "默认门槛不过滤"


async def test_low_importance_evicted_physically():
    """0.2 分记忆淘汰时物理删除（fire and forget），不转长期记忆"""
    mgr = MemoryManager(max_capacity=2, storage_dir=None, session_id=None)
    noise = MemoryItem(content="甲: 哈哈哈哈哈哈哈", importance=0.2)
    await mgr.store(noise)
    await mgr.store(MemoryItem(content="乙: 正常对话内容一", importance=0.5))
    await mgr.store(MemoryItem(content="丙: 正常对话内容二", importance=0.5))
    assert noise.memory_id not in mgr._work_mem_store, "0.2 分（淘汰分最低）应被淘汰"
    assert mgr.long_mem_size == 0, "低分碎片不进长期记忆"


# ---------- 世界装配接线 ----------


class _StubBackend:
    """最小后端（世界构造不触发调用）"""

    async def chat(self, *args, **kwargs):  # pragma: no cover - 不会被调到
        return CompletionResult(content="")


def _make_world(eye, **kwargs) -> TouhouWorld:
    sessions = SessionManager()
    sessions.set_default_backend(_StubBackend())
    character = Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人是也"))
    return TouhouWorld(eye=eye, character=character, sessions=sessions, **kwargs)


def test_world_disabled_attention_keeps_old_behavior():
    """attention 未开启：scorer 为 None，importance 恒 0.5，感知器不被注入"""
    eye = QueuePerceiver()
    world = _make_world(eye)
    assert world._salience is None
    assert eye.scorer is None
    assert world._user_importance(_snap("甲", "哈哈")) == 0.5
    assert world._user_importance(_snap("甲", "在吗", direct=True)) == 0.5


def test_world_enabled_attention_wires_scorer():
    """attention 开启：scorer 注入 QueuePerceiver；importance 分级生效"""
    eye = QueuePerceiver()
    world = _make_world(eye, attention=AttentionSettings(enabled=True))
    assert world._salience is not None
    assert eye.scorer is world._salience
    assert world._user_importance(_snap("甲", "哈哈")) == 0.2
    assert world._user_importance(_snap("甲", "在吗", direct=True)) == 0.6
    assert world._user_importance(_snap("甲", "今天去哪玩")) == 0.5


def test_world_binds_energy_when_available():
    """精力模型存在且联动开启：scorer 绑定精力回调；无精力模型则不绑"""
    eye = QueuePerceiver()
    world = _make_world(
        eye,
        attention=AttentionSettings(enabled=True),
        energy=EnergySettings(enabled=True),
    )
    assert world._salience is not None
    assert world._salience._energy is not None

    eye2 = QueuePerceiver()
    world2 = _make_world(eye2, attention=AttentionSettings(enabled=True))
    assert world2._salience is not None
    assert world2._salience._energy is None, "精力模型未启用时不绑定"


def test_world_non_queue_eye_not_injected():
    """非 QueuePerceiver 的眼睛（如控制台鸭子）：不注入也不炸"""
    eye = types.SimpleNamespace(stopping=True, drain=lambda: [])
    world = _make_world(eye, attention=AttentionSettings(enabled=True))
    assert world._salience is not None, "记忆分级仍然生效"
    assert world._user_importance(_snap("甲", "哈哈")) == 0.2
