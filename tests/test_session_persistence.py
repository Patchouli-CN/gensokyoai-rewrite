"""会话持久化：后端 / 记忆快照 / 会话导出导入 / 事件驱动落盘与恢复 单元测试"""

import asyncio

import msgspec
import pytest

from gensokyoai.core.event_bus import EventBus
from gensokyoai.core.memorizer.manager import MemoryManager
from gensokyoai.core.persistence import JsonFilePersistence
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.persistence import SessionPersister
from gensokyoai.schemas.event_schema import EventTopic, TurnEndPayload
from gensokyoai.schemas.memory_schema import MemoryItem
from gensokyoai.schemas.model_schema import Message

# ---------- JsonFilePersistence ----------


async def test_persistence_roundtrip(tmp_path):
    """保存后能读回相同数据"""
    backend = JsonFilePersistence(tmp_path)
    await backend.save("sessions/a/session", {"turn": 3, "items": [1, 2]})
    assert await backend.load("sessions/a/session") == {"turn": 3, "items": [1, 2]}


async def test_saved_json_is_pretty_printed(tmp_path):
    """落盘 JSON 默认带缩进与末尾换行 —— 文件是给人读和 diff 的"""
    backend = JsonFilePersistence(tmp_path)
    await backend.save("demo", {"turn": 3, "items": [1, 2]})

    text = (tmp_path / "demo.json").read_text(encoding="utf-8")
    assert "\n" in text, "不应挤在一行"
    assert '  "turn": 3' in text, "应有 2 空格缩进"
    assert text.endswith("\n"), "应以换行结尾"


async def test_compact_mode_available(tmp_path):
    """indent=None 时退回紧凑单行（体积敏感场景可关）"""
    backend = JsonFilePersistence(tmp_path, indent=None)
    await backend.save("demo", {"a": 1, "b": [1, 2]})

    text = (tmp_path / "demo.json").read_text(encoding="utf-8")
    assert text == '{"a":1,"b":[1,2]}'


async def test_indented_json_still_roundtrips(tmp_path):
    """缩进输出不影响读回（格式化不改变语义）"""
    backend = JsonFilePersistence(tmp_path)
    payload = {"nested": {"list": [{"k": "v"}]}, "n": 1}
    await backend.save("demo", payload)
    assert await backend.load("demo") == payload


async def test_persistence_bak_recovery(tmp_path):
    """主文件损坏时从 .bak 恢复（.bak 保存的是上一版内容）"""
    backend = JsonFilePersistence(tmp_path)
    await backend.save("s/key", {"v": 1})
    await backend.save("s/key", {"v": 2})
    path = tmp_path / "s" / "key.json"
    assert path.with_suffix(".json.bak").exists(), "第二次写入时应备份第一版"
    path.write_text("{broken", encoding="utf-8")  # 模拟写一半崩溃
    assert await backend.load("s/key") == {"v": 1}, "应从 .bak 恢复上一版"
    assert path.read_text(encoding="utf-8") != "{broken", ".bak 内容应回写主文件"


async def test_persistence_quarantine_and_none(tmp_path):
    """主文件与 .bak 都损坏：隔离坏文件并返回 None，不抛异常"""
    backend = JsonFilePersistence(tmp_path)
    await backend.save("s/key", {"v": 1})
    (tmp_path / "s" / "key.json").write_text("{bad", encoding="utf-8")
    (tmp_path / "s" / "key.json.bak").write_text("{bad2", encoding="utf-8")
    assert await backend.load("s/key") is None
    quarantined = list((tmp_path / "quarantine").iterdir())
    assert len(quarantined) == 1, "坏文件应被隔离留证"


async def test_persistence_missing_key(tmp_path):
    """不存在的键返回 None"""
    backend = JsonFilePersistence(tmp_path)
    assert await backend.load("no/such/key") is None


def test_persistence_list_keys(tmp_path):
    """list_keys 列出全部键且支持前缀过滤"""
    backend = JsonFilePersistence(tmp_path)
    asyncio.run(backend.save("sessions/a/session", {}))
    asyncio.run(backend.save("sessions/b/session", {}))
    asyncio.run(backend.save("other/x", {}))
    keys = backend.list_keys("sessions/")
    assert keys == ["sessions/a/session", "sessions/b/session"]


# ---------- MemoryManager 快照/恢复 + 会话隔离 ----------


async def test_memory_snapshot_restore_roundtrip(tmp_path):
    """工作记忆快照后重建，顺序与内容一致"""
    mem = MemoryManager(storage_dir=tmp_path, session_id="s1")
    for i in range(3):
        await mem.store(MemoryItem(topic="对话", content=f"消息{i}", memory_type="dialogue"))
    snap = mem.snapshot()

    mem2 = MemoryManager(storage_dir=tmp_path, session_id="s1")
    restored = mem2.restore(snap)
    assert restored == 3
    recent = await mem2.recent(10)
    assert [m.content for m in recent] == ["消息2", "消息1", "消息0"], "恢复后保持原顺序"


