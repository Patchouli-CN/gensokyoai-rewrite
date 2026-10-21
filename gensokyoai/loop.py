"""组装器 + 热路径循环 —— 全包唯一认识所有器官的地方。

一轮 = 一次模型判定：模型通过工具决定说不说、说几条、等多久。
说话、连发节奏、事件形状由场景决定；回合、未读、gap、记忆留在这里。
单轮失败按指数退避重试，循环不退出。
"""

import asyncio
import time

from .api import call_tool, spec_of
from .health import HealthCenter
from .memory import Memory
from .mind import Mind
from .room import Event, Room
from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("LOOP")

_BACKOFF_S = 1.0
""" 首次失败后的重试间隔（秒） """

_BACKOFF_MAX_S = 30.0
""" 退避上限（秒） """

_WINDOW = 30
""" 一次投递多少条未读 """

_MAX_STEPS = 4
""" 一轮里最多几次模型调用（read_events 会再来一次） """

_IMPORTANCE = 0.6
""" 记忆写入强度 """


class SceneContext:
    """库交给场景实例的上下文（每个房间一份）。"""

    def __init__(self, mouth, name: str, room: str, log) -> None:
        self.mouth = mouth
        self.name = name
        self.room = room
        self.log = log


class Loop:
    """一个房间的运转循环。"""

    def __init__(
        self,
        room: Room,
        mind: Mind,
        mouth,
        memory: Memory | None = None,
        health: HealthCenter | None = None,
        *,
        scene=None,
        window: int = _WINDOW,
        max_steps: int = _MAX_STEPS,
        retry_backoff: float = _BACKOFF_S,
        retry_backoff_max: float = _BACKOFF_MAX_S,
        bubble_gap: float | None = None,
    ) -> None:
        """组装。

        Args:
            room: 事件流
            mind: 心智（决定动作）
            mouth: 口层（场景默认用它投递）
            memory: 记忆（None = 不记不召回）
            health: 健康中心（None = 不记账）
            scene: 场景实例（None = 内置 chat 场景）
            window: 一次投递多少条未读
            max_steps: 一轮里最多几次模型调用
            retry_backoff: 首轮失败后的重试间隔（秒）
            retry_backoff_max: 退避上限（秒）
            bubble_gap: 连发间隔（None = 用场景声明的）
        """
        self.room = room
        self.mind = mind
        self.mouth = mouth
        self.memory = memory
        self._health = health
        self._scene = scene if scene is not None else _default_scene(room, mind, mouth)
        bubbles = spec_of(self._scene)["bubbles"]
        self._max_bubbles = max(1, int(bubbles.get("max", 1)))
        self._bubble_gap = (
            max(0.0, float(bubbles.get("gap_s", 0.0)))
            if bubble_gap is None
            else max(0.0, bubble_gap)
        )
        self._window = max(0, window)
        self._max_steps = max(1, max_steps)
        self._retry_backoff = max(0.0, retry_backoff)
        self._retry_backoff_max = max(self._retry_backoff, retry_backoff_max)
        self._backoff = self._retry_backoff
        self._wake = asyncio.Event()
        self._revealed: list[Event] = []
        self._revealed_gap: dict | None = None
        self._results: list[dict] = []

    def notify(
        self,
        kind: str,
        sender: str,
        text: str,
        *,
        user_id: str = "",
        mentioned: bool = False,
    ) -> None:
        """消息入房 + 唤醒循环。

        Args:
            kind: message 或 reply
            sender: 发送者
            text: 内容
            user_id: 发送者的稳定标识
            mentioned: 是否点名了角色
        """
        self.room.append(kind, sender, text, user_id=user_id, mentioned=mentioned)
        self._wake.set()

    async def run(self) -> None:
        """常驻循环（被取消退出；单轮失败退避重试）。"""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while True:
                started = time.monotonic()
                try:
                    await self._turn()
                except asyncio.CancelledError:
                    raise
                except Exception:  # 单轮失败不退出循环
                    _logger.exception(
                        f"{self.room.name} 回合失败，{self._backoff:.1f}s 后重试"
                    )
                    if self._health is not None:
                        self._health.count(f"{self.room.name}.turn_errors")
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, self._retry_backoff_max)
                    continue
                self._backoff = self._retry_backoff
                if self._health is not None:
                    self._health.observe(
                        f"{self.room.name}.turn_s", time.monotonic() - started
                    )
                break

    async def _turn(self) -> None:
        """一个回合：投递未读 → 模型选动作 → 照做 → 可能要再来一次。"""
        shown, gap = self.room.take_feed(self._window, self._revealed)
        self._revealed = []
        try:
            for _ in range(self._max_steps):
                if not shown:
                    return
                reply = await self.mind.step(await self._feed(shown, gap), self._scene)
                waited = None
                said = 0
                for call in reply.calls:
                    if call.name == "send_message":
                        if said >= self._max_bubbles:
                            continue
                        if said and self._bubble_gap > 0:
                            await asyncio.sleep(self._bubble_gap)
                        await self._say(_segments_of(call.arguments))
                        said += 1
                    elif call.name == "read_events":
                        self._revealed, self._revealed_gap = self.room.reveal(
                            _int(call.arguments.get("gap_id"), -1),
                            side=str(call.arguments.get("side") or "latest"),
                            limit=_int(call.arguments.get("limit"), 0),
                        )
                    elif call.name == "wait":
                        waited = max(waited or 0, _int(call.arguments.get("ms"), 0))
                    else:
                        self._results.extend(
                            await call_tool(self._scene, call.name, call.arguments)
                        )
                if self._revealed:
                    shown, gap = self._revealed, self._revealed_gap
                    self._revealed, self._revealed_gap = [], None
                    continue
                if self._results:
                    continue  # 工具结果回填，同一批事件再看一遍
                if waited is None:
                    return
                if not await self._sleep_for_event(waited):
                    return
                shown, gap = self.room.take_feed(self._window)
        except BaseException:
            self.room.push_back(shown)
            raise

    async def _feed(self, shown: list[Event], gap: dict | None) -> dict:
        events = (
            [self._scene.describe(event.wire()) for event in shown]
            if hasattr(self._scene, "describe")
            else [event.wire() for event in shown]
        )
        results, self._results = self._results, []
        return {
            "now": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "events": events,
            "gap": gap,
            "history": [event.wire() for event in self.room.take_history()],
            "memories": await self._recall(shown),
            "results": results,
        }

    async def _say(self, segments: list[dict], reply_to: int | None = None) -> None:
        """经场景出口投递，入房，记一笔。"""
        text = _text_of(segments)
        if not text:
            return
        await self._scene.say(segments, reply_to)
        self.room.append("reply", self.mind.name, text)
        if self._health is not None:
            self._health.count(f"{self.room.name}.says")
        await self._remember(text)

    async def _remember(self, text: str) -> None:
        """记忆是旁路：写失败只告警，不重试（否则发言会重播）。"""
        if self.memory is None:
            return
        trigger = _last_message(self.room.events)
        try:
            await self.memory.note(f"{trigger} / {self.mind.name}: {text}", _IMPORTANCE)
        except Exception:
            _logger.exception(f"{self.room.name} 记忆写入失败")

    async def _recall(self, shown: list[Event]) -> list[str]:
        """按这一轮最后一条消息召回；失败当没想起。"""
        if self.memory is None:
            return []
        query = _last_message(shown)
        if not query:
            return []
        try:
            return await self.memory.recall(query)
        except Exception:
            _logger.exception(f"{self.room.name} 记忆召回失败")
            return []

    async def _sleep_for_event(self, ms: int) -> bool:
        """等下一批消息或超时，返回是否等到了新消息。"""
        if ms <= 0:
            return self.room.unread > 0
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=ms / 1000)
        except TimeoutError:
            return False
        self._wake.clear()
        return True


def _default_scene(room: Room, mind: Mind, mouth):
    """没指定场景时用内置 chat。"""
    from .scenes.chat import ChatScene

    return ChatScene(
        SceneContext(mouth=mouth, name=mind.name, room=room.name, log=_logger)
    )


def _segments_of(arguments: dict) -> list[dict]:
    """模型给的话 -> 段列表。目前只有文本，场景可以自己扩。"""
    text = str(arguments.get("text") or "").strip()
    return [{"type": "text", "text": text}] if text else []


def _text_of(segments: list[dict]) -> str:
    """段列表里的纯文本部分。"""
    return "".join(
        str(segment.get("text", ""))
        for segment in segments
        if segment.get("type") == "text"
    ).strip()


def _last_message(events: list[Event]) -> str:
    """最近一条别人的话（记忆召回/写入的话题锚点）。"""
    for event in reversed(events):
        if event.kind == "message":
            return event.text
    return ""


def _int(value, default: int) -> int:
    """把模型给的参数转成整数，转不动就用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
