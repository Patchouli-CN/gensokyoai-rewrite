"""嗓音层配置 —— 与眼层同一约定：`config/voice/{服务名}.yaml`。

每个 TTS 服务（目前只有 gsvits）自己的 schema 自己定义，加载复用眼层那套
通用 YAML 加载器（`load_eye_config` 只是按目录+schema 解析，与 eye 无关）。
"""

from pathlib import Path

import msgspec

from ..core.config import load_eye_config

DEFAULT_VOICE_DIR = "config/voice"
""" 嗓音层配置根目录（与 config/eyes 平级）"""


class VoiceProfile(msgspec.Struct, frozen=True):
    """一个角色的声线档案（GSVI 权重 + 参考音频）"""

    gpt_weights: str
    """ GPT 权重路径（相对 GSVI 服务根目录）"""
    sovits_weights: str
    """ SoVITS 权重路径（相对 GSVI 服务根目录）"""
    ref_audio: str
    """ 参考音频绝对路径（3-10 秒干声）"""
    prompt_text: str = ""
    """ 参考音频对应的台词文本 """
    prompt_lang: str = "ja"
    """ 参考音频语言（ja/zh/en...）"""


class VoiceSettings(msgspec.Struct, frozen=True):
    """嗓音层配置（config/voice/gsvits.yaml 的 schema）"""

    enabled: bool = False
    """ 总开关：关掉则整个语音旁路不装配 """
    base_url: str = "http://127.0.0.1:9880"
    """ GSVI api_v2 服务地址 """
    timeout: float = 120.0
    """ 单次合成的 HTTP 超时（秒）"""
    text_lang: str = "zh"
    """ 待合成文本的语言（角色回复一般是中文）"""
    speed_factor: float = 1.0
    """ 语速（1.0 正常）"""
    characters: dict[str, VoiceProfile] = {}
    """ 角色名（角色卡 name） -> 声线档案 """


def load_voice_config(
    name: str = "gsvits", *, base_dir: str | Path = DEFAULT_VOICE_DIR
) -> VoiceSettings | None:
    """加载嗓音层配置：`{base_dir}/{name}.yaml`，缺失返回 None。

    Args:
        name: 服务名（文件名，不含后缀）
        base_dir: 嗓音层配置根目录（测试可指向临时目录）

    Returns:
        VoiceSettings | None: 解析出的强类型配置；文件不存在为 None
    """
    return load_eye_config(name, VoiceSettings, base_dir=base_dir)
