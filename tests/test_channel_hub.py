"""频道中枢 / 队列感知器 / 广播口 单元测试：多路复用与广播"""

import asyncio

from gensokyoai.core.config import WorldSettings
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.mouth import broadcast
from gensokyoai.mouth.broadcast import BroadcastMouth
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.hub import ChannelHub
from gensokyoai.satori.queue import QueuePerceiver
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
    """只与 satori/mouth 打交道的假世界：收到一条就回一句。"""

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
        world_settings=WorldSettings(
            ooc_audit=False,
            ooc_retry=False,
            stall_probability=0.0,
            initiative_interval=9999.0,
        ),
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


async def test_hub_prewarm_warms_shared_backend(tmp_path):
    """默认工厂下 prewarm 把频道会话批量预载进共享后端的暖缓存"""
    from gensokyoai.core.persistence import JsonFilePersistence

    seeder = JsonFilePersistence(tmp_path)
    await seeder.save("sessions/c1/session", {"turn_count": 7})
    await seeder.save("sessions/c2/session", {"turn_count": 3})

    hub = ChannelHub(
        sessions=SessionManager(),
        character=_char(),
        storage_dir=str(tmp_path),
    )
    assert await hub.prewarm(["c1", "c2", "c3"]) == 3
    warm = hub._persistence._warm
    assert warm["sessions/c1/session"] == {"turn_count": 7}
    assert warm["sessions/c2/session"] == {"turn_count": 3}
    assert warm["sessions/c3/session"] is None, "无存档的频道预载为 None"


async def test_hub_prewarm_noop_with_custom_factory():
    """自定义 world_factory 没有共享后端，prewarm 安全无操作"""
    hub, _ = _build_hub()
    assert await hub.prewarm(["c1"]) == 0


async def test_hub_default_world_isolates_sessions_across_channels(tmp_path):
    """默认工厂：频道间 responder 历史不串，回收一个频道不清空其他频道的人设"""

    class _Recording(_EchoBackend):
        def __init__(self) -> None:
            super().__init__()
            self.responder_calls: list[list] = []

        async def chat(self, messages, **kw):
            system_text = next((m.content for m in messages if m.role == "system"), "")
            if "决策模块" not in system_text:
                self.responder_calls.append(list(messages))
            return await super().chat(messages, **kw)

    backend = _Recording()
    sessions = SessionManager()
    sessions.set_default_backend(backend)
    clock = _Clock()
    hub = ChannelHub(
        sessions=sessions,
        character=_char(),
        storage_dir=tmp_path,
        clock=clock,
        world_settings=WorldSettings(
            ooc_audit=False, ooc_retry=False, stall_probability=0.0, initiative_interval=9999.0
        ),
    )
    sink_a, sink_b = _Sink(), _Sink()
    hub.attach("room-a", sink_a)
    hub.attach("room-b", sink_b)

    hub.submit("room-a", user="小明", text="A群的暗号是蓝莓")
    assert await _settle(lambda: len(backend.responder_calls) >= 1, timeout=3.0)
    hub.submit("room-b", user="小红", text="你好")
    assert await _settle(lambda: len(backend.responder_calls) >= 2, timeout=3.0)
    sent_b = " ".join(m.content for m in backend.responder_calls[-1])
    assert "蓝莓" not in sent_b, "B 频道的上下文不应包含 A 频道的消息"

    hub.detach("room-a", sink_a)
    clock.advance(10_000)
    assert await hub.reap_idle() == ["room-a"]

    hub.submit("room-b", user="小红", text="还在吗")
    assert await _settle(lambda: len(backend.responder_calls) >= 3, timeout=3.0)
    assert backend.responder_calls[-1][0].role == "system", "回收他人频道后本频道人设前缀应仍在"

    a_world_chars = {id(c.world.character) for c in hub._channels.values()}
    assert len(a_world_chars) == len(hub._channels)
    await hub.shutdown()


