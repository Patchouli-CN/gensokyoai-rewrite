"""角色卡加载 —— 卡面只有三个字段：version / name / detail。

detail 装全部设定，不做花哨键值对；格式约束归场景层，卡不参与。
"""

from pathlib import Path

import yaml


class CharacterCard:
    """人设卡。"""

    def __init__(self, version: str, name: str, detail: str) -> None:
        self.version = version
        self.name = name
        self.detail = detail

    @property
    def persona(self) -> str:
        """人设 system prompt：角色名 + 设定正文。"""
        return f"【{self.name}】\n{self.detail}".strip()


def load_card(path: str | Path) -> CharacterCard:
    """从 YAML 加载人设卡。

    Args:
        path: 卡面 YAML 路径

    Returns:
        CharacterCard: 版本 / 角色名 / 设定正文

    Raises:
        ValueError: 缺字段或卡面不是 mapping
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"卡面不是 mapping: {path}")
    missing = [key for key in ("version", "name", "detail") if key not in data]
    if missing:
        raise ValueError(
            f"卡面缺字段 {missing}: {path}（应为 version / name / detail）"
        )
    return CharacterCard(
        version=str(data["version"]),
        name=str(data["name"]),
        detail=str(data["detail"]),
    )
