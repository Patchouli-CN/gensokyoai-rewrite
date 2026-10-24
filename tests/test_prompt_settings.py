"""配置能被覆盖：prompt / idle 两块从 settings.yaml 读进来。"""

from gensokyoai.settings import Settings, load_settings

SAMPLE = """prompt:
  cache: false
  align: 128
  compact_ratio: 0.5
  compact_keep: 16
idle:
  sleep_s: 600
  patrol_s: 30
"""


def test_prompt_defaults():
    """默认值独立可用，不读取开发者本机配置"""
    settings = Settings()

    assert settings.prompt.cache is True
    assert settings.prompt.align == 64
    assert settings.prompt.compact_ratio == 0.75
    assert settings.prompt.compact_keep == 8


def test_prompt_overrides(tmp_path):
    """配置文件能改掉这些旋钮"""
    path = tmp_path / "settings.yaml"
    path.write_text(SAMPLE, encoding="utf-8")

    settings = load_settings(str(path))

    assert settings.prompt.cache is False
    assert settings.prompt.align == 128
    assert settings.prompt.compact_ratio == 0.5
    assert settings.prompt.compact_keep == 16
    assert settings.idle.sleep_s == 600
    assert settings.idle.patrol_s == 30


def test_repo_settings_example_parse():
    """已跟踪的配置模板可解析，干净 CI 不需要本地 settings.yaml"""
    settings = load_settings("config/settings.example.yaml")

    assert settings.prompt.align > 0
    assert 0 < settings.prompt.compact_ratio <= 1
    assert settings.idle.sleep_s >= 0
