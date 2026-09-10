"""持久化后端 —— 可插拔的会话/记忆落盘层。

只定义"键 -> JSON 文档"的最小存储协议，业务语义（存什么、何时存）
归上层（roleplay 装配层的 SessionPersister）。默认实现是 JSON 文件
（tmp -> .bak -> os.replace 原子替换），换 SQLite 只需再实现一个后端。
"""

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any, Protocol

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

    def __init__(self, root_dir: str | Path = "data") -> None:
        self._logger = LoggerManager.get_logger("PERSIST")
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._quarantine = self._root / "quarantine"
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, key: str) -> Path:
        """key -> 目标文件路径（key 允许带子目录分隔符）"""
        return self._root / f"{key}.json"

    def _lock(self, key: str) -> asyncio.Lock:
        """每个 key 一把写锁，避免并发写同一文件"""
        return self._locks.setdefault(key, asyncio.Lock())

    async def save(self, key: str, data: Any) -> None:
        """原子写入：线程池里执行 tmp -> .bak -> replace，不阻塞事件循环"""
        async with self._lock(key):
            await asyncio.to_thread(self._save_sync, key, data)

    def _save_sync(self, key: str, data: Any) -> None:
        """同步落盘（在线程池中执行）"""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = msgspec.json.encode(data)
        tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
        try:
            tmp.write_bytes(content)
            if path.exists():
                shutil.copy2(path, path.with_suffix(".json.bak"))
            tmp.replace(path)
        except Exception:
            self._logger.exception(f"写入失败: {key}")
            tmp.unlink(missing_ok=True)
            raise

    async def load(self, key: str) -> Any | None:
        """读主文件；损坏时尝试 .bak 恢复；两者皆坏则隔离并返回 None"""
        async with self._lock(key):
            return await asyncio.to_thread(self._load_sync, key)

    def _load_sync(self, key: str) -> Any | None:
        """同步读取（在线程池中执行）；.bak 恢复成功后回写主文件"""
        path = self._path(key)
        for candidate, is_backup in ((path, False), (path.with_suffix(".json.bak"), True)):
            if not candidate.exists():
                continue
            try:
                data = msgspec.json.decode(candidate.read_bytes())
            except Exception:
                self._logger.exception(f"文件损坏: {candidate.name} (backup={is_backup})")
                if is_backup:
                    self._quarantine_file(candidate)
                else:
                    self._logger.warning(f"主文件损坏，将尝试 .bak 恢复: {key}")
                continue
            if is_backup:
                shutil.copy2(candidate, path)  # 回写主文件，后续读取不再走备份
                self._logger.warning(f"已从 .bak 恢复主文件: {key}")
            return data
        return None

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
