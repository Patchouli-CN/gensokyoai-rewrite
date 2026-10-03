"""限流窗口快照测试：两族响应头解析 / 注册表存取 / 无头静默"""

from gensokyoai.utils.ratelimit import (
    RATE_LIMITS,
    RateLimitRegistry,
    RateLimitWindow,
    parse_rate_limit_headers,
)

NOW = 1_800_000_000.0


def test_parse_anthropic_headers():
    """Anthropic 族：requests/tokens 两维 + RFC3339 重置时间戳"""
    headers = {
        "anthropic-ratelimit-requests-limit": "100",
        "anthropic-ratelimit-requests-remaining": "63",
        "anthropic-ratelimit-requests-reset": "2026-10-03T23:40:00Z",
        "anthropic-ratelimit-tokens-limit": "200000",
        "anthropic-ratelimit-tokens-remaining": "82000",
    }
    windows = parse_rate_limit_headers(headers, now=NOW)

    req = windows["requests"]
    assert req.limit == 100
    assert req.remaining == 63
    assert req.remaining_ratio == 0.63
    assert req.reset_at is not None
    tok = windows["tokens"]
    assert tok.remaining_ratio == 0.41
    assert tok.reset_at is None, "没给 reset 的维度为 None"


def test_parse_openai_headers():
    """OpenAI 族：时长串重置（"6m0s"）折算成绝对时刻"""
    headers = {
        "x-ratelimit-limit-requests": "500",
        "x-ratelimit-remaining-requests": "120",
        "x-ratelimit-reset-requests": "6m0s",
    }
    windows = parse_rate_limit_headers(headers, now=NOW)

    req = windows["requests"]
    assert req.remaining_ratio == 120 / 500
    assert req.reset_at == NOW + 360.0


def test_parse_openai_duration_units():
    """时长串各分量：ms/s/m/h 混合"""
    headers = {"x-ratelimit-remaining-tokens": "7", "x-ratelimit-reset-tokens": "1h30m"}
    windows = parse_rate_limit_headers(headers, now=NOW)
    assert windows["tokens"].reset_at == NOW + 5400.0


def test_parse_no_headers_returns_empty():
    """本地 llama-server / 普通响应：无限流头，静默空 dict"""
    assert parse_rate_limit_headers({"content-type": "application/json"}, now=NOW) == {}


def test_window_ratio_guards_zero_limit():
    """limit 为 0（只给了 remaining）：比例给 None 而不是除零"""
    window = RateLimitWindow(limit=0, remaining=42)
    assert window.remaining_ratio is None


def test_registry_note_and_snapshot():
    """注册表：同一供应商多次采集取最新；快照是副本"""
    registry = RateLimitRegistry(clock=lambda: NOW)
    registry.note(
        "https://api.example.com/v1",
        {"x-ratelimit-limit-requests": "100", "x-ratelimit-remaining-requests": "80"},
    )
    registry.note(
        "https://api.example.com/v1",
        {"x-ratelimit-limit-requests": "100", "x-ratelimit-remaining-requests": "30"},
    )
    registry.note("http://127.0.0.1:8080/v1", {"content-type": "application/json"})

    snapshot = registry.snapshot()
    assert set(snapshot) == {"https://api.example.com/v1"}, "无头供应商不入账"
    assert snapshot["https://api.example.com/v1"].windows["requests"].remaining == 30
    assert snapshot["https://api.example.com/v1"].updated_at == NOW


def test_registry_reset():
    """reset 清空（测试隔离）"""
    registry = RateLimitRegistry(clock=lambda: NOW)
    registry.note("https://a.b/v1", {"x-ratelimit-remaining-requests": "1"})
    registry.reset()
    assert registry.snapshot() == {}


def test_module_singleton_isolated():
    """模块级单例：用前用后都 reset，不污染其他测试"""
    RATE_LIMITS.reset()
    RATE_LIMITS.note("https://a.b/v1", {"x-ratelimit-remaining-requests": "5"})
    assert RATE_LIMITS.snapshot()["https://a.b/v1"].windows["requests"].remaining == 5
    RATE_LIMITS.reset()
