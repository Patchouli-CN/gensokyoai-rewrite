"""宿主装配：默认场景与第三方构造器都要能创建首间房。"""

from gensokyoai.__main__ import _loop_factory
from gensokyoai.api import scene
from gensokyoai.mind import Mind
from gensokyoai.scenes.chat import ChatScene


class _Model:
    capabilities = frozenset()
    context = 1000


class _Mouth:
    async def send(self, name: str, text: str) -> None:
        pass


def _mind() -> Mind:
    return Mind(_Model(), "觉", "人设")


def test_factory_builds_default_chat_scene():
    """默认 chat 不声明工具参数，也应由真实工厂正常装配"""
    loop = _loop_factory(_mind(), lambda _room: _Mouth(), None, scene_cls=ChatScene)(
        "console"
    )

    assert isinstance(loop._scene, ChatScene)
    assert loop._scene.ctx.room == "console"


def test_factory_supports_context_only_plugin():
    """README 所示的单 ctx 构造器不多收 tools_config"""

    @scene(name="t_context_only")
    class ContextOnly:
        def __init__(self, ctx) -> None:
            self.ctx = ctx

    loop = _loop_factory(_mind(), lambda _room: _Mouth(), None, scene_cls=ContextOnly)(
        "g1"
    )

    assert isinstance(loop._scene, ContextOnly)


def test_factory_passes_tools_config_when_supported():
    """声明 tools_config 或 **kwargs 的插件仍能收到站点配置"""

    @scene(name="t_configured")
    class Configured:
        def __init__(self, ctx, *, tools_config=None) -> None:
            self.ctx = ctx
            self.tools_config = tools_config

    @scene(name="t_kwargs")
    class Kwargs:
        def __init__(self, ctx, **kwargs) -> None:
            self.ctx = ctx
            self.tools_config = kwargs.get("tools_config")

    configured = _loop_factory(
        _mind(),
        lambda _room: _Mouth(),
        None,
        scene_cls=Configured,
        tools_config="config/tools",
    )("g1")._scene
    kwargs = _loop_factory(
        _mind(),
        lambda _room: _Mouth(),
        None,
        scene_cls=Kwargs,
        tools_config="config/tools",
    )("g1")._scene

    assert configured.tools_config == "config/tools"
    assert kwargs.tools_config == "config/tools"
