"""房间（Room）—— append-only 事件流，全系统唯一事实源。

所有器官读它，所有产出写它。只追加、不改写——前缀稳定，天然缓存友好。
事件种类只有两种：别人的话（message）、自己的话（reply）。

房间自己记住"哪些还没给模型看过"：模型每次只拿到未读，更早的折成 gap，
由它自己决定要不要展开。
"""

import asyncio
import base64
import time
from pathlib import Path
from typing import Literal

import ayafileio
import msgspec

from .bgtasks import BgTasks
from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("ROOM")

type EventKind = Literal["message", "reply"]
""" message=别人的话 reply=角色发言 """

_MAX_EVENTS = 2000
""" 单房间保留的事件上界 """

_DEFAULT_WINDOW = 30
""" 一次最多投递多少条未读 """

_MAX_LOG_BYTES = 4 * 1024 * 1024
""" 单房间日志的大小上界，超了轮转到 .1（只留一份备份） """

_MAX_MEDIA_PER_EVENT = 4
""" 一条消息最多存几张图（多出来的当没发） """

_MAX_MEDIA_FILES = 60
""" 房间目录下最多留这么多张图，超了按时间丢最老的 """

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
)


def _sniff(data: bytes) -> str | None:
    """按文件头认 mime；认不出返回 None（存过的东西不保证还是图）。"""
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_RECENT_IMAGES = 2
""" 这批没图时，往回带几张最近的历史图 """

_CONTEXT = 12
""" 每轮附带多少条上下文（含自己的发言） """


class Event(msgspec.Struct, frozen=True):
    """房间里的一条事件。"""

    kind: EventKind
    sender: str
    text: str
    ts: float = msgspec.field(default_factory=time.time)
    msg_id: int = 0
    user_id: str = ""
    mentioned: bool = False
    images: tuple[str, ...] = ()
    """ 图片（data URL）。只活在内存里，不落盘 """

    def line(self) -> str:
        """一行纯文本（控制台回显用）。"""
        stamp = time.strftime("%H:%M", time.localtime(self.ts))
        pictures = f" [图×{len(self.images)}]" if self.images else ""
        return f"[{stamp}] {self.sender}: {self.text}{pictures}"

    def wire(self) -> dict:
        """给模型看的形状（图片只报数量，数据本身走多模态附件）。"""
        payload = {
            "msgId": self.msg_id,
            "name": self.sender,
            "userId": self.user_id,
            "text": self.text,
            "time": time.strftime("%H:%M", time.localtime(self.ts)),
            "mentioned": self.mentioned,
        }
        if self.images:
            payload["images"] = len(self.images)
        return payload


