"""组装器 + 热路径循环 —— 全包唯一认识所有器官的地方。

一轮 = 一次模型判定：模型通过工具决定说不说、说几条、等多久。
说话、连发节奏、事件形状由场景决定；回合、未读、gap、记忆留在这里。
单轮失败按指数退避重试，循环不退出。
"""

import asyncio
import json
import random
import re
import time
from collections import deque
from collections.abc import Callable

from .api import call_tool, spec_of
from .health import HealthCenter
from .llm.base import ModelError
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

_MAX_ATTEMPTS = 3
""" 同一批消息最多试几次（4xx 直接放弃，不纠缠） """

_DEDUP_WINDOW_S = 20.0
""" 同一句话在这个窗口内不重复发（秒） """


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
        max_attempts: int = _MAX_ATTEMPTS,
        retry_backoff: float = _BACKOFF_S,
        retry_backoff_max: float = _BACKOFF_MAX_S,
        bubble_gap: float | None = None,
        dedup_window: float = _DEDUP_WINDOW_S,
        rng: Callable[[], float] = random.random,
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
            dedup_window: 同一句话多少秒内不重复发（0 = 不拦）
            rng: 唤醒概率用的随机源（测试注入）
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
        self._max_attempts = max(1, max_attempts)
        self._attempts = 0
        self._retry_backoff = max(0.0, retry_backoff)
        self._retry_backoff_max = max(self._retry_backoff, retry_backoff_max)
        self._backoff = self._retry_backoff
        self._wake = asyncio.Event()
        self._revealed: list[Event] = []
        self._revealed_gap: dict | None = None
        self._results: list[dict] = []
        self._dedup_window = max(0.0, dedup_window)
        self._said: deque[tuple[float, str]] = deque()
        """ 最近发出去的（时间, 文本），防同一句在短时间里说两遍 """
        wake = spec_of(self._scene)["wake"]
        self._prose_policy = spec_of(self._scene).get("on_prose", "drop")
        self._wake_on_mention = _chance(wake.get("on_mention", 1.0))
        self._wake_on_others = _chance(wake.get("on_others", 1.0))
        self._wake_cooldown = max(0.0, float(wake.get("cooldown_s", 0.0)))
        self._rng = rng
        self._last_spoke = 0.0
        """ 上次开口的时间；冷却期内没被点名就不醒 """

    def notify(
        self,
        kind: str,
        sender: str,
        text: str,
        *,
        user_id: str = "",
        mentioned: bool = False,
        images: tuple[str, ...] = (),
    ) -> None:
        """消息入房，按场景的唤醒策略决定开不开一轮。

        Args:
            kind: message 或 reply
            sender: 发送者
            text: 内容（可以是空的，只要带图）
            user_id: 发送者的稳定标识
            mentioned: 是否点名了角色
            images: 图片（data URL；只活在内存里，不落盘）
        """
        self.room.append(
            kind,
            sender,
            text,
            user_id=user_id,
            mentioned=mentioned,
            images=images,
        )
        woke, reason = self._decide_wake(mentioned)
        _logger.debug(f"{self.room.name} {'唤醒' if woke else '不唤醒'}（{reason}）")
        if woke:
            self._wake.set()

    def _decide_wake(self, mentioned: bool) -> tuple[bool, str]:
        """要不要因为这条消息开一轮，以及理由（日志里要说清是哪种原因）。"""
        if mentioned:
            return True, "点名"
        if self._wake_cooldown > 0:
            gap = time.monotonic() - self._last_spoke
            if gap < self._wake_cooldown:
                return False, f"冷却中（还差 {self._wake_cooldown - gap:.0f}s）"
        if self._rng() < self._wake_on_others:
            return True, f"概率命中 {self._wake_on_others:g}"
        return False, f"概率没中 {self._wake_on_others:g}"

    def _should_wake(self, mentioned: bool) -> bool:
        """要不要因为这条消息开一轮（消息本身已经入房，不影响上下文）。"""
        return self._decide_wake(mentioned)[0]

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
                except Exception as err:  # noqa: BLE001 - 单轮任何失败都不该让房间死掉
                    if self._health is not None:
                        self._health.count(f"{self.room.name}.turn_errors")
                    self._attempts += 1
                    if self._give_up(err):
                        _logger.error(
                            f"{self.room.name} 放弃这一批"
                            f"（第 {self._attempts} 次）：{err}"
                        )
                        self._attempts = 0
                        self.room.take_feed(self._window)  # 别拿坏输入卡死房间
                        break
                    _logger.warning(
                        f"{self.room.name} 回合失败，{self._backoff:.1f}s 后重试"
                        f"（{err}）"
                    )
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, self._retry_backoff_max)
                    continue
                self._backoff = self._retry_backoff
                self._attempts = 0
                if self._health is not None:
                    self._health.observe(
                        f"{self.room.name}.turn_s", time.monotonic() - started
                    )
                break

    async def _step(self, shown: list[Event], gap: dict | None):
        """问模型一次；带图被端点拒了就去掉图再问一次（图不该让整轮失联）。"""
        images = _images_of(shown)
        if not images:
            # 这一批没图但刚才发过：把最近的图带上，否则"她是谁"接不上
            images = _images_of(self.room.recent_images(shown))
            if images:
                _logger.debug(f"{self.room.name} 这批没图，带上 {len(images)} 张历史图")
        payload = await self._feed(shown, gap)
        try:
            return await self.mind.step(payload, self._scene, images=images)
        except ModelError as err:
            if not images or err.retryable:
                raise
            _logger.warning(
                f"{self.room.name} 带图请求被拒（HTTP {err.status}），去掉图重试"
            )
            return await self.mind.step(payload, self._scene, images=None)

    def _give_up(self, err: Exception) -> bool:
        """这一批要不要放弃：请求本身有问题（4xx）重试没用，重试次数到顶也不再纠缠。"""
        if isinstance(err, ModelError) and not err.retryable:
            return True
        return self._attempts >= self._max_attempts

    async def _turn(self) -> None:
        """一个回合：投递未读 → 模型选动作 → 照做 → 可能要再来一次。"""
        shown, gap = self.room.take_feed(self._window, self._revealed)
        self._revealed = []
        try:
            for _ in range(self._max_steps):
                if not shown:
                    return
                reply = await self._step(shown, gap)
                if not reply.calls:
                    await self._on_prose(reply.text)
                    return
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
            "history": [event.wire() for event in self.room.context(shown)],
            "memories": await self._recall(shown),
            "results": results,
        }

    async def _on_prose(self, text: str) -> None:
        """模型没调工具、只写了正文。

        常见的两种情况：真写了句话（带图时尤其容易），或者把工具调用写成了
        正文——`send_message text ...`、`{"name": "send_message", ...}` 这种。
        后者要先抢救出真正要说的话，抢救不出来就当它什么也没说。
        """
        raw = text.strip()
        if not raw:
            return
        salvaged = _salvage(raw)
        if not salvaged:
            _logger.warning(
                f"{self.room.name} 模型写成了工具调用格式却没调工具，已丢弃: "
                f"{raw[:40]!r}"
            )
            return
        if self._prose_policy != "send":
            _logger.warning(
                f"{self.room.name} 模型只写了正文、没调工具（协议要求工具调用，"
                f"已丢弃）: {salvaged[:40]!r}"
            )
            return
        _logger.warning(
            f"{self.room.name} 模型只写了正文、没调工具，按场景策略直接发出"
            + ("（已从工具调用格式里抢救）" if salvaged != raw else "")
        )
        await self._say([{"type": "text", "text": salvaged}])

    async def _say(self, segments: list[dict], reply_to: int | None = None) -> None:
        """经场景出口投递，入房，记一笔。"""
        text = _text_of(segments)
        if not text:
            return
        if hasattr(self._scene, "clean"):
            text = self._scene.clean(text) or text
        if self._is_repeat(text):
            _logger.debug(f"{self.room.name} 同一句刚说过，丢弃: {text[:30]!r}")
            return
        await self._scene.say(segments, reply_to)
        self._said.append((time.monotonic(), text))
        self._last_spoke = time.monotonic()
        self.room.append("reply", self.mind.name, text)
        if self._health is not None:
            self._health.count(f"{self.room.name}.says")
        await self._remember(text)

    def _is_repeat(self, text: str) -> bool:
        """这句话是不是刚说过（窗口内逐字相同）。"""
        if self._dedup_window <= 0:
            return False
        now = time.monotonic()
        while self._said and now - self._said[0][0] > self._dedup_window:
            self._said.popleft()
        return any(previous == text for _, previous in self._said)

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


