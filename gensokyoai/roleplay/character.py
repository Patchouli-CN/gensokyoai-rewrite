""" 角色人设卡 —— 领域数据与加载（字段兼容原版 GensokyoAI 角色卡）"""

from pathlib import Path

import msgspec
import yaml

class CharacterCard(msgspec.Struct, frozen=True):
    """ 角色人设卡。字段与原版 characters/*.yaml 核心结构对齐，旧角色库可直接迁移。"""
    name: str = ""
    """ 角色名 """
    system_prompt: str = ""
    """ 人设正文（性格、说话方式、世界观）"""
    greeting: str = ""
    """ 开场白 """
    example_dialogue: list[str] = []
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

def load_character(path: str | Path) -> CharacterCard:
    """ 从 YAML 加载人设卡。

    Args:
        path: 角色卡 YAML 文件路径

    Returns:
        CharacterCard: 缺省字段自动补全

    Raises:
        FileNotFoundError: 文件不存在
        msgspec.ValidationError: 字段类型不符
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return msgspec.convert(data, CharacterCard, strict=False)