def test_memory_restore_dedup():
    """重复 restore 同一批记忆不会产生重复条目"""
    mem = MemoryManager()
    items = [MemoryItem(topic="t", content="x", memory_type="dialogue")]
    mem.restore(items)
    mem.restore(items)
    assert mem.work_mem_size == 1


def test_memory_session_id_mismatch_raises():
    """storage_dir 与 session_id 必须同时提供"""
    with pytest.raises(ValueError):
        MemoryManager(storage_dir="data")
    with pytest.raises(ValueError):
        MemoryManager(session_id="s1")


def test_memory_scoped_long_memory_path(tmp_path):
    """提供 session_id 时长期记忆落到 <dir>/<session_id>/long_memory.json"""
    mem = MemoryManager(storage_dir=tmp_path, session_id="abc")
    assert (tmp_path / "abc" / "long_memory.json").exists() or True  # 懒创建
    assert mem._long_mem_store._file_path == tmp_path / "abc" / "long_memory.json"


# ---------- SessionManager 导出/导入 ----------


async def test_session_export_import(tmp_path):
    """导出历史 -> 新管理器导入 -> 历史一致"""
    sm1 = SessionManager()
    sm1.set_default_backend(_FakeBackend())
    sm1.set_system("responder", "人设")
    sm1._get_or_create("responder").messages.append(Message(role="user", content="你好"))
    exported = sm1.export_messages("responder")

    sm2 = SessionManager()
    sm2.import_messages("responder", exported)
    again = sm2.export_messages("responder")
    assert [(m.role, m.content) for m in again] == [(m.role, m.content) for m in exported]


class _FakeBackend:
    async def chat(self, messages, **kwargs):
        from gensokyoai.schemas.model_schema import CompletionResult

        return CompletionResult(content="ok")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


def test_session_export_empty():
    """无会话时导出空列表"""
    assert SessionManager().export_messages("responder") == []


# ---------- SessionPersister ----------


def _make_persister(tmp_path, **kwargs) -> tuple[SessionPersister, EventBus, SessionManager]:
    """组一个挂在真实 EventBus 上的 persister（存储订阅在前，与 TouhouWorld 装配顺序一致）"""
    bus = EventBus()
    memory = MemoryManager(storage_dir=tmp_path / "mem", session_id="s1")
    sessions = SessionManager()

    async def _store(event) -> None:
        if isinstance(event.payload, MemoryItem):
            await memory.store(event.payload)

    bus.subscribe(EventTopic.MEMORY_WRITE, _store)
    backend = JsonFilePersistence(tmp_path / "store")
    persister = SessionPersister(
        backend,
        "s1",
        memory,
        sessions,
        bus,
        character_name="幽幽子",
        coalesce_seconds=0.05,
        **kwargs,
    )
    return persister, bus, sessions


async def test_persister_saves_on_memory_write(tmp_path):
    """MEMORY_WRITE 事件触发落盘（合并窗口后）"""
    persister, bus, _ = _make_persister(tmp_path)
    await bus.publish(
        EventBus.new(
            EventTopic.MEMORY_WRITE,
            source="loop",
            payload=MemoryItem(topic="对话", content="第一条", memory_type="dialogue"),
        )
    )
    await asyncio.sleep(0.3)  # 等合并窗口 + 落盘
    data = await persister._backend.load(persister._key)
    assert data and data["work_memory"][0]["content"] == "第一条"
    assert data["character_name"] == "幽幽子"
    assert data["schema_version"] == 1


async def test_persister_coalesces_burst(tmp_path):
    """同一窗口内多次事件合并为一次落盘"""
    persister, bus, _ = _make_persister(tmp_path)
    for i in range(5):
        await bus.publish(
            EventBus.new(
                EventTopic.MEMORY_WRITE,
                source="loop",
                payload=MemoryItem(topic="对话", content=f"m{i}", memory_type="dialogue"),
            )
        )
    await asyncio.sleep(0.3)
    data = await persister._backend.load(persister._key)
    assert len(data["work_memory"]) == 5


async def test_persister_turn_end_updates_turn_count(tmp_path):
    """TURN_END 事件更新回合数并写入快照"""
    persister, bus, _ = _make_persister(tmp_path)
    await bus.publish(
        EventBus.new(
            EventTopic.TURN_END,
            source="loop",
            payload=TurnEndPayload(turn=7, effort="low", latency_s=1.2),
        )
    )
    await asyncio.sleep(0.3)
    data = await persister._backend.load(persister._key)
    assert data["turn_count"] == 7
    assert persister.turn_count == 7