def _salvage(text: str) -> str:
    """从"写成正文的工具调用"里抠出真正要说的话；抠不出来返回空串。

    认这些形状：`send_message text 你好`、`send_message("你好")`、
    裸 JSON、`<tool_call>{...}</tool_call>`。普通句子原样返回。
    """
    raw = text.strip()
    if not raw:
        return ""
    if re.fullmatch(r"(send_message|wait|read_events)", raw, re.I):
        return ""

    block = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", raw, re.S)
    if block:
        return _payload_of(block.group(1))
    if raw.startswith("{") and raw.endswith("}"):
        return _payload_of(raw)

    match = re.match(
        r"(?:send_message|wait|read_events)\s*(?P<open>[(（:：])?\s*"
        r"(?:text|message|content)?\s*[:=]?\s*(?P<body>.+)$",
        raw,
        re.S | re.I,
    )
    if match:
        body = match.group("body")
        if match.group("open") in ("(", "（"):
            body = body.rstrip(")）").rstrip()
        return _unquote(body)
    return raw


def _payload_of(raw: str) -> str:
    """从 JSON 里取要说的话（认 text / message / content，或嵌在 arguments 里）。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("text", "message", "content"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return _unquote(value)
    arguments = data.get("arguments")
    if isinstance(arguments, dict):
        return _payload_of(json.dumps(arguments, ensure_ascii=False))
    if isinstance(arguments, str):
        return _salvage(arguments)
    return ""


def _unquote(text: str) -> str:
    """去掉包裹的引号/括号，别把标点当内容。"""
    cleaned = text.strip()
    for left, right in (('"', '"'), ("'", "'"), ("「", "」"), ("“", "”"), ("`", "`")):
        if len(cleaned) > 1 and cleaned.startswith(left) and cleaned.endswith(right):
            cleaned = cleaned[1:-1].strip()
            break
    return cleaned


def _images_of(shown: list[Event]) -> list[tuple[str, str]]:
    """把这一批里的图收集成 [(说明, data URL)]，顺序与 events 一致。"""
    found: list[tuple[str, str]] = []
    for event in shown:
        for index, url in enumerate(event.images, 1):
            found.append((f"{event.sender} 发的第 {index} 张图：", url))
    return found


def _chance(value) -> float:
    """把概率夹到 [0, 1]；写错了当必醒。"""
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _int(value, default: int) -> int:
    """把模型给的参数转成整数，转不动就用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