class _SlowStopWorld:
    """收到停止后还要「落盘」一会儿才退出的假世界（记录启动/退出时刻顺序）。"""

    def __init__(self, perceiver: QueuePerceiver, log: list[str], name: str) -> None:
        self._eye = perceiver
        self._log = log
        self._name = name

    async def start(self) -> None:
        self._log.append(f"{self._name}:start")
        while await self._eye.next_snapshot() is not None:
            pass
        await asyncio.sleep(0.05)  # 模拟 flush
        self._log.append(f"{self._name}:exit")


async def test_hub_replacement_channel_survives_old_world_exit():
    """关闭中的旧世界退出时不能把同名新频道摘掉；新世界等旧世界 flush 完才启动"""
    log: list[str] = []
    made: list[_SlowStopWorld] = []

    def factory(channel_id, perceiver, mouth):
        world = _SlowStopWorld(perceiver, log, f"w{len(made)}")
        made.append(world)
        return world

    hub = ChannelHub(sessions=SessionManager(), character=_char(), world_factory=factory)
    hub.attach("room", _Sink())
    await asyncio.sleep(0.01)

    stopping = asyncio.create_task(hub._stop_channel("room"))
    await asyncio.sleep(0)
    hub.submit("room", user="小明", text="我又回来了")  # 关闭窗口内重连
    await stopping
    # 轮询等新世界启动（固定 sleep 在机器繁忙时不够，会让断言偶发失败）
    assert await _settle(lambda: "w1:start" in log), f"新世界应启动: {log}"

    assert hub.channel_ids() == ["room"], "旧世界退出不应摘掉顶替它的新频道"
    assert hub._channels["room"].world is made[1]
    assert log.index("w0:exit") < log.index("w1:start"), f"新世界应等旧世界收尾: {log}"
    await hub.shutdown()


async def test_hub_crashed_world_hands_sinks_to_replacement():
    """世界异常退出后，仍挂着的连接交接给下一个同名世界，回复不投进死口层"""
    crash = {"on": True}

    class _CrashOnce(_FakeWorld):
        async def start(self) -> None:
            if crash.pop("on", False):
                raise RuntimeError("boom")
            await super().start()

    def factory(channel_id, perceiver, mouth):
        return _CrashOnce(perceiver, mouth)

    hub = ChannelHub(sessions=SessionManager(), character=_char(), world_factory=factory)
    sink = _Sink()
    hub.attach("room", sink)
    assert await _settle(lambda: hub.channel_ids() == [])

    hub.submit("room", user="小明", text="还在吗")
    assert await _settle(lambda: any("回应:还在吗" in f.get("text", "") for f in sink.frames))
    await hub.shutdown()


async def test_hub_run_reaper_reclaims_idle_channels():
    """常驻回收节拍：空闲频道会被周期性回收（此前只有测试调用 reap_idle）"""
    clock = _Clock()
    hub, _ = _build_hub(clock=clock)
    sink = _Sink()
    hub.attach("room", sink)
    hub.detach("room", sink)
    clock.advance(10_000)

    reaper = asyncio.create_task(hub.run_reaper(interval=0.01))
    try:
        assert await _settle(lambda: hub.channel_ids() == [])
    finally:
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)


async def test_broadcast_slow_sink_does_not_stall_others(monkeypatch):
    """一个不读数据的连接卡住投递：其他连接照常收到，卡死的连接超时后被摘除"""
    monkeypatch.setattr(broadcast, "_DELIVER_TIMEOUT_S", 0.05)

    class _Stuck:
        async def deliver(self, frame: dict) -> None:
            await asyncio.sleep(10)

    mouth = BroadcastMouth()
    good, stuck = _Sink(), _Stuck()
    mouth.attach(good)
    mouth.attach(stuck)
    await asyncio.wait_for(mouth.send("幽幽子", "你好"), timeout=1.0)
    assert good.frames, "正常连接应收到"
    assert mouth.subscriber_count == 1, "卡死的连接应被摘除"
