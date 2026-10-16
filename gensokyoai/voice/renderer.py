"""语音渲染器 —— REPLY 事件的旁路消费者。

主链路（DeliveryService）每说一句完整的话就往总线发一个 REPLY；
本渲染器订阅它，异步把文本喂给 TTS 服务，拿到音频交给 mouth 补发语音。

旁路三原则：

1. **不挡路**：事件处理器只负责派活，渲染在自己的任务里跑，文本回复零延迟
2. **不炸锅**：渲染失败只记日志，绝不把异常溅回主链路
3. **不乱嗓**：声线热切换与合成串行化（GSVI 服务的权重是全局状态，
   两条回复并发抢权重会互相串味）
"""

import asyncio
from typing import TYPE_CHECKING

from ..mouth.base import Mouth
from ..schemas.event_schema import BaseEvent, EventTopic, ReplyPayload
from ..utils.logger import LoggerManager
from .client import GSVITSClient
from .config import VoiceProfile, VoiceSettings

if TYPE_CHECKING:
    from ..roleplay.loop import TouhouWorld


class VoiceRenderer:
    """REPLY -> TTS -> mouth.send_voice 的旁路渲染管线。"""

    def __init__(self, client: GSVITSClient, settings: VoiceSettings, mouth: Mouth) -> None:
        """初始化。

        Args:
            client: GSVI 服务客户端
            settings: 嗓音层配置（角色声线档案表）
            mouth: 口层（语音的最终投递处）
        """
        self._logger = LoggerManager.get_logger("VOICE")
        self._client = client
        self._settings = settings
        self._mouth = mouth
        self._lock = asyncio.Lock()
        """ 渲染串行锁：声线切换 + 合成对 GSVI 服务是全局操作，必须排队 """
        self._current: str | None = None
        """ 服务当前加载的是哪个角色的权重（免重复切换）"""
        self._tasks: set[asyncio.Task] = set()
        """ 在飞渲染任务（防 GC + 关闭时收割）"""

    async def handle_reply(self, event: BaseEvent) -> None:
        """REPLY 事件处理：认出角色就派一个渲染任务（立即返回，不阻塞总线）。"""
        payload = event.payload
        if not isinstance(payload, ReplyPayload) or not payload.text.strip():
            return
        profile = self._settings.characters.get(payload.character)
        if profile is None:
            return  # 没配声线的角色保持文字形态
        task = asyncio.get_running_loop().create_task(
            self._render(payload.character, payload.text, profile)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle_shutdown(self, event: BaseEvent) -> None:
        """SHUTDOWN 事件处理：等在飞任务收尾，关客户端连接池。"""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._client.aclose()

    async def _render(self, character: str, text: str, profile: VoiceProfile) -> None:
        """渲染一条语音：按需热切换声线 -> 合成 -> 补发 mouth。"""
        try:
            async with self._lock:
                if self._current != character:
                    await self._client.set_gpt_weights(profile.gpt_weights)
                    await self._client.set_sovits_weights(profile.sovits_weights)
                    self._current = character
                    self._logger.info(f"声线已切换: {character}")
                audio = await self._client.tts(
                    text=text,
                    text_lang=self._settings.text_lang,
                    ref_audio_path=profile.ref_audio,
                    prompt_text=profile.prompt_text,
                    prompt_lang=profile.prompt_lang,
                    speed_factor=self._settings.speed_factor,
                )
            await self._mouth.send_voice(character, audio)
            self._logger.info(f"语音已投递: {character} {len(text)}字 -> {len(audio)}字节")
        except Exception:
            self._logger.exception(f"语音渲染失败（{character}，不影响文本回复）")


def attach_voice(world: TouhouWorld, settings: VoiceSettings, mouth: Mouth) -> VoiceRenderer:
    """把语音旁路挂到世界的事件总线上。

    Args:
        world: TouhouWorld（取它的 bus 订阅 REPLY / SHUTDOWN）
        settings: 嗓音层配置
        mouth: 口层（语音投递处）

    Returns:
        VoiceRenderer: 装配好的渲染器
    """
    renderer = VoiceRenderer(GSVITSClient(settings.base_url, settings.timeout), settings, mouth)
    world.bus.subscribe(EventTopic.REPLY, renderer.handle_reply)
    world.bus.subscribe(EventTopic.SHUTDOWN, renderer.handle_shutdown)
    return renderer
