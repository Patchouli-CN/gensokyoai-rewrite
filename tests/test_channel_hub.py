"""频道中枢 / 队列感知器 / 广播口 单元测试：多路复用与广播"""

import asyncio

from gensokyoai.core.session_manager import SessionManager
from gensokyoai.eyes.queue import QueuePerceiver
from gensokyoai.mouth.broadcast import BroadcastMouth
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.hub import ChannelHub
from gensokyoai.schemas.model_schema import CompletionResult
from gensokyoai.schemas.scene_schema import SceneSnapshot


class _Clock:
    """可控时钟（秒）。"""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Sink:
    """记录收到的所有帧。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def deliver(self, frame: dict) -> None:
        self.frames.append(frame)


class _FakeWorld:
    """只与 eyes/mouth 打交道的假世界：收到一条就回一句。"""

    def __init__(self, perceiver: QueuePerceiver, mouth: BroadcastMouth) -> None:
        self._eye = perceiver
        self._mouth = mouth
        self.seen: list[SceneSnapshot] = []

    async def start(self) -> None:
        while True:
            snapshot = await self._eye.next_snapshot()
            if snapshot is None:
                return
            self.seen.append(snapshot)
            await self._mouth.send("幽幽子", f"回应:{snapshot.content}")


def _char() -> Character:
    return Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人", greeting="哟~"))


def _build_hub(clock=None) -> tuple[ChannelHub, list[_FakeWorld]]:
    """构建用假世界工厂的枢纽，返回 (hub, 创建过的世界列表)。"""
    worlds: list[_FakeWorld] = []

    def factory(channel_id, perceiver, mouth):
        world = _FakeWorld(perceiver, mouth)
        worlds.append(world)
        return world

    kwargs = {"clock": clock} if clock is not None else {}
    hub = ChannelHub(sessions=SessionManager(), character=_char(), world_factory=factory, **kwargs)
    return hub, worlds


async def _settle(cond, timeout: float = 1.0) -> bool:
    """轮询等待条件成立（避免固定 sleep 抖动）。"""
    for _ in range(int(timeout / 0.005)):
        if cond():
            return True
        await asyncio.sleep(0.005)
    return bool(cond())


# ---------------------------------------------------------------- 多路复用


async def test_hub_multiplexes_inputs_and_broadcasts_outputs():
    """同频道多连接：输入合流成一条流，输出广播给所有连接"""
    hub, worlds = _build_hub()
    a, b = _Sink(), _Sink()
    hub.attach("room", a)
    hub.attach("room", b)
    assert hub.online_count("room") == 2

    hub.submit("room", user="小明", text="你好")
    assert await _settle(lambda: bool(a.frames) and bool(b.frames))

    assert a.frames == b.frames, "两个连接应看到相同的广播流"
    assert any("回应:你好" in f.get("text", "") for f in a.frames)
    assert worlds[0].seen[0].sender == "小明", "快照应保留发言者"
    await hub.shutdown()


async def test_hub_merges_multiple_users_into_single_stream():
    """多路复用：不同连接的消息进同一条队列，世界按提交顺序处理"""
    hub, worlds = _build_hub()
    hub.attach("room", _Sink())
    hub.attach("room", _Sink())

    hub.submit("room", user="小明", text="一")
    hub.submit("room", user="小红", text="二")

    assert await _settle(lambda: len(worlds[0].seen) >= 2)
    assert [s.sender for s in worlds[0].seen[:2]] == ["小明", "小红"]
    assert worlds[0].seen[0].sender != worlds[0].seen[1].sender
    await hub.shutdown()


async def test_hub_isolates_channels():
    """不同频道各自一个世界，互不串台"""
    hub, worlds = _build_hub()
    hub.attach("room-a", _Sink())
    hub.attach("room-b", _Sink())
    assert len(worlds) == 2
    assert hub.channel_ids() == ["room-a", "room-b"]
    await hub.shutdown()


# ---------------------------------------------------------------- 回收


async def test_hub_detach_then_reap_idle():
    """连接断开不立即销毁；空闲超阈值后才回收世界"""
    clock = _Clock()
    hub, _ = _build_hub(clock=clock)
    sink = _Sink()
    hub.attach("room", sink)
    assert hub.channel_ids() == ["room"]

    hub.detach("room", sink)
    assert hub.online_count("room") == 0
    assert await hub.reap_idle() == [], "未到空闲阈值不应回收"

    clock.advance(1000)
    assert await hub.reap_idle() == ["room"]
    assert hub.channel_ids() == []


async def test_hub_keeps_channel_with_subscribers():
    """还有在线连接时不回收（无论空闲多久）"""
    clock = _Clock()
    hub, _ = _build_hub(clock=clock)
    hub.attach("room", _Sink())
    clock.advance(100000)
    assert await hub.reap_idle() == []
    await hub.shutdown()


# ---------------------------------------------------------------- 队列感知器


async def test_queue_perceiver_push_and_stop():
    """推入可取；请求停止后返回 None 让主循环退出"""
    eye = QueuePerceiver()
    eye.push(SceneSnapshot(sender="a", content="hi"))
    snapshot = await eye.next_snapshot()
    assert snapshot is not None
    assert snapshot.content == "hi"

    eye.request_stop()
    assert await eye.next_snapshot() is None


def test_queue_perceiver_drops_when_full():
    """队列满时丢弃新输入（背压保护），不阻塞调用方"""
    eye = QueuePerceiver(maxsize=1)
    eye.push(SceneSnapshot(content="1"))
    eye.push(SceneSnapshot(content="2"))
    assert eye.queue.qsize() == 1


# ---------------------------------------------------------------- 广播口


async def test_broadcast_mouth_streams_frames_to_all():
    """send/begin/delta/end 逐帧广播给所有订阅者"""
    mouth = BroadcastMouth()
    a, b = _Sink(), _Sink()
    mouth.attach(a)
    mouth.attach(b)

    await mouth.send("幽幽子", "你好")
    await mouth.begin("幽幽子")
    await mouth.delta("逐")
    await mouth.delta("字")
    await mouth.end()

    assert [f["type"] for f in a.frames] == ["message", "begin", "delta", "delta", "end"]
    assert a.frames == b.frames
    assert mouth.subscriber_count == 2

    mouth.detach(b)
    assert mouth.subscriber_count == 1


async def test_broadcast_mouth_drops_failing_sink():
    """投递失败的订阅者被自动摘除，不影响其他订阅者"""
    mouth = BroadcastMouth()

    class _BadSink:
        async def deliver(self, frame: dict) -> None:
            raise RuntimeError("boom")

    good = _Sink()
    mouth.attach(good)
    mouth.attach(_BadSink())

    await mouth.send("幽幽子", "你好")

    assert mouth.subscriber_count == 1
    assert good.frames


# ---------------------------------------------------------------- 端到端


class _EchoBackend:
    """brain 返回合法接力 JSON（一轮收尾），responder 返回固定回复。"""

    def __init__(self) -> None:
        self.reply_n = 0

    async def chat(self, messages, **kw):
        system_text = next((m.content for m in messages if m.role == "system"), "")
        if "决策模块" in system_text:
            return CompletionResult(
                content=(
                    '{"thought": "闲聊", "intent": "回应", "emotion": "平淡", '
                    '"action_hint": "回应", "confidence": 0.8, "need_continue_think": false}'
                )
            )
        self.reply_n += 1
        return CompletionResult(content=f"回复{self.reply_n}")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


async def test_hub_default_world_end_to_end(tmp_path):
    """默认世界工厂：真实 TouhouWorld + 广播口 + 队列感知器 端到端跑通"""
    sessions = SessionManager()
    sessions.set_default_backend(_EchoBackend())
    hub = ChannelHub(
        sessions=sessions,
        character=_char(),
        storage_dir=tmp_path,
        idle_ttl=0.0,
        world_kwargs={
            "ooc_audit": False,
            "ooc_retry": False,
            "stall_probability": 0.0,
            "initiative_interval": 9999.0,
        },
    )
    sink = _Sink()
    hub.attach("room", sink)
    await asyncio.sleep(0.05)  # 等世界启动（含开场白广播）

    hub.submit("room", user="小明", text="你好")

    assert await _settle(
        lambda: any("回复" in f.get("text", "") for f in sink.frames), timeout=3.0
    ), f"应产出回复，实际帧={sink.frames}"
    assert any(f.get("text") == "哟~" for f in sink.frames), "全新会话应广播开场白"

    await hub.shutdown()
    assert hub.channel_ids() == []
