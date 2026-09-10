"""流式投递单元测试：SessionManager.call_stream / Responder.respond_stream"""

from gensokyoai.core.responder.generator import Responder
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.schemas.brain_schema import BrainConclusion, BrainThinkEffort
from gensokyoai.schemas.model_schema import CompletionResult, Message, StreamEvent, Usage
from gensokyoai.schemas.scene_schema import SceneSnapshot


class _StreamBackend:
    """带 chat_stream 的后端：按脚本逐块流出"""

    def __init__(self, deltas: list[str], finish: str = "stop") -> None:
        self.deltas = list(deltas)
        self.finish = finish
        self.chat_calls = 0

    async def chat(self, messages, **kw):
        self.chat_calls += 1
        return CompletionResult(
            content="".join(self.deltas),
            finish_reason=self.finish,
            usage=Usage(prompt_tokens=5, completion_tokens=4),
        )

    async def chat_stream(self, messages, **kw):
        for d in self.deltas:
            yield StreamEvent(delta=d)
        yield StreamEvent(
            delta="", finish_reason=self.finish, usage=Usage(prompt_tokens=5, completion_tokens=4)
        )

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


class _ChatOnlyBackend:
    """只有 chat、无 chat_stream：验证 call_stream 回退 chat() 单块"""

    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, messages, **kw):
        return CompletionResult(
            content=self.content,
            finish_reason="stop",
            usage=Usage(prompt_tokens=5, completion_tokens=3),
        )

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


async def test_call_stream_uses_chat_stream_deltas():
    """call_stream 走真流式：逐块 delta + 末块 finish/usage"""
    sm = SessionManager()
    sm.set_default_backend(_StreamBackend(["唔", "……", "你好呀"]))
    events = [ev async for ev in sm.call_stream("responder", [Message(role="user", content="hi")])]
    assert [ev.delta for ev in events[:-1]] == ["唔", "……", "你好呀"]
    assert events[-1].delta == ""
    assert events[-1].finish_reason == "stop"
    assert "".join(ev.delta for ev in events) == "唔……你好呀"


async def test_call_stream_falls_back_to_chat():
    """后端无 chat_stream 时回退 chat() 一次性产出单块（content 与 finish 同块）"""
    sm = SessionManager()
    sm.set_default_backend(_ChatOnlyBackend("整段回复"))
    events = [ev async for ev in sm.call_stream("responder", [Message(role="user", content="hi")])]
    assert len(events) == 1
    assert events[0].delta == "整段回复"
    assert events[0].finish_reason == "stop"
    assert events[0].usage.completion_tokens == 3


async def test_respond_stream_yields_chunks_and_emotion_suffix():
    """respond_stream 逐块产出；情绪「愤怒」在结尾补「！」"""
    sm = SessionManager()
    sm.set_default_backend(_StreamBackend(["不许", "提那个人"]))
    responder = Responder(sm, persona="【测试】人设")
    conclusion = BrainConclusion(intent="闲聊", emotion="愤怒", effort=BrainThinkEffort.LOW)
    snapshot = SceneSnapshot(sender="小明", content="讲个事", is_direct=True)
    deltas = [d async for d in responder.respond_stream(conclusion, snapshot, [])]
    joined = "".join(deltas)
    assert "不许" in joined and "提那个人" in joined
    assert joined.endswith("！"), "情绪「愤怒」应追加感叹号"


async def test_respond_stream_halfcut_continues():
    """截断时（length）发起流式续写并拼接"""

    class _TruncStreamBackend(_StreamBackend):
        def __init__(self):
            super().__init__(["前半截"], finish="length")

        async def chat_stream(self, messages, **kw):
            yield StreamEvent(delta="前半截")
            yield StreamEvent(delta="", finish_reason="length", usage=Usage(completion_tokens=3))
            # 续写调用（第二次 chat_stream）产出后半截
            yield StreamEvent(delta="后半截")
            yield StreamEvent(delta="", finish_reason="stop", usage=Usage(completion_tokens=3))

    sm = SessionManager()
    sm.set_default_backend(_TruncStreamBackend())
    responder = Responder(sm)
    conclusion = BrainConclusion(intent="闲聊", emotion="平淡", effort=BrainThinkEffort.LOW)
    snapshot = SceneSnapshot(sender="小明", content="讲故事", is_direct=True)
    deltas = [d async for d in responder.respond_stream(conclusion, snapshot, [])]
    joined = "".join(deltas).strip()
    assert "前半截" in joined and "后半截" in joined, "截断后应续写拼接"


class _IdleEye:
    """不产出任何输入、可停止的假感知器"""

    _stop_requested = False

    async def next_snapshot(self):
        return None

    async def close(self):
        pass


async def test_express_streams_proactive_line(tmp_path, capsys):
    """口层支持流式时，_express 走流式投递（主动发言与主循环同款路径）"""
    from gensokyoai.mouth.console import ConsoleMouth
    from gensokyoai.roleplay.character import Character, CharacterCard
    from gensokyoai.roleplay.loop import TouhouWorld

    sm = SessionManager()
    sm.set_default_backend(_StreamBackend(["唔……", "妖梦，茶点准备好了吗～"]))
    character = Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人"))
    world = TouhouWorld(
        eye=_IdleEye(),
        character=character,
        sessions=sm,
        mouth=ConsoleMouth(),
        session_id="t-express",
        storage_dir=tmp_path,
    )
    snapshot = SceneSnapshot(sender="环境", content="（安静了片刻）", is_direct=False)
    conclusion = BrainConclusion(intent="主动发起话题", emotion="平静", effort=BrainThinkEffort.LOW)

    reply = await world._express(snapshot, conclusion, [], ooc_guard=False)

    out = capsys.readouterr().out
    assert "幽幽子: " in out, "应打出角色名前缀"
    assert "妖梦" in out, "流式内容应逐块显示到控制台"
    assert "妖梦" in reply, "应返回完整回复文本"