class RoomLog:
    """房间事件落盘：append-only JSONL，重启回放文件末尾一段。

    每条写进页缓存即返回，不逐条 flush（一次 flush 约 60 ms）；退出或轮转时
    关闭句柄收尾，进程被强杀最多丢掉最后几条。
    """

    def __init__(
        self, path: Path, tasks: BgTasks, *, max_bytes: int = _MAX_LOG_BYTES
    ) -> None:
        """初始化。

        Args:
            path: 日志文件路径
            tasks: 后台任务登记处（写盘走它，退出时由它收尾并关句柄）
            max_bytes: 单文件大小上界，超了轮转到 .1
        """
        self._path = path
        self._tasks = tasks
        self._max_bytes = max(1024, max_bytes)
        self._name = path.parent.name or path.stem
        self._media_dir = path.parent / "media"
        self._lock = asyncio.Lock()
        self._file: ayafileio.AsyncFile[str] | None = None
        self._size = 0
        tasks.on_shutdown(self.aclose)

    def load(self, limit: int | None = None) -> list[Event]:
        """回放文件末尾一段（同步，建房时调用）；坏行跳过。

        图片只存文件名（正文里不放 base64），加载时读回来还原成 data URL，
        所以重启之后"刚才那张图"还在。

        Args:
            limit: 最多取最近多少条（None = 仅受 max_bytes 约束）

        Returns:
            list[Event]: 按写入顺序的事件
        """
        if not self._path.exists():
            return []
        lines = [line for line in self._read_tail().splitlines() if line.strip()]
        if limit is not None:
            lines = lines[-limit:]
        events: list[Event] = []
        for number, line in enumerate(lines, 1):
            try:
                event = msgspec.json.decode(line, type=Event)
            except msgspec.DecodeError:
                _logger.warning(f"房间日志坏行已跳过: {self._path}:{number}")
                continue
            if event.images:
                event = msgspec.structs.replace(
                    event, images=self._read_media(event.images)
                )
            events.append(event)
        return events

    def _read_media(self, names: tuple[str, ...]) -> tuple[str, ...]:
        """按文件名把图读回 data URL；读不到就跳过（别让一条图拖垮回放）。"""
        found: list[str] = []
        for name in names:
            path = self._media_dir / name
            try:
                data = path.read_bytes()
            except OSError:
                continue
            mime = _sniff(data)
            if mime is None:
                continue
            found.append(f"data:{mime};base64,{base64.b64encode(data).decode()}")
        return tuple(found)

    def _save_media(self, event: Event) -> tuple[str, ...]:
        """把图写到旁边（只留文件名进 JSONL），顺手清掉太老的。"""
        if not event.images:
            return ()
        names: list[str] = []
        try:
            self._media_dir.mkdir(parents=True, exist_ok=True)
            for index, url in enumerate(event.images[:_MAX_MEDIA_PER_EVENT]):
                head, _, payload = url.partition(",")
                if not head.startswith("data:") or not payload:
                    continue
                try:
                    data = base64.b64decode(payload, validate=False)
                except (ValueError, TypeError):
                    continue
                name = f"{event.msg_id}-{index}.img"
                (self._media_dir / name).write_bytes(data)
                names.append(name)
        except OSError:
            return ()
        self._prune_media()
        return tuple(names)

    def _prune_media(self) -> None:
        """只留最近这些张图（按修改时间）。"""
        try:
            files = sorted(
                self._media_dir.glob("*.img"), key=lambda p: p.stat().st_mtime
            )
        except OSError:
            return
        for path in files[: max(0, len(files) - _MAX_MEDIA_FILES)]:
            try:
                path.unlink()
            except OSError:
                continue

    def _read_tail(self) -> str:
        """只读文件末尾 max_bytes，避免长历史拖住建房。"""
        start = max(0, self._path.stat().st_size - self._max_bytes)
        with self._path.open("rb") as file:
            if start:
                file.seek(start)
            text = file.read().decode("utf-8", errors="ignore")
        return text.split("\n", 1)[-1] if start else text

    def note(self, event: Event) -> asyncio.Task:
        """异步入盘；同房间的写经锁串行，保序。

        图片写旁边的 `media/`，JSONL 里只留文件名（base64 不进日志）。

        Returns:
            asyncio.Task: 写盘任务（想确认落盘可以 await 它）
        """
        if event.images:
            event = msgspec.structs.replace(event, images=self._save_media(event))
        line = msgspec.json.encode(event).decode() + "\n"
        return self._tasks.spawn(f"roomlog:{self._name}", self._write(line))

    async def aclose(self) -> None:
        """关闭句柄（可重复调用）。"""
        async with self._lock:
            await self._close_file()

    async def _write(self, line: str) -> None:
        size = len(line.encode())
        async with self._lock:
            if self._file is None or self._file.closed:
                await self._open()
            if self._size + size > self._max_bytes:
                await self._rotate()
            await self._file.write(line)
            self._size += size

    async def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._size = self._path.stat().st_size if self._path.exists() else 0
        self._file = ayafileio.open(self._path, "a", encoding="utf-8")

    async def _close_file(self) -> None:
        if self._file is not None and not self._file.closed:
            await self._file.close()
        self._file = None

    async def _rotate(self) -> None:
        await self._close_file()
        self._path.replace(self._path.with_name(self._path.name + ".1"))
        await self._open()


