"""DeliveryService 世界级测试（组件拆分前的临时落户）。

blocking 闸门对投递路径的选择（放弃流式 / 保持流式）要在世界级验证；
P7 拆出 DeliveryService 后，这两个用例迁为组件级。
"""

import types
from pathlib import Path

from gensokyoai.core.config import OOCJudgeSettings
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.schemas.brain_schema import BrainConclusion, BrainThinkEffort
from gensokyoai.schemas.model_schema import CompletionResult, StreamEvent
from gensokyoai.schemas.scene_schema import SceneSnapshot

_TMP_DIR = Path(__file__).resolve().parent / "temp" / "delivery"


class _StubBackend:
    """按脚本回话的假后端"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        self.calls.append({"messages": list(messages)})
        content = self.replies.pop(0) if self.replies else ""
        return CompletionResult(content=content, finish_reason="stop")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


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


class _FakeOOCJudge:
    """按脚本返回概率的假裁判"""

    def __init__(self, answers: dict[str, float]) -> None:
        self._answers = answers

    async def ask(self, state, questions):
        return dict(self._answers)


def _make_world(backend: _StubBackend, **kwargs) -> TouhouWorld:
    sessions = SessionManager()
    sessions.set_default_backend(backend)
    character = Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人是也"))
    eye = types.SimpleNamespace(_stop_requested=True)
    kwargs.setdefault("storage_dir", _TMP_DIR)
    return TouhouWorld(eye=eye, character=character, sessions=sessions, **kwargs)


def _snapshot(content: str = "你好") -> SceneSnapshot:
    return SceneSnapshot(
        scene_type="private_chat",
        sender="灵梦",
        content=content,
        is_direct=True,
    )


async def test_blocking_gate_forces_buffered_express():
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
    world = _make_world(
        backend,
        mouth=mouth,
        ooc_judge=judge,
        ooc_judge_settings=OOCJudgeSettings(enabled=True, mode="blocking"),
    )

    conclusion = BrainConclusion(
        verdict="pass_through", intent="打招呼", emotion="愉悦", effort=BrainThinkEffort.LOW
    )
    reply = await world._express(_snapshot(), conclusion, [])
    assert reply == "啊啦～你好呀。"
    assert mouth.begun == 0, "blocking 模式不走流式"
    assert mouth.sent == ["啊啦～你好呀。"]


async def test_side_chain_mode_still_streams():
    """默认 side_chain 模式不改流式行为（口层支持就流式）"""

    class _StreamStubBackend(_StubBackend):
        """带 chat_stream 的假后端（流式路径用）"""

        def __init__(self, chunks: list[str]) -> None:
            super().__init__([])
            self.chunks = chunks

        async def chat_stream(self, messages, **kwargs):
            for chunk in self.chunks:
                yield StreamEvent(delta=chunk)

    backend = _StreamStubBackend(["啊啦～", "你好呀。"])
    mouth = _StubMouth(streaming=True)
    world = _make_world(backend, mouth=mouth)  # 默认 side_chain

    conclusion = BrainConclusion(
        verdict="pass_through", intent="打招呼", emotion="愉悦", effort=BrainThinkEffort.LOW
    )
    reply = await world._express(_snapshot(), conclusion, [])
    assert reply == "啊啦～你好呀。"
    assert mouth.begun == 1, "side_chain 模式保持流式"
