"""角色卡加载 —— 与 v1 同一份 YAML 卡面（卡只写人设，格式约束归场景层）。"""

from pathlib import Path

import yaml


class CharacterCard:
    """人设卡（只取开口需要的字段；example_dialogue 等以后要用再加）。"""

    def __init__(self, name: str, system_prompt: str, greeting: str = "") -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.greeting = greeting

    @property
    def persona(self) -> str:
        """人设 system prompt：角色名 + 人设正文。"""
        return f"【{self.name}】\n{self.system_prompt}".strip()


def load_card(path: str | Path) -> CharacterCard:
    """从 YAML 加载人设卡。

    Args:
        path: 卡面 YAML 路径

    Returns:
        CharacterCard: 角色名 / 人设正文 / 开场白
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return CharacterCard(
        name=str(data.get("name", "")),
        system_prompt=str(data.get("system_prompt", "")),
        greeting=str(data.get("greeting", "")),
    )
