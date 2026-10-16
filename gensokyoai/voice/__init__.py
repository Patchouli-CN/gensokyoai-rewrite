"""嗓音层 —— 文本转语音（TTS）旁路。

隐喻：mouth 管「说什么、往哪送」，voice 管「听起来什么样」。
主链路只发文本；语音渲染挂在 REPLY 事件旁路上，渲染完由 mouth 补发语音，
慢或失败都不挡文字回复的路。

外部重资产（GPT-SoVITS 服务）留在进程墙外，墙内只有一个门铃：
`GSVITSClient` 按 api_v2 的 HTTP 契约按铃。
"""

from .client import GSVITSClient, VoiceError
from .config import VoiceProfile, VoiceSettings, load_voice_config
from .renderer import VoiceRenderer, attach_voice

__all__ = [
    "GSVITSClient",
    "VoiceError",
    "VoiceProfile",
    "VoiceRenderer",
    "VoiceSettings",
    "attach_voice",
    "load_voice_config",
]
