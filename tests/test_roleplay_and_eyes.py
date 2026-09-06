"""roleplay 人设卡 与 eyes 解析 单元测试"""

import pytest

from gensokyoai.eyes.parser import build_snapshot, parse_message
from gensokyoai.roleplay import CharacterCard, load_character
from gensokyoai.schemas.scene_schema import SceneEvent


def test_load_character_from_yaml(tmp_path):
    """从 YAML 加载人设卡并补全缺省字段"""
    card_file = tmp_path / "marisa.yaml"
    card_file.write_text(
        "name: 雾雨魔理沙\nsystem_prompt: 我是普通的魔法使\ngreeting: 哟\n", encoding="utf-8",
    )
    card = load_character(card_file)
    assert card.name == "雾雨魔理沙"
    assert card.example_dialogue == []
    assert card.motivation_weights == {}


def test_character_system_prompt_format():
    """to_system_prompt 组装角色名 + 人设正文"""
    card = CharacterCard(name="魔理沙", system_prompt="普通的魔法使")
    assert card.to_system_prompt() == "【魔理沙】\n普通的魔法使"


def test_load_character_missing_file():
    """文件不存在时报 FileNotFoundError"""
    with pytest.raises(FileNotFoundError):
        load_character("不存在的路径.yaml")


def test_parse_message_onebot11():
    """解析 OneBot11 事件形态"""
    event = parse_message({
        "post_type": "message",
        "sender": {"nickname": "小明", "user_id": 10001},
        "raw_message": "早上好",
        "time": 1725600000,
    })
    assert event.sender == "小明"
    assert event.content == "早上好"


def test_parse_message_generic_dict():
    """解析通用字典形态"""
    event = parse_message({"sender": "小红", "content": "晚上好"})
    assert event.sender == "小红"
    assert event.content == "晚上好"


def test_parse_message_empty_content_raises():
    """缺内容字段时抛 ValueError"""
    with pytest.raises(ValueError):
        parse_message({"sender": "小明"})


def test_build_snapshot_defaults_from_last_event():
    """快照缺省取最后一条事件的发送者/内容"""
    events = [
        SceneEvent(sender="A", content="第一条"),
        SceneEvent(sender="B", content="第二条"),
    ]
    snapshot = build_snapshot("group_chat", events)
    assert snapshot.sender == "B"
    assert snapshot.content == "第二条"
    assert snapshot.participants == ["A", "B"]
    assert len(snapshot.context_snippet) == 2
