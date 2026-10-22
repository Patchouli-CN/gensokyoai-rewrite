"""示例配置必须覆盖 Settings 的全部字段（加字段忘了更新示例就会红）。"""

import msgspec
import yaml

from gensokyoai.settings import Settings

EXAMPLE = "config/settings.example.yaml"


def _fields(cls) -> set[str]:
    return {field.name for field in msgspec.structs.fields(cls)}


def _load_example() -> dict:
    with open(EXAMPLE, encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def test_example_covers_all_sections():
    """顶层字段一个不落"""
    example = _load_example()

    missing = _fields(Settings) - set(example)
    assert not missing, f"{EXAMPLE} 缺少: {sorted(missing)}"


def test_example_prompt_keys():
    """prompt 段的键与 PromptSettings 对齐"""
    example = _load_example()
    section = example.get("prompt") or {}

    missing = _fields(type(Settings().prompt)) - set(section)
    assert not missing, f"prompt 段缺少: {sorted(missing)}"


def test_example_idle_keys():
    """idle 段的键与 IdleSettings 对齐"""
    example = _load_example()
    section = example.get("idle") or {}

    missing = _fields(type(Settings().idle)) - set(section)
    assert not missing, f"idle 段缺少: {sorted(missing)}"


def test_example_billing_keys():
    """billing 段的键与 BillingSettings 对齐"""
    example = _load_example()
    section = example.get("billing") or {}

    missing = _fields(type(Settings().billing)) - set(section)
    assert not missing, f"billing 段缺少: {sorted(missing)}"


def test_example_parses_as_settings(tmp_path):
    """示例本身能被 load_settings 读进来（键名没写错）"""
    from gensokyoai.settings import load_settings

    settings = load_settings(EXAMPLE)

    assert settings.prompt.fixed > 0
    assert settings.idle.patrol_s > 0
