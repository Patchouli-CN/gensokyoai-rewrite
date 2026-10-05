"""持久化后端 —— 可插拔的会话/记忆落盘层。

只定义"键 -> JSON 文档"的最小存储协议，业务语义（存什么、何时存）
归上层（roleplay 装配层的 SessionPersister）。默认实现是 JSON 文件
（tmp -> .bak -> os.replace 原子替换），换 SQLite 只需再实现一个后端。

文件 IO 走自家基础设施 [ayafileio](https://github.com/Patchouli-CN/ayafileio)
（Windows IOCP / Linux io_uring / macOS GCD 内核级真异步）——不占线程池、
不阻塞事件循环；`tmp.replace` 等同目录 rename 是元数据操作，保持同步。
"""

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any, Protocol

import ayafileio
import msgspec

from ..utils.logger import LoggerManager

_WARM_MISS = object()
""" 暖缓存未命中哨兵（预载结果本身可能是 None，不能用 None 判断命中）"""


class PersistenceBackend(Protocol):
    """持久化后端协议：key 寻址的 JSON 文档存取"""

    async def save(self, key: str, data: Any) -> None:
        """原子写入一个 JSON 文档（调用方保证 data 可 JSON 序列化）"""
        ...

    async def load(self, key: str) -> Any | None:
        """读取一个 JSON 文档；不存在或无法恢复地损坏时返回 None"""
        ...

    async def load_many(self, keys: list[str]) -> dict[str, Any | None]:
        """批量读取多个键；默认逐键并发，后端可覆写为真批量 IO"""
        if not keys:
            return {}
        values = await asyncio.gather(*(self.load(key) for key in keys))
        return dict(zip(keys, values, strict=True))

    def list_keys(self, prefix: str = "") -> list[str]:
        """列出已有键（按存储顺序），可按前缀过滤"""
        ...


