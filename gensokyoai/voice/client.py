"""GSVI api_v2 的异步客户端 —— 墙内的门铃。

只封装框架用到的四个端点（api_v2 契约见服务源码头部注释）：

- `POST /tts`：文本合成，成功返回 wav 字节流，失败 400 + JSON 错误
- `GET /set_gpt_weights` / `GET /set_sovits_weights`：热切换声线权重
- 服务启停与模型加载由部署方负责（与 llama-server 同一原则：框架只连不拉）
"""

import aiohttp

from ..utils.logger import LoggerManager


class VoiceError(Exception):
    """TTS 服务调用失败（服务返回错误或不可达）"""


class GSVITSClient:
    """GPT-SoVITS api_v2 客户端（aiohttp 异步，连接复用）。"""

    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        """初始化。

        Args:
            base_url: api_v2 服务地址（如 http://127.0.0.1:9880）
            timeout: 单次请求总超时（秒；大段文本合成可能较慢）
        """
        self._logger = LoggerManager.get_logger("VOICE")
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _set_weights(self, endpoint: str, weights_path: str) -> None:
        session = await self._get_session()
        async with session.get(
            f"{self._base}/{endpoint}", params={"weights_path": weights_path}
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or body.get("message") != "success":
                raise VoiceError(f"{endpoint} 失败: {body}")

    async def set_gpt_weights(self, weights_path: str) -> None:
        """热切换 GPT 权重（相对 GSVI 服务根目录的路径）。"""
        await self._set_weights("set_gpt_weights", weights_path)

    async def set_sovits_weights(self, weights_path: str) -> None:
        """热切换 SoVITS 权重（相对 GSVI 服务根目录的路径）。"""
        await self._set_weights("set_sovits_weights", weights_path)

    async def tts(
        self,
        *,
        text: str,
        text_lang: str,
        ref_audio_path: str,
        prompt_text: str,
        prompt_lang: str,
        speed_factor: float = 1.0,
    ) -> bytes:
        """合成一段语音。

        Args:
            text: 待合成文本
            text_lang: 文本语言（zh/ja/en...）
            ref_audio_path: 参考音频路径（GSVI 服务可见的路径）
            prompt_text: 参考音频台词
            prompt_lang: 参考音频语言
            speed_factor: 语速（1.0 正常）

        Returns:
            bytes: wav 音频字节流

        Raises:
            VoiceError: 服务返回 400（含错误 JSON）或网络层失败
        """
        session = await self._get_session()
        payload = {
            "text": text,
            "text_lang": text_lang,
            "ref_audio_path": ref_audio_path,
            "prompt_text": prompt_text,
            "prompt_lang": prompt_lang,
            "media_type": "wav",
            "streaming_mode": False,
            "speed_factor": speed_factor,
        }
        try:
            async with session.post(f"{self._base}/tts", json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise VoiceError(f"/tts 返回 {resp.status}: {body[:500]}")
                return await resp.read()
        except aiohttp.ClientError as err:
            raise VoiceError(f"/tts 请求失败: {err}") from err

    async def aclose(self) -> None:
        """关闭底层连接池（世界关闭时调用）。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
