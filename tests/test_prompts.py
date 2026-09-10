"""PromptManager 单元测试：注册 / 渲染 / 缓存 / 缺名报错"""

import pytest

from gensokyoai.prompts import PromptManager, prompt_mgr


def test_builtin_templates_registered():
    """业务模板在导入 manager 时即已注册（数据式/原生式均可）"""
    for name in (
        "brain.think",
        "brain.think.user",
        "ooc.audit",
        "ooc.audit.user",
        "memory.compress",
        "responder.user",
    ):
        assert prompt_mgr.raw(name) is not None, f"模板 {name} 未注册"


def test_render_substitutes_placeholders():
    """$var 占位符正确替换"""
    text = prompt_mgr.render(
        "brain.think.user",
        persona="测试人设",
        sender="小明",
        content="你好",
        context="无上下文",
        memory="无记忆",
    )
    assert "测试人设" in text
    assert "小明: 你好" in text
    assert "$persona" not in text and "$sender" not in text


def test_render_static_template_is_cached():
    """无参数模板走缓存，重复渲染返回同一对象"""
    first = prompt_mgr.render("brain.think")
    again = prompt_mgr.render("brain.think")
    assert first is again


def test_render_json_braces_untouched():
    """JSON 示例的单花括号不被 string.Template 破坏"""
    text = prompt_mgr.render("brain.think")
    assert '{"thought": "当前回合的思考内容"' in text


def test_missing_template_raises():
    """未注册的模板名报 KeyError"""
    with pytest.raises(KeyError):
        prompt_mgr.render("不存在的模板")


def test_duplicate_registration_raises():
    """同名模板重复注册报 RuntimeError"""
    mgr = PromptManager()

    @mgr.prompt("dup")
    def _first() -> str:
        return "一"

    with pytest.raises(RuntimeError):

        @mgr.prompt("dup")
        def _second() -> str:
            return "二"


def test_manager_instances_share_registry():
    """注册表挂在类上，多实例共享已注册模板"""
    mgr2 = PromptManager(enable_cache=False)
    assert mgr2.render("memory.compress") == prompt_mgr.render("memory.compress")


def test_native_renderer_supports_loop_and_condition():
    """原生式模板：条件/循环直接在 Python 里写，不依赖模板语法"""
    mgr = PromptManager()

    @mgr.prompt("native.demo")
    def native_demo(items, emphasize=False, **_) -> str:
        body = "\n".join(f"- {i}" for i in items)
        return f"{'★ 重要 ★\n' if emphasize else ''}{body}"

    out = mgr.render("native.demo", items=["a", "b", "c"], emphasize=True)
    assert "★ 重要 ★" in out
    assert "- a" in out and "- b" in out and "- c" in out
    # 强调关闭时无标记
    assert "★" not in mgr.render("native.demo", items=["a"], emphasize=False)