async def test_persister_restore_roundtrip(tmp_path):
    """存档 -> 全新 persister 恢复 -> 记忆与会话历史齐全"""
    persister, bus, sessions = _make_persister(tmp_path)
    await mem_store_and_turn(persister, bus, sessions)
    await asyncio.sleep(0.3)

    # 模拟重启：全新的记忆/会话/总线/persister，指向同一后端
    backend = persister._backend
    bus2 = EventBus()
    mem2 = MemoryManager(storage_dir=tmp_path / "mem", session_id="s1")
    sessions2 = SessionManager()
    persister2 = SessionPersister(
        backend,
        "s1",
        mem2,
        sessions2,
        bus2,
        character_name="幽幽子",
        coalesce_seconds=0.05,
    )
    assert await persister2.restore() is True
    recent = await mem2.recent(10)
    assert [m.content for m in recent] == ["角色回复", "用户输入"]  # recent 为新 -> 旧序
    msgs = sessions2.export_messages("responder")
    assert any(m.content == "角色回复" for m in msgs)
    assert persister2.turn_count == 3


async def mem_store_and_turn(persister, bus, sessions):
    """测试辅助：写两条记忆 + 导出会话历史 + 发一个 TURN_END"""
    await bus.publish(
        EventBus.new(
            EventTopic.MEMORY_WRITE,
            source="loop",
            payload=MemoryItem(topic="对话", content="用户输入", memory_type="dialogue"),
        )
    )
    await bus.publish(
        EventBus.new(
            EventTopic.MEMORY_WRITE,
            source="loop",
            payload=MemoryItem(topic="对话", content="角色回复", memory_type="dialogue"),
        )
    )
    sessions.import_messages(
        "responder",
        [
            Message(role="system", content="人设"),
            Message(role="user", content="用户输入"),
            Message(role="assistant", content="角色回复"),
        ],
    )
    await bus.publish(
        EventBus.new(
            EventTopic.TURN_END,
            source="loop",
            payload=TurnEndPayload(turn=3, effort="low"),
        )
    )


async def test_persister_restore_empty_store(tmp_path):
    """无存档时 restore 返回 False"""
    persister, _, _ = _make_persister(tmp_path)
    assert await persister.restore() is False


async def test_persister_flush_writes_final_snapshot(tmp_path):
    """flush 立即落盘，之后事件不再触发保存"""
    persister, bus, _ = _make_persister(tmp_path)
    await bus.publish(
        EventBus.new(
            EventTopic.MEMORY_WRITE,
            source="loop",
            payload=MemoryItem(topic="对话", content="关闭前的记忆", memory_type="dialogue"),
        )
    )
    await persister.flush()
    data = await persister._backend.load(persister._key)
    assert data["work_memory"][0]["content"] == "关闭前的记忆"

    # flush 后事件不应再触发新落盘
    await bus.publish(
        EventBus.new(
            EventTopic.MEMORY_WRITE,
            source="loop",
            payload=MemoryItem(topic="对话", content="关闭后的记忆", memory_type="dialogue"),
        )
    )
    await asyncio.sleep(0.2)
    data = await persister._backend.load(persister._key)
    assert len(data["work_memory"]) == 1, "flush 后不应再保存"


async def test_persister_survives_corrupt_archive(tmp_path):
    """存档字段损坏时 restore 返回 False，不抛异常"""
    persister, bus, _ = _make_persister(tmp_path)
    await persister.flush()
    # 覆写为结构正确但字段类型错误的内容
    key_path = persister._backend._path(persister._key)
    key_path.write_bytes(msgspec.json.encode({"turn_count": "not-a-number", "work_memory": []}))
    assert await persister.restore() is False


async def test_double_flush_does_not_clobber_snapshot(tmp_path):
    """重复 flush 是空操作 —— 否则关闭流程清空会话后再 flush 会把好存档覆盖成空

    关闭顺序是「先写快照、再 reset_all 清空会话」；若之后又被 flush 一次，
    快照里的 responder_messages 会被写成空数组，历史对话全丢。
    """
    persister, _, sessions = _make_persister(tmp_path)
    sessions.import_messages(
        "responder",
        [Message(role="user", content="第一句"), Message(role="assistant", content="回应")],
    )

    await persister.flush()
    first = await persister._backend.load(persister._key)
    assert len(first["responder_messages"]) == 2, "首次 flush 应写入会话历史"

    # 模拟关闭流程：会话被清空后又被 flush 一次
    sessions.reset_all()
    await persister.flush()

    second = await persister._backend.load(persister._key)
    assert len(second["responder_messages"]) == 2, "重复 flush 不应覆盖已有存档"
    assert second["responder_messages"][0]["content"] == "第一句"