class JsonFilePersistence:
    """JSON 文件持久化后端。

    布局：<root_dir>/<key>.json，key 可含子目录（如 "sessions/abc/session"）。

    崩溃安全三件套（沿用原版 GensokyoAI 机制）：
    - 原子替换：先写 .tmp 再 os.replace，写一半断电不留半个文件
    - .bak 备份：每次成功写盘前把旧文件复制为 .bak
    - 隔离区：主文件与 .bak 都损坏时移入 quarantine/ 留证，不阻塞启动
    """

    def __init__(self, root_dir: str | Path = "data", *, indent: int | None = 2) -> None:
        """初始化。

        Args:
            root_dir: 存储根目录
            indent: JSON 缩进空格数；None 或 <=0 表示紧凑单行。
                默认 2 —— 落盘文件是给人看与 diff 的，可读性优先于体积。
        """
        self._logger = LoggerManager.get_logger("PERSIST")
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._quarantine = self._root / "quarantine"
        self._locks: dict[str, asyncio.Lock] = {}
        self._warm: dict[str, Any] = {}
        """ prewarm() 预载的文档缓存；load() 命中即取走（一次性），零 IO """
        self._indent = indent if indent and indent > 0 else None
        """ 缩进宽度；None 表示输出紧凑单行 """

    def _encode(self, data: Any) -> bytes:
        """序列化为 JSON 字节。

        `msgspec.json.encode` 只出紧凑格式；要缩进得再过一道 `msgspec.json.format`
        （它才是 msgspec 的「美化」入口，`encode` 与 `Encoder` 都不收 indent）。

        Args:
            data: 待序列化对象

        Returns:
            bytes: JSON 字节；缩进模式下末尾补一个换行，文件更规整
        """
        content = msgspec.json.encode(data)
        if self._indent is None:
            return content
        return msgspec.json.format(content, indent=self._indent) + b"\n"

    def _path(self, key: str) -> Path:
        """key -> 目标文件路径（key 允许带子目录分隔符）"""
        return self._root / f"{key}.json"

    def _lock(self, key: str) -> asyncio.Lock:
        """每个 key 一把写锁，避免并发写同一文件"""
        return self._locks.setdefault(key, asyncio.Lock())

    async def save(self, key: str, data: Any) -> None:
        """原子写入：ayafileio 真异步执行 tmp -> .bak -> replace，零线程占用"""
        async with self._lock(key):
            await self._save_async(key, data)

    async def _save_async(self, key: str, data: Any) -> None:
        """真异步落盘（内核级完成，不经线程池）

        崩溃安全三件套不变：先写 tmp、旧文件复制为 .bak、 rename 替换。
        `tmp.replace(path)` 是同目录 rename（元数据操作，无数据 IO），保持同步。
        """
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self._encode(data)
        tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
        try:
            async with ayafileio.open(tmp, "wb") as handle:
                await handle.write(content)
            if path.exists():
                await self._copy_file(path, path.with_suffix(".json.bak"))
            tmp.replace(path)
        except Exception:
            self._logger.exception(f"写入失败: {key}")
            tmp.unlink(missing_ok=True)
            raise

    async def load(self, key: str) -> Any | None:
        """读主文件；损坏时尝试 .bak 恢复；两者皆坏则隔离并返回 None"""
        warm = self._warm.pop(key, _WARM_MISS)
        if warm is not _WARM_MISS:
            self._logger.debug(f"命中预载缓存，零 IO: {key}")
            return warm
        async with self._lock(key):
            return await self._load_async(key)

    async def load_many(self, keys: list[str]) -> dict[str, Any | None]:
        """批量读取：主文件存在的键一次 `read_bytes_many` 并发读回（并行打开）。

        回退规则（逐键隔离，不让一个坏键拖垮整批）：
        - 主文件缺失 / 解码失败 → 该键走单键 `load()`（含 .bak 恢复链）
        - 批量调用整体失败（如 exists() 之后文件被删的竞态）→ 整批回退单键路径
        """
        if not keys:
            return {}
        fast_keys: list[str] = []
        fast_paths: list[str] = []
        fallback: list[str] = []
        for key in keys:
            path = self._path(key)
            if path.exists():
                fast_keys.append(key)
                fast_paths.append(str(path))
            else:
                fallback.append(key)

        result: dict[str, Any | None] = {}
        if fast_keys:
            try:
                blobs = await ayafileio.read_bytes_many(fast_paths)
            except Exception:
                self._logger.exception("批量读取失败，整批回退单键路径")
                fallback.extend(fast_keys)
            else:
                for key, blob in zip(fast_keys, blobs, strict=True):
                    try:
                        result[key] = msgspec.json.decode(blob)
                    except Exception:
                        self._logger.exception(f"文件损坏: {self._path(key).name} (backup=False)")
                        fallback.append(key)
        if fallback:
            loaded = await asyncio.gather(*(self.load(key) for key in fallback))
            result.update(zip(fallback, loaded, strict=True))
        return result

    async def prewarm(self, keys: list[str]) -> int:
        """批量预载键到暖缓存；后续 `load()` 命中即取，零 IO。

        用于启动期已知将要恢复的键（如白名单频道的会话存档）：
        一次并发批量读替代 N 次冷启动单读。未消费的缓存会常驻到
        进程结束（体积即文档本身，键集合通常很小）。

        Returns:
            int: 本次实际预载的键数（已在缓存中的跳过）
        """
        pending = [key for key in keys if key not in self._warm]
        if not pending:
            return 0
        loaded = await self.load_many(pending)
        self._warm.update(loaded)
        return len(loaded)

    async def _load_async(self, key: str) -> Any | None:
        """真异步读取（ayafileio）；.bak 恢复成功后回写主文件"""
        path = self._path(key)
        for candidate, is_backup in ((path, False), (path.with_suffix(".json.bak"), True)):
            if not candidate.exists():
                continue
            try:
                async with ayafileio.open(candidate, "rb") as handle:
                    data = msgspec.json.decode(await handle.read())
            except Exception:
                self._logger.exception(f"文件损坏: {candidate.name} (backup={is_backup})")
                if is_backup:
                    self._quarantine_file(candidate)
                else:
                    self._logger.warning(f"主文件损坏，将尝试 .bak 恢复: {key}")
                continue
            if is_backup:
                await self._copy_file(candidate, path)  # 回写主文件，后续读取不再走备份
                self._logger.warning(f"已从 .bak 恢复主文件: {key}")
            return data
        return None

    @staticmethod
    async def _copy_file(src: Path, dst: Path) -> None:
        """文件复制，委托 ayafileio.acopy（OS 快车道 / 位置读写流水线兜底）。

        流水线自带短写循环（旧实现不检查 write 返回值，大文件有截断隐患）；
        Windows 快车道 CopyFile2 顺带保留元数据，对 .bak 灾难恢复场景无副作用。
        """
        await ayafileio.acopy(src, dst)

    def _quarantine_file(self, path: Path) -> None:
        """把无法恢复的坏文件移入隔离区留证，不让它阻塞启动"""
        try:
            self._quarantine.mkdir(parents=True, exist_ok=True)
            target = self._quarantine / f"{path.name}.{time.time_ns()}.corrupted"
            shutil.move(str(path), str(target))
            self._logger.warning(f"坏文件已隔离: {target.name}")
        except Exception:
            self._logger.exception(f"隔离失败: {path}")

    def list_keys(self, prefix: str = "") -> list[str]:
        """列出已有键（相对 root 的相对路径去扩展名）"""
        out = []
        for path in sorted(self._root.rglob("*.json")):
            rel = path.relative_to(self._root).with_suffix("")
            key = rel.as_posix()
            if key.startswith(prefix):
                out.append(key)
        return out
