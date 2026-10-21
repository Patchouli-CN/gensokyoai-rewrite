"""适配层测试 —— 厂商无关的消息怎么翻译成各家的线上格式，以及计费。"""

from typing import ClassVar

from gensokyoai.llm import (
    AnthropicProvider,
    FlatBilling,
    Image,
    OpenAIProvider,
    Quota,
    Rates,
    Text,
    Turn,
    Usage,
    UsageLedger,
)


class _Resp:
    def __init__(self, payload) -> None:
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self):
        return self._payload


def _patch(monkeypatch, provider, payload):
    """把适配器的 HTTP 会话换成假的，记录 url / body / headers。"""
    sent: dict = {}

    class _Session:
        def post(self, url, json=None, headers=None):
            sent.update(url=url, body=json, headers=headers or {})
            return _Resp(payload)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(provider, "session", lambda: _Session())
    return sent


OPENAI_REPLY = {
    "model": "deepseek-chat",
    "choices": [
        {
            "message": {
                "content": " 在的。 ",
                "tool_calls": [
                    {
                        "function": {
                            "name": "send_message",
                            "arguments": '{"text": "在的。"}',
                        }
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {
        "prompt_tokens": 1200,
        "completion_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 200},
    },
}

ANTHROPIC_REPLY = {
    "model": "claude-sonnet-4",
    "content": [
        {"type": "text", "text": "在的。"},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "send_message",
            "input": {"text": "在的。"},
        },
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 900, "output_tokens": 40, "cache_read_input_tokens": 100},
}


async def test_openai_payload_carries_model_key_and_extra(monkeypatch):
    """云端端点要的三样：model 字段、Bearer、厂商专有参数"""
    provider = OpenAIProvider(
        "https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key="sk-test",
        extra={"top_p": 0.9},
    )
    sent = _patch(monkeypatch, provider, OPENAI_REPLY)

    completion = await provider.complete(
        [
            Turn(role="system", parts=(Text("人设"),)),
            Turn(role="user", parts=(Text("{}"),)),
        ],
        tools=[{"type": "function", "function": {"name": "send_message"}}],
        max_tokens=256,
    )

    body = sent["body"]
    assert sent["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert body["model"] == "deepseek-chat"
    assert body["top_p"] == 0.9, "厂商专有参数要能透传"
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert body["messages"][0] == {"role": "system", "content": "人设"}
    assert completion.text == "在的。"
    assert completion.calls[0].arguments == {"text": "在的。"}
    assert completion.usage.prompt_tokens == 1200
    assert completion.usage.cached_tokens == 200
    assert completion.finish_reason == "tool_calls"


async def test_openai_can_omit_vendor_specific_think_field(monkeypatch):
    """think=None：不发 chat_template_kwargs（云 API 可能不认这个字段）"""
    provider = OpenAIProvider("http://127.0.0.1:8080/v1", think=None)
    sent = _patch(monkeypatch, provider, OPENAI_REPLY)

    await provider.complete([Turn(role="user", parts=(Text("hi"),))])

    assert "chat_template_kwargs" not in sent["body"]
    assert "model" not in sent["body"], "llama-server 不需要模型名"

    provider.think_on = True
    other = OpenAIProvider("http://127.0.0.1:8080/v1", think=False)
    sent = _patch(monkeypatch, other, OPENAI_REPLY)
    await other.complete([Turn(role="user", parts=(Text("hi"),))])
    assert sent["body"]["chat_template_kwargs"]["thinking"] is False


async def test_openai_images_become_content_blocks(monkeypatch):
    """带图：content 变成数组，说明和图片块按顺序排"""
    provider = OpenAIProvider("http://127.0.0.1:8080/v1")
    sent = _patch(monkeypatch, provider, OPENAI_REPLY)

    await provider.complete(
        [
            Turn(
                role="user",
                parts=(
                    Text("看这张"),
                    Image(
                        url="data:image/png;base64,AA", label="帕秋莉 发的第 1 张图："
                    ),
                ),
            )
        ]
    )

    content = sent["body"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "看这张"}
    assert content[1]["text"].startswith("帕秋莉")
    assert content[2]["image_url"]["url"].startswith("data:image/png")


async def test_anthropic_payload_shape(monkeypatch):
    """Claude：system 单独给、工具用 input_schema、图片是 base64 块、回复是块数组"""
    provider = AnthropicProvider(api_key="sk-ant")
    sent = _patch(monkeypatch, provider, ANTHROPIC_REPLY)

    completion = await provider.complete(
        [
            Turn(role="system", parts=(Text("人设"),)),
            Turn(
                role="user",
                parts=(
                    Text("看这张"),
                    Image(url="data:image/png;base64,AA", label="图："),
                ),
            ),
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": "发一条",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        max_tokens=256,
        temperature=1.5,
    )

    body = sent["body"]
    assert sent["url"].endswith("/messages")
    assert sent["headers"]["x-api-key"] == "sk-ant"
    assert sent["headers"]["anthropic-version"]
    assert body["system"] == "人设"
    assert body["temperature"] == 1.0, "Claude 的温度上限是 1"
    assert body["tools"][0]["input_schema"]["type"] == "object"
    image = body["messages"][0]["content"][2]
    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/png"
    assert image["source"]["data"] == "AA"
    assert completion.text == "在的。"
    assert completion.calls[0].name == "send_message"
    assert completion.calls[0].arguments == {"text": "在的。"}
    assert completion.usage.prompt_tokens == 900


def test_vendor_tables_live_with_their_adapter():
    """价格表跟着适配器：OpenAI 的价不掺别家，最长前缀优先"""
    from gensokyoai.llm import anthropic, openai

    mini = openai.billing_for("gpt-4o-mini-2024")
    full = openai.billing_for("gpt-4o-2024")
    assert mini is not None and mini.rates.input == 0.15
    assert full is not None and full.rates.input == 2.50
    assert openai.billing_for("deepseek-flash") is None, "别家的价不该出现在这张表里"
    assert anthropic.billing_for("claude-sonnet-4") is not None
    assert anthropic.billing_for("unknown-model") is None, "不认识就不给价"


def test_cost_and_ledger():
    """花费按百万 token 算，账本累加"""
    billing = FlatBilling(Rates(input=3.00, output=15.00, cached=0.30))
    ledger = UsageLedger(billing)

    spent = ledger.record(
        Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, cached_tokens=0)
    )

    assert round(spent, 6) == 18.0
    assert round(ledger.cost, 6) == 18.0
    assert ledger.calls == 1
    assert "18.0000" in ledger.summary()


def test_cached_tokens_are_cheaper():
    """命中缓存的输入按缓存价算"""
    rates = Rates(input=1.00, output=0.0, cached=0.10)
    assert (
        round(rates.cost(Usage(prompt_tokens=1_000_000, cached_tokens=1_000_000)), 6)
        == 0.1
    )


def test_ledger_without_billing_records_tokens_only():
    """厂商不提供计费：只记 token，摘要里说明"""
    ledger = UsageLedger()

    ledger.record(Usage(prompt_tokens=100, completion_tokens=10))

    assert ledger.cost == 0.0
    assert "未提供计费" in ledger.summary()


class _Unused:
    """保持 ClassVar 导入不被 lint 判成多余。"""

    items: ClassVar[list[int]] = []


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def test_window_only_counts_recent_usage():
    """5h 窗口只算最近 5 小时：更早的不该混进来"""
    clock = _Clock()
    ledger = UsageLedger(quotas=(Quota(span_s=5 * 3600, tokens=100_000),), clock=clock)

    ledger.record(Usage(prompt_tokens=10_000, completion_tokens=0))
    clock.tick(4 * 3600)
    ledger.record(Usage(prompt_tokens=20_000, completion_tokens=0))

    assert ledger.window(5 * 3600)[0] == 30_000
    clock.tick(2 * 3600)  # 第一条出了 5h 窗口
    assert ledger.window(5 * 3600)[0] == 20_000
    assert ledger.prompt_tokens == 30_000, "累计值不受窗口影响"


def test_quota_status_shows_usage_against_limit():
    """窗口额度按用量给百分比（/doctor 用）"""
    clock = _Clock()
    ledger = UsageLedger(quotas=(Quota(span_s=7 * 86400, tokens=50_000),), clock=clock)

    ledger.record(Usage(prompt_tokens=40_000, completion_tokens=0))

    line = ledger.status()[0]
    assert "7d" in line
    assert "40000" in line and "80%" in line


def test_quota_warns_once_per_window():
    """过线只喊一次，别每次补全都刷屏"""
    clock = _Clock()
    ledger = UsageLedger(quotas=(Quota(span_s=5 * 3600, tokens=10_000),), clock=clock)

    ledger.record(Usage(prompt_tokens=9_000, completion_tokens=0))
    assert (5 * 3600, "high") in ledger._warned

    ledger._warned.clear()
    ledger.record(Usage(prompt_tokens=2_000, completion_tokens=0))
    assert (5 * 3600, "full") in ledger._warned


def test_usage_survives_restart(tmp_path):
    """用量流水落盘：重启后 7 天窗口接着算"""
    clock = _Clock()
    path = tmp_path / "usage.jsonl"
    quota = (Quota(span_s=7 * 86400, tokens=1_000_000),)

    first = UsageLedger(quotas=quota, path=path, clock=clock)
    first.record(Usage(prompt_tokens=1_000, completion_tokens=500), "deepseek-chat")

    second = UsageLedger(quotas=quota, path=path, clock=clock)
    assert second.window(7 * 86400)[0] == 1_500

    clock.tick(8 * 86400)
    third = UsageLedger(quotas=quota, path=path, clock=clock)
    assert third.window(7 * 86400)[0] == 0, "超出窗口的老流水要丢掉"


def test_broken_usage_lines_are_skipped(tmp_path):
    """流水里混进坏行不该让账本起不来"""
    path = tmp_path / "usage.jsonl"
    path.write_text('{"ts": 1000000, "prompt": 10, "completion": 5}\n坏行\n', "utf-8")
    clock = _Clock()

    ledger = UsageLedger(path=path, clock=clock)

    assert ledger.window(7 * 86400)[0] == 15


async def test_reasoning_tokens_are_parsed(monkeypatch):
    """思考 token 单独拆出来（它算在 completion 里，但值得看见）"""
    payload = {
        "choices": [
            {
                "message": {"content": "2"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 38,
            "completion_tokens_details": {"reasoning_tokens": 36},
        },
    }
    provider = OpenAIProvider("https://api.deepseek.com/v1", model="deepseek-flash")
    _patch(monkeypatch, provider, payload)

    completion = await provider.complete([Turn(role="user", parts=(Text("1+1"),))])

    assert completion.usage.completion_tokens == 38
    assert completion.usage.reasoning_tokens == 36


def test_summary_shows_reasoning_when_present():
    """账本摘要里能看到思考花了多少"""
    ledger = UsageLedger()

    ledger.record(Usage(prompt_tokens=10, completion_tokens=40, reasoning_tokens=36))
    assert "思考 36" in ledger.summary()

    plain = UsageLedger()
    plain.record(Usage(prompt_tokens=10, completion_tokens=5))
    assert "思考" not in plain.summary()
