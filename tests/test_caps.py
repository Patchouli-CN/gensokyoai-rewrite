"""模型能力测试 —— 名字猜测、端点自报、看图降级、探测覆盖声明。"""

from gensokyoai.llm import Completion, Usage, caps
from gensokyoai.llm.caps import detect_by_name, from_models_payload, normalize
from gensokyoai.model import ChatModel


def test_name_table_covers_common_models():
    """按名字猜：纯文本 / 图文 / 语音"""
    assert detect_by_name("deepseek-chat") == {"text"}
    assert detect_by_name("gpt-4o-mini") == {"text", "image"}
    assert detect_by_name("claude-sonnet-4-20250514") == {"text", "image"}
    assert detect_by_name("qwen2.5-vl-7b-instruct") == {"text", "image"}
    assert detect_by_name("whisper-1") == {"audio"}
    assert detect_by_name("") is None, "没名字就别乱猜"
    assert detect_by_name("some-unknown-model") is None


def test_endpoint_self_report_wins():
    """端点自报 capabilities（llama.cpp 这类）"""
    payload = {
        "data": [
            {"id": "other-model", "capabilities": ["completion"]},
            {"id": "qwen3.6-35b", "capabilities": ["completion", "multimodal"]},
        ]
    }

    assert from_models_payload(payload, "qwen3.6-35b") == {"text", "image"}
    assert from_models_payload(payload, "") == {"text"}, "没指定模型就取第一个能认的"
    assert from_models_payload({"data": []}, "x") is None


def test_normalize_drops_unknown_names():
    """配置里写了不认识的能力名：忽略，别炸"""
    assert normalize(["TEXT", "video", "bogus"]) == {"text", "video"}
    assert normalize([]) == {"text"}, "空声明按纯文本处理"
    assert normalize(None) == {"text"}


async def test_images_are_dropped_when_model_cannot_see(monkeypatch):
    """不支持看图的模型：图片降级成正文标记，不发多模态块"""
    model = ChatModel("http://127.0.0.1:8080/v1", capabilities=("text",))
    captured = {}

    async def fake_complete(turns, *, tools=None, max_tokens=0, temperature=0.0):
        captured["turns"] = turns
        return Completion(text="嗯", usage=Usage(prompt_tokens=1))

    monkeypatch.setattr(model._provider, "complete", fake_complete)

    await model.chat("sys", "{}", images=[("图：", "data:image/png;base64,AA")])

    user_turn = captured["turns"][1]
    assert [type(part).__name__ for part in user_turn.parts] == ["Text"]


async def test_images_are_sent_when_model_can_see(monkeypatch):
    """能看图的模型：图片作为 Image 块跟在文字后"""
    model = ChatModel("http://127.0.0.1:8080/v1")  # 默认 text + image
    captured = {}

    async def fake_complete(turns, *, tools=None, max_tokens=0, temperature=0.0):
        captured["turns"] = turns
        return Completion(text="红的", usage=Usage())

    monkeypatch.setattr(model._provider, "complete", fake_complete)

    await model.chat("sys", "{}", images=[("图：", "data:image/png;base64,AA")])

    kinds = [type(part).__name__ for part in captured["turns"][1].parts]
    assert kinds == ["Text", "Image"]


async def test_refresh_overrides_declared_capabilities(monkeypatch):
    """探测到端点自报的能力时，覆盖配置里的声明"""
    model = ChatModel("http://127.0.0.1:8080/v1", capabilities=("text",))

    async def fake_detect(base_url, **kwargs):
        assert base_url.endswith("8080/v1")
        return frozenset({"text", "image"})

    monkeypatch.setattr("gensokyoai.model.detect", fake_detect)

    assert await model.refresh_capabilities() == {"text", "image"}
    assert model.capabilities == {"text", "image"}


async def test_refresh_keeps_declaration_when_unknown(monkeypatch):
    """探测失败就保持声明，不要变成"什么都不能" """

    async def fake_detect(base_url, **kwargs):
        return None

    monkeypatch.setattr("gensokyoai.model.detect", fake_detect)
    model = ChatModel("http://127.0.0.1:8080/v1", capabilities=("text", "image"))

    assert await model.refresh_capabilities() == {"text", "image"}


async def test_detect_falls_back_to_name_when_endpoint_down(monkeypatch):
    """端点连不上：退回按名字猜"""

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise OSError("连不上")

    monkeypatch.setattr(caps.aiohttp, "ClientSession", _Boom)

    assert await caps.detect("http://127.0.0.1:9/v1", model="gpt-4o-mini") == {
        "text",
        "image",
    }
