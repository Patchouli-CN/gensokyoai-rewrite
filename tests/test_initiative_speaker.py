"""InitiativeSpeaker 组件测试 —— 节拍门控 / 对话欲评估 / 开口投递 / 记忆回写。

协程齐真：MemoryManager（真落盘到 tmp）+ EventBus + DeliveryService；
只假后端与口层——开口这条链路组件级可复现。
"""

from gensokyoai.core.brain.gate import PresenceTracker
from gensokyoai.core.brain.ooc_detector import OOCDetector
from gensokyoai.core.config import StyleSettings
from gensokyoai.core.event_bus import EventBus
from gensokyoai.core.health import HealthMonitor
from gensokyoai.core.memorizer.manager import MemoryManager
from gensokyoai.core.responder.generator import Responder
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.components.delivery import DeliveryService
from gensokyoai.roleplay.components.effort import EffortGovernor
from gensokyoai.roleplay.components.initiative_speaker import InitiativeSpeaker
from gensokyoai.roleplay.components.ooc_guard import OOCGuard
from gensokyoai.roleplay.components.parrot import ParrotGuard
from gensokyoai.roleplay.runtime import RuntimeState
from gensokyoai.schemas.event_schema import EventTopic
from gensokyoai.schemas.model_schema import CompletionResult


class _StubBackend:
    """缓冲假后端：chat 按脚本回话"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        return CompletionResult(content=self.reply, finish_reason="stop")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


class _StubMouth:
    """假口层：记录投递"""

    def __init__(self) -> None:
        self.supports_streaming = False
        self.sent: list[str] = []

    async def send(self, speaker: str, text: str) -> None:
        self.sent.append(text)


class _StubMemory:
    """鸭子记忆：recent() 返回空（冷场开局）"""

    async def recent(self, n: int = 10, *, search_term: str | None = None):
        return []


def _speaker(
    tmp_path,
    *,
    urge_threshold: float = 0.0,
    idle_threshold: float = 100.0,
    idle_for=200.0,
    stopping=False,
):
    """组一个主动发言节拍器；返回 (speaker, mouth, memory, bus, runtime, character)"""
    backend = _StubBackend("……妾身还以为你忘了呢。")
    sm = SessionManager()
    sm.set_default_backend(backend)
    responder = Responder(sm, persona="【幽幽子】白玉楼的主人是也")
    character = Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人是也"))
    memory = MemoryManager(storage_dir=tmp_path, session_id="initiative")
    bus = EventBus()
    runtime = RuntimeState()
    runtime.last_activity -= idle_for  # 模拟已空闲 idle_for 秒
    parrot = ParrotGuard(StyleSettings(), responder)
    ooc = OOCGuard(
        OOCDetector(sm),
        responder,
        _StubMemory(),
        HealthMonitor(),
        character,
        "【幽幽子】白玉楼的主人是也",
        EffortGovernor(),
        None,
        generation=lambda: 0,
        judge_cfg=None,
    )
    delivery = DeliveryService(
        responder, _StubMouth(), "幽幽子", PresenceTracker(), None, parrot, ooc
    )
    speaker = InitiativeSpeaker(
        character,
        memory,
        bus,
        runtime,
        delivery,
        urge_threshold=urge_threshold,
        idle_threshold=idle_threshold,
        is_stopping=lambda: stopping,
    )
    return speaker, delivery._mouth, memory, bus, runtime, character


async def test_tick_speaks_when_idle_and_urge_reached(tmp_path):
    """空闲够久 + 对话欲达标 -> 开口、投递、记忆回写、活跃度刷新"""
    speaker, mouth, memory, bus, runtime, character = _speaker(tmp_path)

    captured = []

    async def _capture(event):
        captured.append(event)

    bus.subscribe(EventTopic.MEMORY_WRITE, _capture)

    await speaker.tick()

    assert mouth.sent, "冷场够久应主动开口"
    assert "妾身" in mouth.sent[0]
    assert captured, "开口内容应回写记忆总线"
    assert "幽幽子" in str(captured[0].payload.content)
    assert character.status.motivation >= 0.0
    assert runtime.idle_for() < 1.0, "开口后活跃度应刷新"


async def test_tick_skips_when_busy(tmp_path):
    """主链路忙着（生成中）-> 避让，不抢 responder 会话"""
    speaker, mouth, _, _, runtime, _ = _speaker(tmp_path)
    with runtime.begin_busy():
        await speaker.tick()
    assert mouth.sent == []


async def test_tick_skips_when_recently_active(tmp_path):
    """刚互动过（未达空闲门槛）-> 不评估"""
    speaker, mouth, _, _, _, _ = _speaker(tmp_path, idle_for=1.0)
    await speaker.tick()
    assert mouth.sent == []


async def test_tick_skips_when_stopping(tmp_path):
    """关闭流程中 -> 不开口"""
    speaker, mouth, _, _, _, _ = _speaker(tmp_path, stopping=True)
    await speaker.tick()
    assert mouth.sent == []


async def test_tick_skips_below_urge_threshold(tmp_path):
    """对话欲不够（阈值拉满）-> 不开口，但评估照跑（对话欲已回写 status）"""
    speaker, mouth, _, _, _, character = _speaker(tmp_path, urge_threshold=1.0)
    await speaker.tick()
    assert mouth.sent == []
    assert character.status.motivation < 1.0
