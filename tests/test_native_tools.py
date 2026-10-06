"""原生 tool_calls 优先 / 文本喊话备胎：启发式探测、配置短路、接力统一执行"""

from gensokyoai.core.brain.engine import BrainEngine
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.models.llama_cpp import LlamaProvider
from gensokyoai.prompts.manager import prompt_mgr
from gensokyoai.schemas.brain_schema import BrainThinkEffort
from gensokyoai.schemas.model_schema import (
    CompletionResult,
    Message,
    ModelConfig,
    ToolCall,
    ToolSpec,
)
from gensokyoai.schemas.scene_schema import SceneSnapshot


def _echo(text: str) -> str:
    """测试工具：原样回显"""
    return f"ECHO:{text}"


_ECHO_SPEC = ToolSpec(tool_func=_echo, name="echo_tool")


def _think_json(action_hint: str = "照实说", need_continue: bool = False) -> str:
    return (
        '{"thought": "分析完毕", "intent": "回应", "emotion": "平静", '
        f'"action_hint": "{action_hint}", "confidence": 0.9, '
        f'"need_continue_think": {"true" if need_continue else "false"}}}'
    )


class _StubProvider(LlamaProvider):
    """不发真实 HTTP，按序返回预设响应并记录请求体"""

    def __init__(self, responses: list[dict]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.payloads: list[dict] = []

    async def _post_json(self, http, url, payload, headers, started) -> dict:
        self.payloads.append(payload)
        return self._responses.pop(0)


def _tool_call_response(name: str, args: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _text_response(text: str) -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


# ---------- 探测 ----------


async def test_probe_short_circuit_by_config():
    """配置 tool_support 显式指定时跳过探测（不发任何请求）"""
    provider = _StubProvider([])  # 空响应表：真发请求会直接炸
    provider.config(ModelConfig(tool_support=True))
    assert await provider.probe_native_tools() is True

    provider = _StubProvider([])
    provider.config(ModelConfig(tool_support=False))
    assert await provider.probe_native_tools() is False


async def test_probe_detects_and_caches():
    """探测发一次最小请求识别原生支持，结果按实例缓存"""
    provider = _StubProvider([_tool_call_response("noop", "{}")])
    provider.config(ModelConfig())
    assert await provider.probe_native_tools() is True
    assert await provider.probe_native_tools() is True, "应读缓存"
    assert len(provider.payloads) == 1, "探测只应发一次请求"
    assert provider.payloads[0]["tool_choice"] == "required"


async def test_probe_failure_falls_back_to_shout():
    """探测请求失败按不支持处理（文本喊话备胎，不炸启动）"""

    class _BoomProvider(LlamaProvider):
        async def _post_json(self, http, url, payload, headers, started):
            raise ConnectionError("server has no --jinja")

    provider = _BoomProvider()
    provider.config(ModelConfig())
    assert await provider.probe_native_tools() is False


async def test_manager_native_tools_supported_duck_backend():
    """鸭子类型后端（无探测方法）按不支持处理，与旧行为一致"""

    class _PlainBackend:
        async def chat(self, messages, **kw):
            return CompletionResult(content="x")

    sm = SessionManager()
    sm.set_default_backend(_PlainBackend())
    assert await sm.native_tools_supported("brain.think") is False


# ---------- Provider：execute_tools 开关 ----------


async def test_execute_tools_false_returns_raw_calls():
    """execute_tools=False：原生 tool_calls 不进内循环，原样返回给调用方"""
    provider = _StubProvider([_tool_call_response("echo_tool", '{"text": "hi"}')])
    provider.config(ModelConfig())

    result = await provider.chat(
        [Message(role="user", content="x")],
        tools=[_ECHO_SPEC],
        execute_tools=False,
    )
    assert result.tool_calls, "原生调用应原样返回"
    assert result.tool_calls[0].name == "echo_tool"
    assert len(provider.payloads) == 1, "不应有内循环的第二次请求"


async def test_execute_tools_default_inner_loop_unchanged():
    """默认行为不变：原生调用由 Provider 内循环自行消化"""
    provider = _StubProvider(
        [
            _tool_call_response("echo_tool", '{"text": "hi"}'),
            _text_response("结果是 ECHO:hi"),
        ]
    )
    provider.config(ModelConfig())

    result = await provider.chat([Message(role="user", content="x")], tools=[_ECHO_SPEC])
    assert not result.tool_calls
    assert result.content == "结果是 ECHO:hi"
    assert len(provider.payloads) == 2, "内循环应把工具结果回填后重取一次"


# ---------- 接力思考：原生轮统一执行 ----------


async def test_relay_native_tool_round_executed_by_brain():
    """原生模式接力：tool_calls 轮没有 JSON 正文也不算坏输出，
    脑层统一执行工具并按【工具执行结果】回填（与喊话同一条路径）"""
    scripted = [
        CompletionResult(
            content="",
            tool_calls=[ToolCall(id="c1", name="echo_tool", arguments='{"text": "hi"}')],
        ),
        CompletionResult(content=_think_json()),
    ]
    calls: list[dict] = []

    class _NativeBackend:
        async def probe_native_tools(self):
            return True

        async def chat(self, messages, **kw):
            calls.append({"kw": kw, "messages": list(messages)})
            return scripted[len(calls) - 1]

    sm = SessionManager()
    sm.set_default_backend(_NativeBackend())
    engine = BrainEngine(sm, tools=[_ECHO_SPEC])
    conclusion = await engine.think(
        SceneSnapshot(sender="测试员", content="用工具试试"), [], BrainThinkEffort.LOW
    )

    assert conclusion.verdict == "draft", "不应因空正文误判坏输出而降级"
    assert calls[0]["kw"].get("execute_tools") is False, "接力应要求 provider 别消化原生调用"
    tool_feed = "\n".join(m.content for m in calls[1]["messages"])
    assert "【工具执行结果】" in tool_feed and "ECHO:hi" in tool_feed, "工具结果应按接力格式回填"
    assert [len(calls)] == [2], "工具轮 + 收束轮，共两次调用"


async def test_relay_shout_mode_still_works_without_probe():
    """备胎模式不变：无探测方法的后端走文本喊话，喊话调用照常执行"""
    scripted = [
        CompletionResult(
            content='{"thought": "先调用 echo_tool 试试。调用 echo_tool {\\"text\\": \\"hey\\"}", '
            '"intent": "测试", "emotion": "好奇", "action_hint": "等结果", '
            '"confidence": 0.8, "need_continue_think": true}'
        ),
        CompletionResult(content=_think_json()),
    ]
    calls: list[list] = []

    class _ShoutBackend:
        def normalize_tool_calls(self, result, parsed):
            # 真机里这层由 LlamaProvider 提供；假后端借用同一个喊话提取器
            from gensokyoai.models.llama_cpp import extract_text_tool_calls

            calls = extract_text_tool_calls(str((parsed or {}).get("thought", "")))
            if calls:
                return CompletionResult(content=result.content, tool_calls=calls)
            return result

        async def chat(self, messages, **kw):
            calls.append(list(messages))
            return scripted[len(calls) - 1]

    sm = SessionManager()
    sm.set_default_backend(_ShoutBackend())
    engine = BrainEngine(sm, tools=[_ECHO_SPEC])
    conclusion = await engine.think(
        SceneSnapshot(sender="测试员", content="用工具试试"), [], BrainThinkEffort.LOW
    )

    assert conclusion.verdict == "draft"
    tool_feed = "\n".join(m.content for m in calls[1])
    assert "ECHO:hey" in tool_feed, "喊话调用应被执行并回填"


# ---------- Prompt 双版本 ----------


def test_brain_think_prompt_has_dual_tool_styles():
    """接力 prompt：原生版不教喊话，备胎版明文教喊话"""
    native = prompt_mgr.render("brain.think", tool_style="native")
    shout = prompt_mgr.render("brain.think", tool_style="shout")
    assert "调用 days_until" not in native, "原生版不应出现喊话示例"
    assert "tool_calls" in native
    assert "调用 days_until" in shout, "备胎版应保留喊话教学"


def test_step_tools_hint_dual_styles():
    """思考链步骤的工具提示：原生版只摊签名，备胎版教喊话"""
    native = prompt_mgr.render("think.step.tools_hint", tool_lines=["- x()"], tool_style="native")
    shout = prompt_mgr.render("think.step.tools_hint", tool_lines=["- x()"], tool_style="shout")
    assert "调用 days_until" not in native
    assert "调用 days_until" in shout


# ---------- Toolkit：缺参预检 ----------


async def test_toolkit_missing_required_param_skips_execution():
    """缺必填参数时不做必败执行，直接回自教学错误（省一次异常 + 白烧的思考轮）"""
    from gensokyoai.core.toolkit import build_executor

    invoked: list[str] = []

    def _needs_arg(target_date: str) -> str:
        """倒计时"""
        invoked.append(target_date)
        return "77"

    executor = build_executor([ToolSpec(tool_func=_needs_arg, name="days_until")])
    result = await executor.execute("days_until", "{}")
    assert not result.ok
    assert "缺少必填参数: target_date" in result.error
    assert "调用格式" in result.error
    assert not invoked, "缺参不应真执行工具"


async def test_toolkit_optional_param_not_blocked():
    """全是无参/带默认值的工具不受预检影响"""
    from gensokyoai.core.toolkit import build_executor

    def _no_arg() -> str:
        """无参工具"""
        return "ok"

    executor = build_executor([ToolSpec(tool_func=_no_arg, name="noop")])
    result = await executor.execute("noop", "{}")
    assert result.ok and result.content == "ok"


# ---------- 喊话提取：prose 参数抢救 ----------


def test_prose_param_salvage():
    """模型口头提及工具不带花括号时，从 prose 里抢救参数值（真机写作习惯）"""
    from gensokyoai.models.llama_cpp import extract_text_tool_calls

    calls = extract_text_tool_calls(
        '我现在要正确调用days_until工具，将target_date设置为"2026-12-22"来计算到冬至的天数'
    )
    assert len(calls) == 1
    assert calls[0].name == "days_until"
    assert calls[0].arguments == '{"target_date":"2026-12-22"}'


def test_prose_param_salvage_curly_quotes():
    """弯引号（模型爱用）也能抢救"""
    from gensokyoai.models.llama_cpp import extract_text_tool_calls

    calls = extract_text_tool_calls("使用days_until工具，target_date为“2026-12-22”即可")
    assert calls and calls[0].arguments == '{"target_date":"2026-12-22"}'


def test_prose_no_value_still_empty_args():
    """纯提及（连参数值都没有）仍然空参——交给 toolkit 缺参预检去教学"""
    from gensokyoai.models.llama_cpp import extract_text_tool_calls

    calls = extract_text_tool_calls("我可以使用days_until工具计算这个日期")
    assert calls and calls[0].arguments == "{}"


def test_prose_param_salvage_loose_wording():
    """参数名和值之间隔着废话也能抢救（真机原话：`target_date参数为"..."`）"""
    from gensokyoai.models.llama_cpp import extract_text_tool_calls

    calls = extract_text_tool_calls(
        '我需要重新调用days_until工具，正确传入target_date参数为"2026-12-22"。'
    )
    assert calls and calls[0].arguments == '{"target_date":"2026-12-22"}'

    calls = extract_text_tool_calls(
        "使用days_until工具获取天数。根据工具定义，target_date格式为YYYY-MM-DD，"
        '所以应该是"2026-12-22"。'
    )
    assert calls and calls[0].arguments == '{"target_date":"2026-12-22"}'


def test_prose_salvage_excludes_tool_name():
    """工具名本身不会被当成参数名（窗口起点就是它）"""
    from gensokyoai.models.llama_cpp import extract_text_tool_calls

    calls = extract_text_tool_calls('调用days_until工具，参数target_date为"2026-12-22"')
    assert calls
    assert "days_until" not in calls[0].arguments
    assert "target_date" in calls[0].arguments


# ---------- Toolkit：唯一必填参数的孤值抢救 ----------


def test_fill_single_param_lone_value():
    """「参数是 "2026-12-22"」——连参数名都不提时，唯一必填参数工具吃孤引号值"""
    from gensokyoai.core.toolkit import fill_single_param_calls

    def _needs_arg(target_date: str) -> str:
        """倒计时"""

    spec = ToolSpec(tool_func=_needs_arg, name="days_until")
    calls = [ToolCall(id="c1", name="days_until", arguments="{}")]
    filled = fill_single_param_calls(
        calls, [spec], '让我重新调用 days_until 工具，参数是 "2026-12-22"。'
    )
    assert filled[0].arguments == '{"target_date": "2026-12-22"}'


def test_fill_single_param_not_for_multi_param():
    """多必填参数的工具不吃孤值（没法判断对应关系，交给缺参预检教学）"""
    from gensokyoai.core.toolkit import fill_single_param_calls

    def _two_args(a: str, b: str) -> str:
        """双参"""

    spec = ToolSpec(tool_func=_two_args, name="two_tool")
    calls = [ToolCall(id="c1", name="two_tool", arguments="{}")]
    filled = fill_single_param_calls(calls, [spec], '参数是 "x"')
    assert filled[0].arguments == "{}"


def test_fill_single_param_no_value_keeps_empty():
    """文本里没有引号值时保持空参（走缺参预检的自教学路径）"""
    from gensokyoai.core.toolkit import fill_single_param_calls

    def _needs_arg(target_date: str) -> str:
        """倒计时"""

    spec = ToolSpec(tool_func=_needs_arg, name="days_until")
    calls = [ToolCall(id="c1", name="days_until", arguments="{}")]
    filled = fill_single_param_calls(calls, [spec], "我用 days_until 算一下")
    assert filled[0].arguments == "{}"


def test_prose_param_salvage_single_quotes():
    """单引号参数值也能抢救（真机 action_hint 原话：`target_date='2026-12-22'`）"""
    from gensokyoai.models.llama_cpp import extract_text_tool_calls

    calls = extract_text_tool_calls("调用 days_until 工具，参数 target_date='2026-12-22'")
    assert calls and calls[0].arguments == '{"target_date":"2026-12-22"}'


async def test_relay_lone_value_fill_uses_thought_not_json_keys():
    """孤值抢救的源文本是 thought/action_hint 字段值，不是原始 JSON——
    否则第一个引号匹配永远是字段名 "thought"（真机把 'thought' 填进过 target_date）"""
    scripted = [
        CompletionResult(
            content=(
                '{"thought": "目标是 \'hey\'，用 echo_tool 试。调用 echo_tool", '
                '"intent": "测试", "emotion": "平静", '
                '"action_hint": "", '
                '"confidence": 0.9, "need_continue_think": true}'
            )
        ),
        CompletionResult(content=_think_json()),
    ]
    calls: list[list] = []

    class _ShoutBackend:
        def normalize_tool_calls(self, result, parsed):
            from gensokyoai.models.llama_cpp import extract_text_tool_calls

            text = str((parsed or {}).get("thought", "")) + str(
                (parsed or {}).get("action_hint", "")
            )
            found = extract_text_tool_calls(text)
            if found:
                return CompletionResult(content=result.content, tool_calls=found)
            return result

        async def chat(self, messages, **kw):
            calls.append(list(messages))
            return scripted[len(calls) - 1]

    sm = SessionManager()
    sm.set_default_backend(_ShoutBackend())
    engine = BrainEngine(sm, tools=[_ECHO_SPEC])
    conclusion = await engine.think(
        SceneSnapshot(sender="测试员", content="算算日子"), [], BrainThinkEffort.LOW
    )

    assert conclusion.verdict == "draft"
    feed = "\n".join(m.content for m in calls[1])
    assert "ECHO:" in feed, "工具应被执行"
    assert "'thought'" not in feed, "字段名不应被当成参数值填进去"
