"""模型客户端测试 —— 工具调用解析 / 连接复用 / 超时 / 响应形状。"""

from typing import ClassVar

import aiohttp
import pytest

from gensokyoai.model import ChatModel, Reply


class _FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self._payload


class _FakeSession:
    """记录创建次数、会话级超时与请求体的假会话。"""

    instances: ClassVar[list["_FakeSession"]] = []
    payload: ClassVar[dict] = {"choices": [{"message": {"content": " 你好 "}}]}

    def __init__(self, *, timeout=None, **kwargs) -> None:
        self.timeout = timeout
        self.closed = False
        self.posts: list[tuple[str, dict]] = []
        type(self).instances.append(self)

    def post(self, url: str, json=None):
        self.posts.append((url, json))
        return _FakeResponse(type(self).payload)

    async def close(self) -> None:
        self.closed = True


def _install(monkeypatch, payload: dict | None = None) -> type[_FakeSession]:
    _FakeSession.instances = []
    _FakeSession.payload = payload or {"choices": [{"message": {"content": " 你好 "}}]}
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    return _FakeSession


async def test_chat_returns_text(monkeypatch):
    """纯文本回复：strip 后放进 Reply.text"""
    _install(monkeypatch)
    model = ChatModel("http://127.0.0.1:8080/v1")

    reply = await model.chat("sys", "user")

    assert isinstance(reply, Reply)
    assert reply.text == "你好" and reply.calls == ()
    await model.close()


async def test_tool_calls_are_parsed(monkeypatch):
    """arguments 是 JSON 字符串，要解成 dict；解不动的当空参"""
    _install(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "send_message",
                                    "arguments": '{"text": "在。"}',
                                },
                            },
                            {
                                "type": "function",
                                "function": {"name": "wait", "arguments": "不是 JSON"},
                            },
                        ],
                    }
                }
            ]
        },
    )
    model = ChatModel("http://127.0.0.1:8080/v1")

    reply = await model.chat("sys", "user", tools=[{"type": "function"}])

    assert [call.name for call in reply.calls] == ["send_message", "wait"]
    assert reply.calls[0].arguments == {"text": "在。"}
    assert reply.calls[1].arguments == {}
    await model.close()


async def test_tools_go_into_payload_only_when_given(monkeypatch):
    """给了 tools 才带 tool_choice"""
    fake = _install(monkeypatch)
    model = ChatModel("http://127.0.0.1:8080/v1", think=True)

    await model.chat("sys", "user", tools=[{"type": "function"}], max_tokens=64)
    await model.chat("sys", "user")

    with_tools = fake.instances[0].posts[0][1]
    without = fake.instances[0].posts[1][1]
    assert with_tools["tool_choice"] == "auto" and with_tools["tools"]
    assert "tools" not in without
    assert with_tools["chat_template_kwargs"]["enable_thinking"] is True
    await model.close()


async def test_chat_reuses_one_session(monkeypatch):
    """两次补全只建一个会话"""
    fake = _install(monkeypatch)
    model = ChatModel("http://127.0.0.1:8080/v1")

    await model.chat("sys", "user")
    await model.chat("sys", "user")

    assert len(fake.instances) == 1
    assert len(fake.instances[0].posts) == 2
    await model.close()


async def test_session_carries_configured_timeout(monkeypatch):
    """timeout 传给会话"""
    fake = _install(monkeypatch)
    model = ChatModel("http://127.0.0.1:8080/v1", timeout=7.5)

    await model.chat("sys", "user")

    assert fake.instances[0].timeout.total == 7.5
    await model.close()


async def test_malformed_response_raises(monkeypatch):
    """响应形状不对时抛 RuntimeError"""
    _install(monkeypatch, {"error": "no choices here"})
    model = ChatModel("http://127.0.0.1:8080/v1")

    with pytest.raises(RuntimeError, match="形状异常"):
        await model.chat("sys", "user")
    await model.close()


async def test_close_is_idempotent_and_reopens(monkeypatch):
    """close 可重复调用，之后能重建会话"""
    fake = _install(monkeypatch)
    model = ChatModel("http://127.0.0.1:8080/v1")
    await model.chat("sys", "user")

    await model.close()
    await model.close()
    await model.chat("sys", "user")

    assert fake.instances[0].closed is True
    assert len(fake.instances) == 2
    await model.close()


async def test_images_become_content_parts(monkeypatch):
    """带图时 user 消息变成 content 数组：文本 + 说明 + image_url"""
    fake = _install(monkeypatch)
    model = ChatModel("http://127.0.0.1:8080/v1")

    await model.chat(
        "sys", "{}", images=[("帕秋莉 发的第 1 张图：", "data:image/png;base64,AAAA")]
    )

    content = fake.instances[0].posts[0][1]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "{}"}
    assert content[1]["type"] == "text" and "帕秋莉" in content[1]["text"]
    assert content[2]["type"] == "image_url"
    assert content[2]["image_url"]["url"].startswith("data:image/png")
    await model.close()
