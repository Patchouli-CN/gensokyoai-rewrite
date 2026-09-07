""" 角色人设卡 —— 领域数据与加载（字段兼容原版 GensokyoAI 角色卡）"""

from pathlib import Path
from typing import Any
import uuid

import msgspec
import yaml

class CharacterStats(msgspec.Struct):
    """ 角色状态 """
    
    emotion: str = "平静"
    """ 当前主要情绪 """
    
    motivation: float = 0.5
    """" 对话欲望 """
    
    extra: dict[str, Any] = msgspec.field(default_factory=dict)
    """ 扩展状态槽位 (如: 临时buff, 任务进度) """
    
    def update(self, **kwargs) -> None:
        """ 批量更新状态 """
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self.extra[key] = value
                
class Dialogue(msgspec.Struct):
    """ 单条对话 """
    user: str = ""
    assistant: str = ""

class CharacterCard(msgspec.Struct, frozen=True):
    """ 角色人设卡。字段与原版 characters/*.yaml 核心结构对齐，旧角色库可直接迁移。"""
    name: str = ""
    """ 角色名 """
    system_prompt: str = ""
    """ 人设正文（性格、说话方式、世界观）"""
    greeting: str = ""
    """ 开场白 """
    example_dialogue: list[Dialogue] = []
    """ 示例对话（few-shot 风格参照）"""
    motivation_weights: dict[str, float] = {}
    """ 四维对话欲权重（expression/emotional/relational/situational）"""
    emotion_baseline: dict[str, float] = {}
    """ 八维情绪基线 """
    metadata: dict[str, str] = {}
    """ 扩展元数据（作者、来源、版本等）"""

    def to_system_prompt(self) -> str:
        """ 组装 system prompt：角色名 + 人设正文。

        Returns:
            拼接后的 system prompt 文本
        """
        return f"【{self.name}】\n{self.system_prompt}".strip()
    
class Character(msgspec.Struct):
    """ 角色 """
    
    _card: CharacterCard
    """ 角色卡 """
    
    status: CharacterStats = msgspec.field(default_factory=CharacterStats)
    """ 状态存储 """
    
    cid: str = msgspec.field(default_factory=lambda: str(uuid.uuid4()))
    """ 角色ID """
    
    @property
    def name(self) -> str:
        """ 名字 """
        return self._card.name
    
    @property
    def prompt(self) -> str:
        """ 角色提示词 """
        return self._card.to_system_prompt()
    
    @property
    def card(self) -> CharacterCard:
        """ 暴露只读的人设卡引用 """
        return self._card
    

def load_character(path: str | Path) -> Character:
    """ 从 YAML 加载人设卡。

    Args:
        path: 角色卡 YAML 文件路径

    Returns:
        Character: 缺省字段自动补全

    Raises:
        FileNotFoundError: 文件不存在
        msgspec.ValidationError: 字段类型不符
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    card = msgspec.convert(data, CharacterCard, strict=False)
    character = Character(card)
    return character