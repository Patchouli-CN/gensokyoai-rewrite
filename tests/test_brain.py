"""Brain 决策层单元测试：档位路由 / 快速路径 / 模型结论 / OOC 快筛"""

import pytest

from gensokyoai.core.brain.engine import BrainEngine, route
from gensokyoai.core.brain.ooc_detector import OOCDetector
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.schemas.brain_schema import BrainThinkEffort
from gensokyoai.schemas.memory_schema import MemoryItem
from gensokyoai.schemas.model_schema import CompletionResult, Message
from gensokyoai.schemas.scene_schema import SceneSnapshot


class FakeBackend:
    """返回预设内容的假模型后端"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[Message]] = []

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        self.calls.append(list(messages))
        return CompletionResult(content=self.content)


def _snapshot(text: str = "你好啊", **kw) -> SceneSnapshot:
    return SceneSnapshot(sender="测试员", content=text, **kw)


def test_route_greeting_is_low():
    """短寒暄走 LOW"""
    assert route(_snapshot("你好啊")) is BrainThinkEffort.LOW


def test_route_philosophy_is_high():
    """世界观提问走 HIGH（对齐架构文档 §9.2 示例）"""
    assert route(_snapshot("你觉得这个世界的本质是什么？")) is BrainThinkEffort.HIGH


def test_route_empty_is_off():
    """空内容走 OFF"""
    assert route(_snapshot("")) is BrainThinkEffort.OFF


async def test_think_off_uses_fast_path_without_model_call():
    """OFF 档零模型调用，走快速路径透传"""
    backend = FakeBackend('{"intent": "x"}')
    engine = BrainEngine(SessionManager(backend))
    conclusion = await engine.think(_snapshot("你好啊"), [], BrainThinkEffort.OFF)
    assert backend.calls == []
    assert conclusion.verdict == "pass_through"
    assert conclusion.intent == "回应打招呼"


async def test_think_parses_model_json_conclusion():
    """模型输出 JSON 时正确产出带初稿的结论"""
    backend = FakeBackend(
        '{"intent": "问路", "emotion": "好奇", "draft": "你要去哪？", "confidence": 0.8}'
    )
    engine = BrainEngine(SessionManager(backend))
    conclusion = await engine.think(_snapshot("怎么去红魔馆？"), [], BrainThinkEffort.MID)
    assert conclusion.verdict == "draft"
    assert conclusion.intent == "问路"
    assert conclusion.draft == "你要去哪？"
    # 无状态调用：不落会话
    assert engine._sessions.usage("brain.think") == 0


async def test_think_downgrades_on_bad_json():
    """模型输出无法解析时降级为快速路径"""
    backend = FakeBackend("这不是JSON")
    engine = BrainEngine(SessionManager(backend))
    conclusion = await engine.think(_snapshot("随便聊聊"), [], BrainThinkEffort.LOW)
    assert conclusion.verdict == "pass_through"
    assert conclusion.effort is BrainThinkEffort.LOW


async def test_think_ooc_filter_drops_draft():
    """初稿命中 OOC 规则时丢弃初稿并打标"""
    backend = FakeBackend(
        '{"intent": "闲聊", "emotion": "平淡", "draft": "作为一个AI语言模型我无法回答", "confidence": 0.9}'
    )
    engine = BrainEngine(
        SessionManager(backend), persona="测试人设", ooc=OOCDetector(SessionManager(FakeBackend("{}"))),
    )
    conclusion = await engine.think(_snapshot("讲个故事"), [], BrainThinkEffort.LOW)
    assert conclusion.ooc_flag is True
    assert conclusion.draft is None
    assert conclusion.verdict == "pass_through"


async def test_ooc_pre_filter_patterns():
    """OOC 规则快筛命中与放行"""
    detector = OOCDetector(SessionManager(FakeBackend("{}")))
    assert detector.pre_filter("作为一个AI, 我没有感情").is_ooc is True
    assert detector.pre_filter("哟，又来红魔馆蹭饭？").is_ooc is False


async def test_memory_refs_passed_through_conclusion():
    """记忆条目随结论回传（memory_refs）"""
    backend = FakeBackend('{"intent": "问路", "emotion": "好奇", "draft": "去哪？"}')
    engine = BrainEngine(SessionManager(backend))
    item = MemoryItem(topic="对话", content="她住在雾之湖")
    conclusion = await engine.think(_snapshot("魔理沙住哪？"), [item], BrainThinkEffort.LOW)
    assert item in conclusion.memory_refs
