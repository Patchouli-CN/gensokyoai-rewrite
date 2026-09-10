"""配置加载测试：单一模型契约 / YAML 键映射 / 会话预算下发"""

from gensokyoai.core.config import GensokyoConfig, load_config
from gensokyoai.schemas.model_schema import ModelConfig


def test_model_config_is_single_contract():
    """模型配置只此一处：顶层配置直接复用 schemas.ModelConfig（不再有两个近重复结构体）"""
    config = GensokyoConfig()
    assert isinstance(config.default_model, ModelConfig)
    assert isinstance(config.brain, ModelConfig)
    assert isinstance(config.responder, ModelConfig)


def test_model_config_carries_routing_and_budget():
    """合并后的契约同时承载路由（provider）与预算（context_window）"""
    model = ModelConfig()
    assert model.provider == "llama_cpp"
    assert model.context_window > 0


def test_yaml_keys_actually_apply(tmp_path):
    """YAML 键名与字段名一致时真的生效"""
    path = tmp_path / "settings.yaml"
    path.write_text(
        "default_model:\n"
        "  model_name: my-model\n"
        "  context_window: 12345\n"
        "resource:\n"
        "  max_concurrent: 3\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.default_model.model_name == "my-model"
    assert config.default_model.context_window == 12345
    assert config.resource.max_concurrent == 3


def test_mismatched_key_silently_ignored(tmp_path):
    """键名写错会被静默忽略 —— 正是上一版 `model:` 失效的原因，这里固化该行为"""
    path = tmp_path / "settings.yaml"
    path.write_text("model:\n  model_name: ignored\n", encoding="utf-8")
    config = load_config(path)
    assert config.default_model.model_name == "qwen", "未知键不影响字段，走默认值"


def test_project_settings_file_key_matches():
    """仓库里的 settings.yaml 用的是 default_model:（键名匹配，配置真的生效）"""
    config = load_config("config/settings.yaml")
    assert config.default_model.model_name == "qwen"
    assert config.default_model.context_window == 32768


def test_session_factory_applies_context_window():
    """装配层把 context_window 下发到会话预算（该字段此前零消费者）"""
    from gensokyoai.core.bootstrap import discover_all
    from gensokyoai.core.session_factory import build_session_manager

    discover_all()
    config = GensokyoConfig(responder=ModelConfig(context_window=4096))
    sessions = build_session_manager(config)

    assert sessions._sessions["responder"].max_tokens == 4096
    # 默认模型窗口也下发给了默认预算
    assert sessions._sessions["brain.think"].max_tokens == config.brain.context_window
