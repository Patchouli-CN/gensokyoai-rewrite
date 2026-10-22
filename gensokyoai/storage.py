"""append-only 存储 —— 业务只管往里塞，什么时候落盘由它自己决定。

写是顺序追加、带锁保序；落盘走"静默后批量刷"：

    · 有写在动 → 攒着（不刷）
    · 安静 idle_ms 之后 → 刷一次
    · 脏数据滞留超过 max_dirty_s → 立刻刷（否则一直有人说话就永远不刷）
    · aclose() → 刷完再关

flush 的语义跟底层一致（ayafileio 在 Windows 上是 FlushFileBuffers，实测
40–120 ms），所以绝不能按行刷；按批刷的代价可以忽略。
"""

import asyncio
import time
from pathlib import Path

import ayafileio

_MAX_BYTES = 10 * 1024 * 1024
""" 单文件大小上界，超了轮转到 .1 """

_IDLE_MS = 6000
""" 安静多久后自动落盘 """

_MAX_DIRTY_S = 30.0
""" 脏数据最多滞留多久（防止一直有人说话就永远不刷） """


class Storage:
    """追加写 + 空闲自动落盘。"""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = _MAX_BYTES,
        idle_ms: int = _IDLE_MS,
        max_dirty_s: float = _MAX_DIRTY_S,
        clock=time.monotonic,
        log=None,
    ) -> None:
        """初始化（不建文件，第一次写才开）。

        Args:
            path: 文件路径
            max_bytes: 超过就轮转成 `<path>.1`
            idle_ms: 安静多久后落盘（0 = 关闭自动落盘）
            max_dirty_s: 脏数据滞留上限（0 = 不设上限）
            clock: 时间源（测试注入）
            log: 日志器
        """
        self.path = Path(path)
        self._max_bytes = max(1024, max_bytes)
        self._idle = max(0.0, idle_ms / 1000.0)
        self._max_dirty = max(0.0, max_dirty_s)
        self._clock = clock
        self._log = log
        self._lock = asyncio.Lock()
        self._file: ayafileio.AsyncFile[str] | None = None
        self._size = 0
        self._dirty = False
        self._first_dirty = 0.0
        self._last_write = 0.0
        self._flusher: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()
        self._closing = False

    async def append(self, text: str) -> int:
        """追加一行（保序），必要时轮转；返回写入的字节数。

        Args:
            text: 要写的文本（调用方负责换行）

        Returns:
            int: 写入的字节数

        Raises:
            RuntimeError: 已经关闭
        """
        payload = text.encode()
        async with self._lock:
            if self._closing:
                raise RuntimeError("storage 已关闭")
            await self._ensure_open()
            if self._size + len(payload) > self._max_bytes:
                await self._rotate_locked()
            await self._file.write(text)
            self._size += len(payload)
            self._mark_dirty()

        await self._flush_if_overdue()
        self._arm_flusher()
        return len(payload)

    def write_soon(self, text: str) -> None:
        """把一行排进事件循环（同步调用点用）；没有循环就当场同步写。

        Args:
            text: 要写的文本（调用方负责换行）
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_sync(text)
            return
        task = loop.create_task(self.append(text))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _write_sync(self, text: str) -> None:
        """没有事件循环时的兜底：直接追加（写穿到 OS 即返回）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(text)
                self._size += len(text.encode())
        except OSError as error:
            if self._log is not None:
                self._log.warning(f"写 {self.path.name} 失败: {error}")

    async def flush(self) -> None:
        """立刻落盘（没脏数据就什么都不做）。"""
        async with self._lock:
            await self._flush_locked()

    async def aclose(self) -> None:
        """刷完、关句柄（可重复调用）。"""
        self._closing = True
        task, self._flusher = self._flusher, None
        if task is not None and not task.done():
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        async with self._lock:
            await self._flush_locked()
            await self._close_locked()

    @property
    def size(self) -> int:
        """已写入的字节数（本进程视角）。"""
        return self._size

    @property
    def dirty(self) -> bool:
        """有没有还没落盘的内容。"""
        return self._dirty

    # ---------- 内部 ----------

    def _mark_dirty(self) -> None:
        now = self._clock()
        if not self._dirty:
            self._first_dirty = now
            self._dirty = True
        self._last_write = now

    async def _flush_if_overdue(self) -> None:
        """脏数据滞留太久就立刻刷（防饿死）。"""
        if not self._dirty or self._max_dirty <= 0:
            return
        if self._clock() - self._first_dirty >= self._max_dirty:
            await self.flush()

    def _arm_flusher(self) -> None:
        """重新计时：有新写就把上一轮计时器取消。"""
        if self._idle <= 0 or self._closing:
            return
        task = self._flusher
        if task is not None and not task.done():
            task.cancel()
        self._flusher = asyncio.get_running_loop().create_task(self._flush_when_idle())

    async def _flush_when_idle(self) -> None:
        """安静够久就刷；期间又被写就退出，由新的计时器接手。"""
        while True:
            await asyncio.sleep(self._idle)
            if not self._dirty:
                return
            if self._clock() - self._last_write >= self._idle:
                await self.flush()
                return

    async def _flush_locked(self) -> None:
        if self._file is None or not self._dirty:
            return
        await self._file.flush()
        self._dirty = False
        self._first_dirty = 0.0

    async def _ensure_open(self) -> None:
        if self._file is not None and not self._file.closed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = ayafileio.open(self.path, "a", encoding="utf-8")
        try:
            self._size = self.path.stat().st_size
        except OSError:
            self._size = 0

    async def _close_locked(self) -> None:
        if self._file is not None:
            await self._file.close()
            self._file = None

    async def _rotate_locked(self) -> None:
        """关掉当前文件、备份成 .1、重新打开。"""
        await self._flush_locked()
        await self._close_locked()
        backup = self.path.with_name(self.path.name + ".1")
        try:
            if backup.exists():
                backup.unlink()
            self.path.replace(backup)
        except OSError as error:
            if self._log is not None:
                self._log.warning(f"日志轮转失败（{error}），继续写原文件")
            return
        self._size = 0
        await self._ensure_open()
        if self._log is not None:
            self._log.debug(f"日志已轮转: {self.path.name} -> {backup.name}")


__all__ = ["Storage"]
