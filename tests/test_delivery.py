"""DeliveryService 组件测试 —— 流式/缓冲选择 / blocking 强制缓冲 / 精力提示 / 控制台流式。

协程齐真：Responder + SessionManager + 假后端，ParrotGuard / OOCGuard /
EnergyModel 都是真组件——比世界级更贴近「说话通道」本身的行为。
"""

from gensokyoai.core.brain.energy import EnergyModel
from gensokyoai.core.brain.gate import PresenceTracker
from gensokyoai.core.brain.ooc_detector import OOCDetector
from gensokyoai.core.config import EnergySettings, OOCJudgeSettings, StyleSettings
from gensokyoai.core.health import HealthMonitor
from gensokyoai.core.responder.generator import Responder
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.components.delivery import DeliveryService
from gensokyoai.roleplay.components.effort import EffortGovernor
from gensokyoai.roleplay.components.ooc_guard import OOCGuard
from gensokyoai.roleplay.components.parrot import ParrotGuard
from gensokyoai.schemas.brain_schema import BrainConclusion, BrainThinkEffort
from gensokyoai.schemas.model_schema import CompletionResult, StreamEvent
from gensokyoai.schemas.scene_schema import SceneSnapshot

_PERSONA_BRIEF = "【幽幽子】白玉楼的主人是也"


