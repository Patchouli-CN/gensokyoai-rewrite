"""配置文件测试 —— 默认值、窗口写法、构造模型与账本。"""

import pytest

from gensokyoai.settings import Settings, load_settings, parse_span


def test_missing_file_falls_back_to_local_defaults(tmp_path):
    """没有配置文件：本地 llama-server，不记钱"""
    settings = load_settings(tmp_path / "nope.yaml")

    assert settings.model.base_url.endswith("8080/v1")
    assert settings.model.name == ""
    assert settings.prices().input == 0.0, "没配单价、模型名不在内置表里 = 不算钱"
    assert settings.quotas() == ()


def test_spans_parse_to_seconds():
    """5h / 7d / 90m / 2w"""
    assert parse_span("5h") == 5 * 3600
    assert parse_span("7d") == 7 * 86400
    assert parse_span("90m") == 90 * 60
    assert parse_span("2w") == 2 * 86400 * 7


def test_bad_span_is_rejected():
    """写法错了要当场报，别算成 0 秒"""
    for bad in ("5", "5x", "h", "0h", ""):
        with pytest.raises(ValueError):
            parse_span(bad)


def test_yaml_settings_round_trip(tmp_path):
    """完整配置：模型、单价、窗口、流水路径"""
    path = tmp_path / "settings.yaml"
    path.write_text(
        """
model:
  provider: anthropic
  base_url: https://api.anthropic.com/v1
  name: claude-sonnet-4-20250514
  api_key: sk-ant-test
  think: null
  timeout: 90
billing:
  price:
    input: 3.0
    output: 15.0
  quotas:
    - span: 5h
      tokens: 200000
    - span: 7d
      tokens: 1000000
  usage: data/usage.jsonl
""",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.model.provider == "anthropic"
    assert settings.model.api_key == "sk-ant-test"
    assert settings.model.think is None, "null = 不发厂商专有字段"
    assert settings.model.timeout == 90
    assert settings.prices().output == 15.0
    quotas = settings.quotas()
    assert [quota.label for quota in quotas] == ["5h", "7d"]
    assert quotas[1].tokens == 1_000_000


def test_zero_quota_entries_are_ignored():
    """额度和钱都为 0 的条目不算限额"""
    settings = load_settings(path=None)
    assert settings.quotas() == ()


def test_ledger_path_is_made_absolute_against_base_dir(tmp_path):
    """相对路径按给定的基准目录解析（两个宿主的 cwd 不一样）"""
    path = tmp_path / "settings.yaml"
    path.write_text(
        "billing:\n"
        "  usage: data/usage.jsonl\n"
        "  quotas:\n"
        "    - span: 5h\n"
        "      tokens: 100\n",
        encoding="utf-8",
    )
    settings = load_settings(path)

    ledger = settings.build_ledger(base_dir=tmp_path)
    ledger.record(_usage(50))

    assert (tmp_path / "data" / "usage.jsonl").exists()


def _usage(tokens: int):
    from gensokyoai.llm import Usage

    return Usage(prompt_tokens=tokens)


def test_model_name_lookup_when_price_absent(tmp_path):
    """文件里没写单价，但模型名在内置表里 -> 按内置价算"""
    path = tmp_path / "settings.yaml"
    path.write_text("model:\n  name: deepseek-chat\n", encoding="utf-8")

    settings = load_settings(path)

    assert settings.prices().input == 0.27
    assert isinstance(settings, Settings)
