"""append-only 存储 —— 提交者只管塞队列，落盘由后台 worker 负责。

    storage = Storage(path, tasks=tasks, idle_ms=6000, max_dirty_s=30)
    storage.write(line)          # 同步提交，不阻塞
    await storage.sync()         # 等队列里这批真的写下去（测试/收尾用）
    await storage.aclose()       # 排空 + 刷盘 + 关句柄 + 停 worker

flush 跟底层语义一致（ayafileio 在 Windows 上是 FlushFileBuffers，实测
40–120 ms），所以绝不按行刷；按批刷的成本可以忽略。
"""

import asyncio
import time
from pathlib import Path

import ayafileio

_MAX_BYTES = 10 * 1024 * 1024
_IDLE_MS = 6000
_MAX_DIRTY_S = 30.0
_BATCH = 64


class Storage:
    """追加写 + 队列 + 空闲落盘。"""

    def __init__(
        self,
        path,
        *,
        tasks=None,
        max_bytes=_MAX_BYTES,
        idle_ms=_IDLE_MS,
        max_dirty_s=_MAX_DIRTY_S,
        clock=time.monotonic,
        log=None,
    ) -> None:
        """初始化（第一次写才开文件和 worker）。

        Args:
            path: 文件路径
            tasks: 后台任务登记处（None = 自己 create_task）
            max_bytes: 超过就轮转成 `<path>.1`
            idle_ms: 队列安静多久后落盘（0 = 不做空闲落盘）
            max_dirty_s: 脏数据滞留上限（0 = 不设上限）
            clock: 时间源（测试注入）
            log: 日志器
        """
        self.path = Path(path)
        self._tasks = tasks
        self._max_bytes = max(1024, max_bytes)
        self._idle = max(0.0, idle_ms / 1000.0)
        self._max_dirty = max(0.0, max_dirty_s)
        self._clock = clock
        self._log = log
        self._queue: asyncio.Queue[tuple[str | None, asyncio.Future | None]] = (
            asyncio.Queue()
        )
        self._file: ayafileio.AsyncFile[str] | None = None
        self._size = 0
        self._dirty = False
        self._first_dirty = 0.0
        self._worker: asyncio.Task | None = None
        self._closing = False
        self._failure: Exception | None = None
        if tasks is not None:
            tasks.on_idle(self.sync)

    # ---------- 对外 ----------

    def write(self, text):
        """提交一行（同步、不阻塞）。

        Args:
            text: 要写的文本（调用方负责换行）

        Returns:
            asyncio.Future | None: 这一行写完时 resolve；没有事件循环时为 None

        Raises:
            RuntimeError: 已关闭或此前写入已经失败
        """
        if self._closing:
            raise RuntimeError("storage 已关闭")
        self._raise_failure()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_sync(text)
            return None
        future = loop.create_future()
        self._start_worker()
        self._queue.put_nowait((text, future))
        return future

    async def append(self, text):
        """提交一行并等它写完（测试与需要确认的调用方用）。

        Args:
            text: 要写的文本

        Returns:
            int: 写入的字节数
        """
        future = self.write(text)
        if future is not None:
            await future
        return len(text.encode())

    async def sync(self) -> None:
        """等队列里的东西都写下去（不等待 fsync），故障必须传给调用方。"""
        self._raise_failure()
        worker = self._worker
        if worker is None:
            if not self._queue.empty():
                raise RuntimeError("storage worker 未运行，队列未排空")
            return
        if worker.done():
            self._raise_failure()
            if not self._queue.empty():
                raise RuntimeError("storage worker 已停止，队列未排空")
            return
        marker = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((None, marker))
        await marker
        self._raise_failure()

    async def flush(self) -> None:
        """立刻落盘（没脏数据就什么都不做）。"""
        if self._file is None or not self._dirty:
            return
        await self._file.flush()
        self._dirty = False
        self._first_dirty = 0.0

    async def aclose(self) -> None:
        """排空队列、刷盘、关句柄、停 worker；已有故障不会被掩盖。"""
        if self._closing:
            self._raise_failure()
            return
        self._closing = True
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            self._queue.put_nowait((None, None))
            try:
                await asyncio.wait_for(worker, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                worker.cancel()
        try:
            await self.flush()
        except Exception as err:  # noqa: BLE001 - 故障要记录并传给等待方
            self._record_failure(err)
        try:
            await self._close_file()
        except Exception as err:  # noqa: BLE001 - 故障要记录并传给等待方
            self._record_failure(err)
        self._raise_failure()

    @property
    def size(self) -> int:
        """已写入的字节数（本进程视角）。"""
        return self._size

    @property
    def dirty(self) -> bool:
        """有没有还没落盘的内容。"""
        return self._dirty

    # ---------- worker ----------

    def _start_worker(self) -> None:
        worker = self._worker
        if worker is not None and not worker.done():
            return
        name = f"storage:{self.path.parent.name}/{self.path.name}"
        if self._tasks is not None:
            self._worker = self._tasks.spawn_worker(name, self._run())
        else:
            self._worker = asyncio.get_running_loop().create_task(
                self._run(), name=name
            )

    async def _run(self) -> None:
        """队列循环；首个 I/O 异常使存储进入可见的失败状态。"""
        try:
            while True:
                item = await self._next()
                if item is None:
                    return
                if item[0] is None:
                    await self._drain([item])
                    return
                batch = [item]
                return_now = False
                while len(batch) < _BATCH:
                    try:
                        more = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    batch.append(more)
                    if more[0] is None:
                        return_now = True
                        break
                await self._drain(batch)
                if return_now:
                    return
        except Exception as err:  # noqa: BLE001 - 故障要记录并传给等待方
            self._record_failure(err)
            if self._log is not None:
                self._log.opt(exception=err).error(f"存储 worker 失败: {self.path}")

    async def _next(self):
        """等下一件；空闲超过 idle 就先落盘再继续等。"""
        if self._idle <= 0:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=self._idle)
        except TimeoutError:
            await self.flush()
            return await self._queue.get()

    async def _drain(self, batch) -> None:
        """把这一批写下去；任何异常都会结束本批所有等待者。

        句柄每轮重新取：轮转会换掉 `self._file`，抓旧句柄会写到已关闭的文件上。
        """
        pending = [future for _text, future in batch if future is not None]
        try:
            await self._ensure_open()
            for text, _future in batch:
                if text is None:
                    continue
                payload = text.encode()
                if self._size + len(payload) > self._max_bytes:
                    await self._rotate()
                await self._write_locked(text)
                self._size += len(payload)
                self._mark_dirty()
            if (
                self._dirty
                and self._max_dirty > 0
                and self._clock() - self._first_dirty >= self._max_dirty
            ):
                await self.flush()
            for future in pending:
                if not future.done():
                    future.set_result(None)
        except Exception as err:
            for future in pending:
                if not future.done():
                    future.set_exception(err)
                    future.exception()
            raise

    # ---------- 内部 ----------

    def _record_failure(self, error: Exception) -> None:
        """记录首个故障，并让尚未接受的排队写入全部以异常结束。"""
        if self._failure is None:
            self._failure = error
        while True:
            try:
                _text, future = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if future is not None and not future.done():
                future.set_exception(error)
                future.exception()

    def _raise_failure(self) -> None:
        """如果存储已经进入故障状态，向当前调用方抛出明确异常。"""
        if self._failure is not None:
            raise RuntimeError("storage 此前写入失败") from self._failure

    def _mark_dirty(self) -> None:
        if not self._dirty:
            self._first_dirty = self._clock()
            self._dirty = True

    async def _write_locked(self, text: str) -> None:
        """写进当前句柄（句柄不存在就开一个）。"""
        await self._ensure_open()
        if self._file is not None:
            await self._file.write(text)

    async def _ensure_open(self) -> None:
        if self._file is not None and not self._file.closed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = ayafileio.open(self.path, "a", encoding="utf-8")
        try:
            self._size = self.path.stat().st_size
        except OSError:
            self._size = 0

    async def _close_file(self) -> None:
        if self._file is not None:
            await self._file.close()
            self._file = None

    async def _rotate(self) -> None:
        """关掉当前文件、备份成 .1、重新打开。"""
        await self.flush()
        await self._close_file()
        backup = self.path.with_name(self.path.name + ".1")
        try:
            if backup.exists():
                backup.unlink()
            self.path.replace(backup)
        except OSError as error:
            if self._log is not None:
                self._log.warning(f"日志轮转失败（{error}），继续写原文件")
            await self._ensure_open()
            return
        self._size = 0
        await self._ensure_open()
        if self._log is not None:
            self._log.debug(f"日志已轮转: {self.path.name} -> {backup.name}")

    def _write_sync(self, text) -> None:
        """没有事件循环时同步写入，失败同样记录并抛给调用方。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(text)
            self._size += len(text.encode())
        except OSError as error:
            self._record_failure(error)
            if self._log is not None:
                self._log.warning(f"写 {self.path.name} 失败: {error}")
            raise RuntimeError("storage 同步写入失败") from error


__all__ = ["Storage"]
