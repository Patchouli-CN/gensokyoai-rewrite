"""SessionManager 单元测试"""

from gensokyoai.core.session_manager import SessionManager
from gensokyoai.schemas.model_schema import CompletionResult, Message


class FakeBackend:
    """记录调用并返回固定结果的假模型后端"""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        self.calls.append(list(messages))
        return CompletionResult(content=f"回复{len(self.calls)}")


async def test_stateless_call_does_not_persist():
    """无状态调用不落会话历史"""
    backend = FakeBackend()
    sm = SessionManager(backend)
    await sm.call("brain.think", [Message(role="user", content="hi")], stateless=True)
    assert sm.usage("brain.think") == 0
    assert len(backend.calls) == 1


async def test_stateful_call_accumulates_history():
    """有状态调用累积历史并回写 assistant 回复"""
    backend = FakeBackend()
    sm = SessionManager(backend)
    await sm.call("responder", [Message(role="user", content="第一句")])
    await sm.call("responder", [Message(role="user", content="第二句")])
    history = backend.calls[-1]
    roles = [m.role for m in history]
    assert roles == ["user", "assistant", "user"], f"历史应为 滑动窗口: {roles}"
    assert sm.usage("responder") > 0


async def test_trim_keeps_system_and_evicts_oldest():
    """超预算时 system 前缀保留，最早的非 system 消息先淘汰"""
    backend = FakeBackend()
    sm = SessionManager(backend)
    sm.set_system("r", "固定人设" * 10)
    sm._sessions["r"].max_tokens = 600  # 预算 600-512=88, 人设占 60
    await sm.call("r", [Message(role="user", content="消息" * 30)])
    await sm.call("r", [Message(role="user", content="新消息")])
    last_call = backend.calls[-1]
    assert last_call[0].role == "system", "system 前缀必须保留"
    assert any(m.content == "新消息" for m in last_call), "最新消息必须保留"


async def test_reset_clears_owner_session():
    """reset 只清空指定 owner"""
    backend = FakeBackend()
    sm = SessionManager(backend)
    await sm.call("a", [Message(role="user", content="hi")])
    await sm.call("b", [Message(role="user", content="hi")])
    sm.reset("a")
    assert sm.usage("a") == 0
    assert sm.usage("b") > 0
    sm.reset_all()
    assert sm.usage("b") == 0


async def test_set_system_is_idempotent():
    """set_system 重复调用不产生重复 system 消息"""
    backend = FakeBackend()
    sm = SessionManager(backend)
    sm.set_system("r", "人设A")
    sm.set_system("r", "人设A")
    await sm.call("r", [Message(role="user", content="hi")])
    system_msgs = [m for m in backend.calls[-1] if m.role == "system"]
    assert len(system_msgs) == 1
