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


class PersistenceBackend(Protocol):
    """持久化后端协议：key 寻址的 JSON 文档存取"""

    async def save(self, key: str, data: Any) -> None:
        """原子写入一个 JSON 文档（调用方保证 data 可 JSON 序列化）"""
        ...

    async def load(self, key: str) -> Any | None:
        """读取一个 JSON 文档；不存在或无法恢复地损坏时返回 None"""
        ...

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
        async with self._lock(key):
            return await self._load_async(key)

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
        """ayafileio 读+写的文件复制。

        不保留 `shutil.copy2` 的元数据（.bak 只用于灾难恢复，mtime 无意义），
        换取整条路径零线程占用。
        """
        async with ayafileio.open(src, "rb") as reader, ayafileio.open(dst, "wb") as writer:
            await writer.write(await reader.read())

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