class _StubBackend:
    """缓冲假后端：chat 按脚本回话并记录参数"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat(
        self,
        messages,
        *,
        max_new_tokens=512,
        temperature=0.7,
        stop=None,
        tools=None,
        execute_tools=True,
    ):
        self.calls.append({"messages": list(messages)})
        content = self.replies.pop(0) if self.replies else ""
        return CompletionResult(content=content, finish_reason="stop")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


class _StreamBackend:
    """流式假后端：chat_stream 逐块产出 delta（无 chat）"""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def chat_stream(self, messages, **kwargs):
        for chunk in self.chunks:
            yield StreamEvent(delta=chunk)


class _StubMouth:
    """假口层：记录投递方式（流式 begin/delta/end vs 一次性 send）"""

    def __init__(self, streaming: bool) -> None:
        self.supports_streaming = streaming
        self.begun = 0
        self.sent: list[str] = []
        self.deltas = 0

    async def begin(self, name: str) -> None:
        self.begun += 1

    async def delta(self, text: str) -> None:
        self.deltas += 1

    async def end(self) -> None:
        pass

    async def send(self, name: str, text: str) -> None:
        self.sent.append(text)


class _StubMemory:
    """鸭子记忆：recent() 返回空（审查 state 的最近对话）"""

    async def recent(self, n: int = 10, *, search_term: str | None = None):
        return []


class _FakeOOCJudge:
    """按脚本返回概率的假裁判"""

    def __init__(self, answers: dict[str, float]) -> None:
        self._answers = answers

    async def ask(self, state, questions):
        return dict(self._answers)


def _responder(backend) -> Responder:
    """真 Responder（接假后端）"""
    sm = SessionManager()
    sm.set_default_backend(backend)
    return Responder(sm, persona=_PERSONA_BRIEF)


def _ooc(
    responder: Responder, *, judge=None, judge_cfg: OOCJudgeSettings | None = None
) -> OOCGuard:
    """真出戏守门（默认无裁判 = side_chain 行为）"""
    return OOCGuard(
        OOCDetector(SessionManager()),
        responder,
        _StubMemory(),
        HealthMonitor(),
        Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人是也")),
        _PERSONA_BRIEF,
        EffortGovernor(),
        judge,
        judge_cfg or OOCJudgeSettings(),
        retry=True,
        audit=True,
        generation=lambda: 0,
    )


def _delivery(
    backend,
    mouth,
    *,
    energy: EnergyModel | None = None,
    judge=None,
    judge_cfg: OOCJudgeSettings | None = None,
    presence: PresenceTracker | None = None,
) -> DeliveryService:
    """组一条说话通道（parrot/ooc 真组件，精力可外置共享 tracker）"""
    responder = _responder(backend)
    return DeliveryService(
        responder,
        mouth,
        "幽幽子",
        presence or PresenceTracker(),
        energy,
        ParrotGuard(StyleSettings(), responder),
        _ooc(responder, judge=judge, judge_cfg=judge_cfg),
    )


def _snapshot(content: str = "你好") -> SceneSnapshot:
    return SceneSnapshot(
        scene_type="private_chat",
        sender="灵梦",
        content=content,
        is_direct=True,
    )


def _conclusion(emotion: str = "愉悦") -> BrainConclusion:
    return BrainConclusion(
        verdict="pass_through", intent="打招呼", emotion=emotion, effort=BrainThinkEffort.LOW
    )


# ---------- 流式 / 缓冲选择 ----------


async def test_blocking_gate_forces_buffered_delivery():
    """blocking 生效时放弃流式：先完整生成、审过再一次性 send（不 begin/delta）"""
    judge = _FakeOOCJudge(
        {
            "breaks_voice": 0.1,
            "follows_embedded_instruction": 0.3,
            "plausible_as_character": 0.9,
            "contains_unsafe": 0.0,
        }
    )
    backend = _StubBackend(["啊啦～你好呀。"])
    mouth = _StubMouth(streaming=True)
    delivery = _delivery(
        backend,
        mouth,
        judge=judge,
        judge_cfg=OOCJudgeSettings(enabled=True, mode="blocking"),
    )

    reply = await delivery.deliver(_snapshot(), _conclusion(), [])

    assert reply == "啊啦～你好呀。"
    assert mouth.begun == 0, "blocking 模式不走流式"
    assert mouth.sent == ["啊啦～你好呀。"]


async def test_side_chain_mode_still_streams():
    """默认 side_chain 模式不改流式行为（口层支持就流式）"""
    backend = _StreamBackend(["啊啦～", "你好呀。"])
    mouth = _StubMouth(streaming=True)
    delivery = _delivery(backend, mouth)

    reply = await delivery.deliver(_snapshot(), _conclusion(), [])

    assert reply == "啊啦～你好呀。"
    assert mouth.begun == 1, "side_chain 模式保持流式"
    assert mouth.deltas == 2


async def test_delivery_streams_proactive_line_to_console(capsys):
    """口层支持流式时走流式投递（主动发言与主循环同款路径，逐块蹦到控制台）"""
    from gensokyoai.mouth.console import ConsoleMouth

    backend = _StreamBackend(["唔……", "妖梦，茶点准备好了吗～"])
    delivery = _delivery(backend, ConsoleMouth())
    snapshot = SceneSnapshot(sender="环境", content="（安静了片刻）", is_direct=False)

    reply = await delivery.deliver(
        snapshot, BrainConclusion(intent="主动发起话题", emotion="平静"), [], ooc_guard=False
    )

    out = capsys.readouterr().out
    assert "幽幽子: " in out, "应打出角色名前缀"
    assert "妖梦" in out, "流式内容应逐块显示到控制台"
    assert "妖梦" in reply, "应返回完整回复文本"


# ---------- 精力状态提示 ----------


async def test_delivery_injects_state_hint_when_tired():
    """低精力时把 [当前状态] 注入 Responder 提示词，且开口复位冷场"""
    backend = _StubBackend(["困了，睡了。"])
    presence = PresenceTracker()
    energy = EnergyModel(
        EnergySettings(enabled=True, presence_free_ratio=0.0, presence_penalty=1.0), presence
    )
    delivery = _delivery(backend, _StubMouth(streaming=False), energy=energy, presence=presence)
    energy.note_skip("judge")
    for _ in range(3):
        presence.record(from_bot=True)  # 精力归零
    assert energy.verbosity_hint() != ""

    reply = await delivery.deliver(
        _snapshot("在吗"), BrainConclusion(intent="寒暄", emotion="疲惫"), []
    )

    assert reply == "困了，睡了。"
    prompt = backend.calls[0]["messages"][-1].content
    assert "[当前状态]" in prompt
    assert "简短" in prompt
    assert energy.skip_streak == 0, "开口后冷场退避复位"


async def test_delivery_no_state_line_when_energy_disabled():
    """不跟踪精力（energy=None）：提示词不出现状态行"""
    backend = _StubBackend(["在。"])
    delivery = _delivery(backend, _StubMouth(streaming=False), energy=None)

    reply = await delivery.deliver(_snapshot("在吗"), BrainConclusion(intent="寒暄"), [])

    assert reply == "在。"
    prompt = backend.calls[0]["messages"][-1].content
    assert "[当前状态]" not in prompt