class Room:
    """一个房间 = 一条只追加的聊天流（一个群 / 一个私聊 / 一个终端）。"""

    def __init__(
        self, name: str, *, max_events: int = _MAX_EVENTS, log: RoomLog | None = None
    ) -> None:
        """初始化。

        Args:
            name: 房间名
            max_events: 事件上界，超了丢最老的
            log: 落盘（None = 不持久化；给了就先回放历史）
        """
        self.name = name
        self._max_events = max(1, max_events)
        self._log = log
        self.events: list[Event] = (
            log.load(limit=self._max_events) if log is not None else []
        )
        self._unread: list[Event] = []
        self._gaps: dict[int, list[Event]] = {}
        self._next_gap = 0
        self._next_msg = max((event.msg_id for event in self.events), default=0) + 1

    @property
    def unread(self) -> int:
        """还没投递给模型的条数。"""
        return len(self._unread)

    def append(
        self,
        kind: EventKind,
        sender: str,
        text: str,
        *,
        user_id: str = "",
        mentioned: bool = False,
        images: tuple[str, ...] = (),
    ) -> Event:
        """追加一条事件（唯一的写入口）。空文本且无图不入房。"""
        text = text.strip()
        if not text and not images:
            return Event(kind=kind, sender=sender, text="")
        event = Event(
            kind=kind,
            sender=sender,
            text=text,
            msg_id=self._next_msg,
            user_id=user_id or sender,
            mentioned=mentioned,
            images=tuple(images),
        )
        self._next_msg += 1
        self.events.append(event)
        self._trim()
        if kind == "message":
            self._unread.append(event)
        if self._log is not None:
            # 图片写旁边存文件，JSONL 里只留文件名（由 RoomLog 处理）
            self._log.note(event)
        return event

    def take_feed(
        self, window: int = _DEFAULT_WINDOW, extra: list[Event] | None = None
    ) -> tuple[list[Event], dict | None]:
        """取出这一轮的输入：未读里最新的 window 条，更早的折成 gap。

        Args:
            window: 一次最多给多少条
            extra: 额外要一起展示的事件（展开的 gap）

        Returns:
            tuple[list[Event], dict | None]: (展示的事件, gap 描述)
        """
        unread, self._unread = self._unread, []
        if window > 0 and len(unread) > window:
            shown, rest = unread[-window:], unread[:-window]
        else:
            shown, rest = unread, []
        gap = None
        if rest:
            gap_id = self._next_gap
            self._next_gap += 1
            self._gaps[gap_id] = rest
            gap = {
                "gapId": gap_id,
                "skipped": len(rest),
                "mentioned": any(event.mentioned for event in rest),
            }
        return [*(extra or ()), *shown], gap

    def reveal(
        self, gap_id: int, *, side: str = "latest", limit: int = 0
    ) -> tuple[list[Event], dict | None]:
        """展开一段 gap；没揭完的再挂成新 gap，一并返回。

        Args:
            gap_id: gap 编号
            side: earliest 从头揭 / latest 从尾揭
            limit: 最多揭几条（0 = 全揭）

        Returns:
            tuple[list[Event], dict | None]: (揭开的事件, 剩下的 gap 描述)
        """
        items = self._gaps.pop(gap_id, None)
        if items is None:
            return [], None
        count = len(items) if limit <= 0 else min(limit, len(items))
        if side == "earliest":
            taken, rest = items[:count], items[count:]
        else:
            taken, rest = items[len(items) - count :], items[: len(items) - count]
        gap = None
        if rest:
            gap = {
                "gapId": self._next_gap,
                "skipped": len(rest),
                "mentioned": any(event.mentioned for event in rest),
            }
            self._gaps[self._next_gap] = rest
            self._next_gap += 1
        return taken, gap

    def push_back(self, events: list[Event]) -> None:
        """把没消费完的消息退回未读（回合失败重试时用）。"""
        if events:
            self._unread = [*events, *self._unread]

    def recent_images(
        self, exclude: list[Event], *, limit: int = _RECENT_IMAGES
    ) -> list[Event]:
        """这一批之外最近的几条带图事件。

        图和文字不在同一批里的时候（先发图、下一句才问），模型靠它才接得上
        "刚才那张"。

        Args:
            exclude: 这一批已经投递的事件
            limit: 最多给几条带图事件（0 = 不给）

        Returns:
            list[Event]: 按时间顺序
        """
        if limit <= 0:
            return []
        skip = {event.msg_id for event in exclude if event.msg_id}
        found = [
            event for event in self.events if event.images and event.msg_id not in skip
        ]
        return found[-limit:]

    def context(self, exclude: list[Event], limit: int = _CONTEXT) -> list[Event]:
        """这一批之前的最近几条，含自己的发言——模型要靠它接得上话、不重复。

        Args:
            exclude: 这一批已经投递给模型的事件（不重复给）
            limit: 最多给几条（0 = 不给）

        Returns:
            list[Event]: 按时间顺序的上下文
        """
        if limit <= 0:
            return []
        skip = {event.msg_id for event in exclude if event.msg_id}
        recent = [event for event in self.events if event.msg_id not in skip]
        return recent[-limit:]

    def _trim(self) -> None:
        """超出上界时丢弃最老的整条。"""
        overflow = len(self.events) - self._max_events
        if overflow > 0:
            del self.events[:overflow]

    def last_activity(self) -> float:
        """最后一条事件的时间戳（DMN 判安静用；空房为 0）。"""
        return self.events[-1].ts if self.events else 0.0
