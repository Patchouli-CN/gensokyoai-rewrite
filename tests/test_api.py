"""插件 API —— 装饰器、注册表、工具 schema、工具调用。"""

import pytest

from gensokyoai.api import (
    API_VERSION,
    call_tool,
    drop_module,
    scene,
    scenes,
    schema_of,
    spec_of,
    tool,
    tools_of,
)


def test_scene_decorator_records_spec():
    """@scene 把名字/提示词/连发策略/动作记在类上"""

    @scene(
        name="t_basic",
        prompt="在测试里。",
        bubbles={"max": 2, "gap_s": 0.1},
        actions=("say", "wait"),
    )
    class Demo:
        pass

    spec = spec_of(Demo)

    assert spec["name"] == "t_basic" and spec["prompt"] == "在测试里。"
    assert spec["bubbles"] == {"max": 2, "gap_s": 0.1}
    assert spec["actions"] == ("say", "wait")
    assert scenes()["t_basic"] is Demo
    assert spec_of(Demo())["name"] == "t_basic", "实例也能取到声明"


def test_scene_defaults_and_unknown_actions_dropped():
    """默认只有 say、单条；不认识的动作被丢掉"""

    @scene(name="t_min")
    class Minimal:
        pass

    assert spec_of(Minimal)["actions"] == ("say",)
    assert spec_of(Minimal)["bubbles"] == {"max": 1, "gap_s": 0.0}

    @scene(name="t_bad", actions=("say", "跳舞"))
    class Bad:
        pass

    assert spec_of(Bad)["actions"] == ("say",)


def test_duplicate_scene_name_from_other_module_raises():
    """两个模块抢同一个场景名：直接报错，不静默覆盖"""

    @scene(name="t_dup")
    class First:
        pass

    First.__module__ = "somewhere.else"

    with pytest.raises(ValueError, match="场景重名"):

        @scene(name="t_dup")
        class Second:
            pass


def test_drop_module_removes_its_scenes():
    """版本不匹配时可以整体卸掉一个模块的场景"""

    @scene(name="t_drop_me")
    class Dropped:
        pass

    drop_module(Dropped.__module__)

    assert "t_drop_me" not in scenes()


def test_tool_decorator_and_schema():
    """@tool 记元信息，schema_of 转成 OpenAI 声明"""

    class Demo:
        @tool(
            "read_member", "读一个人的名片", {"user_id": "string", "limit": "integer"}
        )
        async def read_member(self, user_id, limit=3):
            return []

    metas = tools_of(Demo)
    assert [meta["name"] for meta in metas] == ["read_member"]

    schema = schema_of(metas[0])
    params = schema["function"]["parameters"]
    assert schema["function"]["description"] == "读一个人的名片"
    assert params["properties"]["limit"]["type"] == "integer"
    assert params["required"] == ["user_id", "limit"]


async def test_call_tool_filters_args_and_supports_sync():
    """场景工具只收到自己声明的参数；同步/异步都能调"""

    class Demo:
        @tool("ping", "回声", {"text": "string"})
        def ping(self, text):
            return [{"text": f"pong:{text}"}]

    assert await call_tool(Demo(), "ping", {"text": "a", "多余": 1}) == [
        {"text": "pong:a"}
    ]
    assert await call_tool(Demo(), "不存在", {}) == [], "没声明的工具返回空"


def test_api_version_is_a_string():
    """版本号是个字符串，握手用"""
    assert isinstance(API_VERSION, str) and API_VERSION
