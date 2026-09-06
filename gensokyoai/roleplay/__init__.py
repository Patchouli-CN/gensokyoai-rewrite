""" 角色扮演领域模块 —— 人设卡、场景等领域数据与规则 """

from .character import CharacterCard, load_character

__all__ = [
    "CharacterCard",
    "load_character",
]
