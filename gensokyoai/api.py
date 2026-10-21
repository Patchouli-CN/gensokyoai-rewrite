"""插件 API —— 只有声明与装饰器，不依赖内核任何实现。

插件这样写（一个 .py 就够）：

    from gensokyoai.api import scene, tool

    @scene(name="qq", prompt="你在一个 QQ 群里。", bubbles={"max": 3, "gap_s": 0.4})
    class QQScene:
        def __init__(self, ctx):
            self.ctx = ctx          # ctx.mouth / ctx.name / ctx.room / ctx.log

        @tool("read_member", "读一个人的群名片", {"user_id": "string"})
        async def read_member(self, user_id):
            return [{"name": "帕秋莉", "text": "红魔馆的魔法使"}]

        def describe(self, event):
            return event            # 可选：把事件改写成你想让模型看到的形状

        async def say(self, segments, reply_to=None):
            await self.ctx.mouth.send(self.ctx.name, "".join(
                s["text"] for s in segments if s["type"] == "text"))

库启动时扫描并挂载；插件不需要 import 内核，只需要 import 这个模块。
"""

import inspect
from collections.abc import Callable
from typing import Protocol

API_VERSION = "1"

_SCENES: dict[str, type] = {}
""" 已注册的场景类（装饰器在 import 时填进来） """

_ALL_ACTIONS = ("say", "wait", "read_events")
""" 内核提供的动作；场景可以只挑其中几个 """

_WAKE_DEFAULTS = {"on_mention": 1.0, "on_others": 1.0, "cooldown_s": 0.0}
""" 唤醒策略默认值：点名必醒、别的消息也必醒、无冷却（= 不节流） """

_ON_PROSE = ("drop", "send")
""" 模型只写了正文、没调工具时怎么办：丢掉 / 当作一句话发出去 """


class SceneContext(Protocol):
    """库交给场景实例的东西（每个房间一份）。"""

    mouth: object
    """ 投递出口（`await mouth.send(name, text)`） """
    name: str
    """ 角色名 """
    room: str
    """ 房间名 """
    log: object
    """ 这个场景的 logger """


def scene(
    *,
    name: str,
    prompt: str = "",
    bubbles: dict | None = None,
    actions: tuple[str, ...] = ("say",),
    wake: dict | None = None,
    on_prose: str = "drop",
) -> Callable[[type], type]:
    """把一个类注册成场景。

    Args:
        name: 场景名（`--scene` 用它选）
        prompt: 只给这个场景的名词与规矩（"你在一个 QQ 群里。"）
        bubbles: 连发策略 `{"max": 几条, "gap_s": 间隔秒}`
        actions: 启用哪些内核动作（say / wait / read_events）
        wake: 唤醒策略 `{"on_mention": 概率, "on_others": 概率, "cooldown_s": 秒}`
            —— 消息一律入房，只是按这个决定要不要因此开一轮
    """

    def wrap(cls: type) -> type:
        existing = _SCENES.get(name)
        if existing is not None and existing.__module__ != cls.__module__:
            raise ValueError(
                f"场景重名: {name!r}（{existing.__module__} 与 {cls.__module__}）"
            )
        cls._gensokyo_scene = {  # type: ignore[attr-defined]
            "name": name,
            "prompt": prompt,
            "bubbles": bubbles or {},
            "actions": _clean_actions(actions),
            "wake": wake or {},
            "on_prose": on_prose if on_prose in _ON_PROSE else "drop",
        }
        _SCENES[name] = cls
        return cls

    return wrap


def tool(name: str = "", description: str = "", params: dict | None = None) -> Callable:
    """把一个方法注册成场景工具。

    Args:
        name: 工具名（默认取函数名）
        description: 给模型看的说明（默认取 docstring）
        params: 参数表 `{"参数名": "string" | "integer" | "number" | "boolean"}`
    """

    def wrap(fn: Callable) -> Callable:
        fn._gensokyo_tool = {  # type: ignore[attr-defined]
            "name": name or fn.__name__,
            "description": description or (fn.__doc__ or "").strip(),
            "params": dict(params or {}),
        }
        return fn

    return wrap


def scenes() -> dict[str, type]:
    """当前已注册的场景（名字 -> 类）。"""
    return dict(_SCENES)


def drop_module(module_name: str) -> None:
    """卸掉某个模块注册的场景（版本不匹配时用）。"""
    for name in [n for n, cls in _SCENES.items() if cls.__module__ == module_name]:
        del _SCENES[name]


def spec_of(scene_object) -> dict:
    """取场景的声明（实例或类都行），缺项补默认值。"""
    raw = getattr(scene_object, "_gensokyo_scene", None)
    if raw is None:
        raw = getattr(type(scene_object), "_gensokyo_scene", {})
    return {
        "name": raw.get("name", ""),
        "prompt": raw.get("prompt", ""),
        "bubbles": {"max": 1, "gap_s": 0.0, **raw.get("bubbles", {})},
        "actions": _clean_actions(raw.get("actions", ())),
        "wake": {**_WAKE_DEFAULTS, **raw.get("wake", {})},
        "on_prose": raw.get("on_prose", "drop"),
    }


def tools_of(scene_object) -> list[dict]:
    """场景声明的工具元信息（不含内核动作）。

    沿继承链收集：`WebTools` 这类混入声明的工具，场景继承下来也算。子类覆盖同名
    方法时以子类为准。
    """
    cls = scene_object if inspect.isclass(scene_object) else type(scene_object)
    found: dict[str, dict] = {}
    for klass in reversed(cls.__mro__):
        if klass is object:
            continue
        for value in vars(klass).values():
            meta = getattr(value, "_gensokyo_tool", None)
            if meta is not None:
                found[meta["name"]] = meta
    return list(found.values())


def schema_of(meta: dict) -> dict:
    """把工具元信息转成 OpenAI 的工具声明。"""
    types = {
        "string": "string",
        "text": "string",
        "int": "integer",
        "integer": "integer",
        "number": "number",
        "bool": "boolean",
        "boolean": "boolean",
    }
    props = {
        key: {"type": types.get(kind, "string")} for key, kind in meta["params"].items()
    }
    return {
        "type": "function",
        "function": {
            "name": meta["name"],
            "description": meta["description"],
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(props),
            },
        },
    }


async def call_tool(scene_object, name: str, args: dict) -> list[dict]:
    """调用场景工具；场景没声明就返回空列表。

    Args:
        scene_object: 场景实例
        name: 工具名
        args: 模型给的参数

    Returns:
        list[dict]: 要回填给模型的结果（形状同事件）
    """
    method = getattr(scene_object, name, None)
    if method is None or getattr(method, "_gensokyo_tool", None) is None:
        return []
    params = inspect.signature(method).parameters
    usable = {key: value for key, value in args.items() if key in params}
    result = method(**usable)
    if inspect.isawaitable(result):
        result = await result
    return list(result or ())


def _clean_actions(actions) -> tuple[str, ...]:
    wanted = tuple(a for a in actions if a in _ALL_ACTIONS)
    return wanted or ("say",)


__all__ = [
    "API_VERSION",
    "SceneContext",
    "call_tool",
    "drop_module",
    "scene",
    "scenes",
    "schema_of",
    "spec_of",
    "tool",
    "tools_of",
]
