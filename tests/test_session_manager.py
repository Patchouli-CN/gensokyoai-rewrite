"""SessionManager 单元测试：双模式 / 裁剪 / 多模型路由"""

from gensokyoai.core.session_manager import SessionManager
from gensokyoai.schemas.model_schema import CompletionResult, Message, Usage


class FakeBackend:
    """记录调用并返回固定结果的假模型后端"""

    def __init__(self, tag: str = "回复") -> None:
        self.tag = tag
        self.calls: list[list[Message]] = []

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        self.calls.append(list(messages))
        return CompletionResult(content=f"{self.tag}{len(self.calls)}")


def _manager(backend: FakeBackend) -> SessionManager:
    """带默认 backend 的管理器"""
    sm = SessionManager()
    sm.set_default_backend(backend)
    return sm


async def test_stateless_call_does_not_persist():
    """无状态调用不落会话历史"""
    sm = _manager(FakeBackend())
    await sm.call("brain.think", [Message(role="user", content="hi")], stateless=True)
    assert sm.usage("brain.think") == 0


async def test_stateful_call_accumulates_history():
    """有状态调用累积历史并回写 assistant 回复"""
    backend = FakeBackend()
    sm = _manager(backend)
    await sm.call("responder", [Message(role="user", content="第一句")])
    await sm.call("responder", [Message(role="user", content="第二句")])
    # 第二次调用发送的历史应为：用户 + 首轮回复 + 新输入
    roles = [m.role for m in backend.calls[-1]]
    assert roles == ["user", "assistant", "user"], f"历史应为滑动窗口: {roles}"
    assert sm.usage("responder") > 0


async def test_trim_keeps_system_and_evicts_oldest():
    """超预算时 system 前缀保留，最早的非 system 消息先淘汰"""
    sm = _manager(FakeBackend())
    sm.set_system("r", "固定人设" * 10)
    sm._sessions["r"].max_tokens = 600  # 预算 600-512=88, 人设占 60
    await sm.call("r", [Message(role="user", content="消息" * 30)])
    await sm.call("r", [Message(role="user", content="新消息")])
    last_call = sm._sessions["r"].messages
    assert last_call[0].role == "system", "system 前缀必须保留"
    assert any(m.content == "新消息" for m in last_call), "最新消息必须保留"


async def test_reset_clears_owner_session():
    """reset 只清空指定 owner"""
    sm = _manager(FakeBackend())
    await sm.call("a", [Message(role="user", content="hi")])
    await sm.call("b", [Message(role="user", content="hi")])
    sm.reset("a")
    assert sm.usage("a") == 0
    assert sm.usage("b") > 0
    sm.reset_all()
    assert sm.usage("b") == 0


async def test_set_system_is_idempotent():
    """set_system 重复调用不产生重复 system 消息"""
    sm = _manager(FakeBackend())
    sm.set_system("r", "人设A")
    sm.set_system("r", "人设A")
    await sm.call("r", [Message(role="user", content="hi")])
    system_msgs = [m for m in sm._sessions["r"].messages if m.role == "system"]
    assert len(system_msgs) == 1


async def test_multi_backend_routes_by_owner():
    """每个 owner 可绑定独立 backend，未注册的走默认"""
    sm = SessionManager()
    brain_backend = FakeBackend("脑")
    default_backend = FakeBackend("通用")
    sm.register_backend("brain.think", brain_backend)
    sm.set_default_backend(default_backend)

    await sm.call("brain.think", [Message(role="user", content="hi")], stateless=True)
    await sm.call("responder", [Message(role="user", content="hi")], stateless=True)
    await sm.call("brain.ooc", [Message(role="user", content="hi")], stateless=True)

    assert len(brain_backend.calls) == 1, "brain.think 路由到专属 backend"
    assert len(default_backend.calls) == 2, "未注册 owner 回退默认 backend"


async def test_call_without_any_backend_raises():
    """既无专属也无默认 backend 时报错"""
    import pytest

    sm = SessionManager()
    with pytest.raises(ValueError):
        await sm.call("responder", [Message(role="user", content="hi")])


async def test_accumulates_token_usage():
    """调用累计 token 用量（供健康监控按回合算差值）"""

    class _UsageBackend:
        async def chat(self, messages, **kw):
            return CompletionResult(content="ok", usage=Usage(prompt_tokens=5, completion_tokens=7))

    sm = SessionManager()
    sm.set_default_backend(_UsageBackend())
    await sm.call("responder", [Message(role="user", content="hi")])

    assert sm.token_usage("responder") == Usage(prompt_tokens=5, completion_tokens=7)
    assert sm.total_usage() == Usage(prompt_tokens=5, completion_tokens=7)
    assert sm.token_usage("没调用过") == Usage()


async def test_context_usage_ratio_and_owners():
    """上下文占用率 = 已用 token / 预算；owners 列出活跃会话"""
    sm = _manager(FakeBackend())
    await sm.call("responder", [Message(role="user", content="hi")])

    assert 0.0 < sm.context_usage("responder") < 1.0
    assert sm.context_usage("不存在") == 0.0
    assert sm.owners() == ["responder"]


async def test_default_context_window_applies_to_new_sessions():
    """default_context_window 作为新建会话的默认预算"""
    sm = SessionManager(default_context_window=2048)
    sm.set_default_backend(FakeBackend())
    await sm.call("responder", [Message(role="user", content="hi")])
    assert sm._sessions["responder"].max_tokens == 2048


async def test_set_context_window_overrides_and_ignores_invalid():
    """set_context_window 覆盖预算；非法值（<=0）被忽略"""
    sm = SessionManager(default_context_window=1000)
    sm.set_context_window("responder", 4000)
    assert sm._sessions["responder"].max_tokens == 4000

    sm.set_context_window("responder", 0)
    assert sm._sessions["responder"].max_tokens == 4000, "非法值不应改动预算"
