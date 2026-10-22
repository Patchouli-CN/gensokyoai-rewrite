"""模型管理 —— 按"任务"取端点，能力不够就找能干的替它看。

配置分三份（`config/llm/`）：

    providers.yaml   厂商（base_url / key / 超时）
    models.yaml      模型（能力、价格、端点上的名字）
    tasks.yaml       任务派给谁（chat / vlm / cheap）

主模型没有视觉也没关系：带图的回合先让 `vlm` 描述成文字，再塞进主模型的
上下文——对外看起来就是它自己看见了（中文房间）。
"""

import base64
import os
from pathlib import Path

import aiohttp
import msgspec

from ..model import ChatModel
from .billing import Quota
from .caps import Capability

DEFAULT_ROOT = Path("config/llm")

_AUDIO_NAME = {
    "audio/amr": "voice.amr",
    "audio/silk": "voice.silk",
    "audio/mp3": "voice.mp3",
    "audio/mpeg": "voice.mp3",
    "audio/wav": "voice.wav",
    "audio/ogg": "voice.ogg",
}
""" 转写时给文件起个合适的名字（端点按扩展名判格式） """

_PROMPTS = {
    Capability.IMAGE: (
        "你在帮一个看不见图的同伴转述画面。用两三句话，客观描述图里的人或物："
        "外貌特征、衣着、姿态、可读文字。不要寒暄，不要猜测身份之外的内容。"
    ),
    Capability.AUDIO: (
        "你在帮一个听不到的同伴转述音频。用两三句话说明：谁在说、说了什么、"
        "语气如何。有歌词或旁白就照抄要点。"
    ),
    Capability.VIDEO: (
        "你在帮一个看不到视频的同伴转述画面。简述场景、人物动作与关键变化，"
        "不要逐帧描述。"
    ),
}
""" 每种媒体的转述要求 """

_TASK_FOR = {
    Capability.IMAGE: "vlm",
    Capability.AUDIO: "asr",
    Capability.VIDEO: "vlm",
}
""" 哪种媒体交给哪个任务 """


class Provider(msgspec.Struct, frozen=True):
    """一个厂商端点。"""

    name: str
    base_url: str
    api_key: str = ""
    api_key_env: str = ""
    client_type: str = "openai"
    timeout: float = 120.0


class ModelEntry(msgspec.Struct, frozen=True):
    """一个可用的模型。"""

    id: str
    provider: str
    api_name: str
    name: str = ""
    capabilities: tuple[str, ...] = ("text",)
    input_price: float = 0.0
    output_price: float = 0.0
    cached_price: float = 0.0
    think: bool | None = None
    extra: dict = msgspec.field(default_factory=dict)
    billing: dict = msgspec.field(default_factory=dict)
    """ 计价方式：{mode: peak|flat, currency, off_peak/peak 或 input/output/cached} """

    @property
    def label(self) -> str:
        """日志里显示的名字。"""
        return self.name or self.id


class _Config(msgspec.Struct):
    providers: list[Provider] = msgspec.field(default_factory=list)
    models: list[ModelEntry] = msgspec.field(default_factory=list)
    tasks: dict = msgspec.field(default_factory=dict)


class LLMManager:
    """按任务取模型，顺手把"看不见的图"转述给看不见的模型。"""

    def __init__(
        self,
        root: str | Path = DEFAULT_ROOT,
        *,
        health=None,
        usage_path: Path | None = None,
        log=None,
    ) -> None:
        """初始化。

        Args:
            root: 配置目录（providers.yaml / models.yaml / tasks.yaml）
            health: 健康中心（记延迟与失败）
            usage_path: 用量流水落点
            log: 日志器（None = 不记）
        """
        self.root = Path(root)
        self._health = health
        self._usage_path = usage_path
        self._log = log
        self._config: _Config | None = None
        self._cache: dict[str, ChatModel] = {}

    async def get(self, task: str = "chat") -> ChatModel:
        """按任务名取模型（首次读配置并建端点，之后复用）。

        Args:
            task: 任务名（tasks.yaml 里的键），或直接写模型 id

        Returns:
            ChatModel: 可以用的端点

        Raises:
            LookupError: 配置里没有这个任务/模型
        """
        model = self._pick(task)
        cached = self._cache.get(model.id)
        if cached is not None:
            return cached
        provider = self._provider_of(model)
        built = ChatModel(
            provider.base_url,
            provider=self._client_type(provider),
            name=model.api_name,
            api_key=self._key_of(provider),
            think=model.think,
            timeout=provider.timeout,
            extra=model.extra or None,
            capabilities=model.capabilities,
            billing=self._billing_of(model),
            quotas=self.quotas(),
            usage_path=self._usage_path,
            health=self._health,
        )
        self._cache[model.id] = built
        self._info(f"建端点 {model.label}（{provider.name} / {model.api_name}）")
        return built

    async def supports(self, task: str, capability: Capability) -> bool:
        """这个任务的模型能不能干某件事。"""
        return capability in self._pick(task).capabilities

    async def describe(
        self,
        media,
        *,
        capability: Capability = Capability.IMAGE,
        question: str = "",
    ) -> str:
        """让能处理的模型把一段媒体转述成文字（中文房间的那一半）。

        Args:
            media: `[(说明, data URL), ...]`
            capability: 这是哪种媒体（图 / 音频 / 视频）
            question: 用户在问什么（给转述一点方向）

        Returns:
            str: 转述文本；没有可用模型或失败时返回空串
        """
        if not media:
            return ""
        task = _TASK_FOR.get(capability, "vlm")
        try:
            model = await self.get(task)
        except LookupError:
            self._warn(f"没配 {task} 任务，{capability} 只能当没有")
            return ""
        lines = [_PROMPTS.get(capability, _PROMPTS[Capability.IMAGE])]
        labels = [label for label, _ in media]
        if labels:
            lines.append("它们分别是：" + "；".join(labels))
        if question:
            lines.append(f"同伴想问：{question}")
        try:
            reply = await model.chat(
                "\n".join(lines),
                f"请转述这段{capability}。",
                images=list(media),
            )
        except Exception as error:  # noqa: BLE001 - 转述失败不该拖垮整轮
            self._warn(f"转述 {capability} 失败：{error!r}")
            return ""
        return (reply.text or "").strip()

    async def transcribe(self, media, *, label: str = "") -> str:
        """把语音转成文字（走 asr 任务的 /audio/transcriptions）。

        Args:
            media: `(说明, data URL)` 或 data URL 字符串
            label: 日志用的说明

        Returns:
            str: 转写文本；没配 asr 或失败时返回空串
        """
        if not media:
            return ""
        url = media[1] if isinstance(media, tuple) else str(media)
        head, _, payload = url.partition(",")
        if not head.startswith("data:") or not payload:
            return ""
        mime = head[5:].split(";")[0] or "audio/amr"
        try:
            model = await self.get("asr")
        except LookupError:
            self._warn("没配 asr 任务，语音只能当没有")
            return ""
        raw = base64.b64decode(payload, validate=False)
        provider = self._provider_of(self._pick("asr"))
        endpoint = provider.base_url.rstrip("/") + "/audio/transcriptions"
        form = aiohttp.FormData()
        form.add_field(
            "file", raw, filename=_AUDIO_NAME.get(mime, "audio.bin"), content_type=mime
        )
        form.add_field("model", model.name)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=provider.timeout)
            ) as session:
                async with session.post(
                    endpoint,
                    data=form,
                    headers={"Authorization": f"Bearer {self._key_of(provider)}"},
                ) as response:
                    if response.status != 200:
                        self._warn(
                            f"转写失败 HTTP {response.status}: "
                            f"{(await response.text())[:120]}"
                        )
                        return ""
                    data = await response.json()
        except Exception as error:  # noqa: BLE001 - 转写失败不该拖垮整轮
            self._warn(f"转写请求失败（{label or mime}）：{error!r}")
            return ""
        return str(data.get("text") or "").strip()

    def quotas(self) -> tuple:
        """计费窗口（config/llm/billing.yaml 的 quotas；没有就是空）。"""
        data = self._read("billing.yaml")
        found = []
        for item in data.get("quotas") or []:
            if not isinstance(item, dict):
                continue
            found.append(
                Quota(
                    span_s=_span_seconds(str(item.get("span") or "5h")),
                    tokens=int(item.get("tokens") or 0),
                    cost=float(item.get("cost") or 0.0),
                )
            )
        return tuple(found)

    async def aclose(self) -> None:
        """关掉所有建过的端点。"""
        for model in self._cache.values():
            await model.close()
        self._cache.clear()

    # ---------- 内部 ----------

    def _load(self) -> _Config:
        """读三份配置（只读一次）。"""
        if self._config is not None:
            return self._config
        providers = self._read("providers.yaml")
        models = self._read("models.yaml")
        tasks = self._read("tasks.yaml")
        self._config = _Config(
            providers=msgspec.convert(providers.get("providers", []), list[Provider]),
            models=msgspec.convert(models.get("models", []), list[ModelEntry]),
            tasks=tasks.get("tasks", {}),
        )
        return self._config

    def _read(self, name: str) -> dict:
        """读一个 yaml；没有就当空。"""
        path = self.root / name
        if not path.exists():
            return {}
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _pick(self, task: str) -> ModelEntry:
        """任务名 → 模型条目（找不到就按模型 id 直接找）。"""
        config = self._load()
        by_id = {model.id: model for model in config.models}
        if task in by_id:
            return by_id[task]
        wanted = config.tasks.get(task)
        if isinstance(wanted, str):
            wanted = [wanted]
        for model_id in wanted or []:
            if model_id in by_id:
                return by_id[model_id]
        raise LookupError(f"config/llm 里没有任务或模型: {task}")

    def _provider_of(self, model: ModelEntry) -> Provider:
        """找模型所属厂商。"""
        for provider in self._load().providers:
            if provider.name == model.provider:
                return provider
        raise LookupError(f"模型 {model.id} 指向未知厂商 {model.provider}")

    @staticmethod
    def _client_type(provider: Provider) -> str:
        """厂商的客户端类型（先只认 openai 兼容）。"""
        return provider.client_type or "openai"

    @staticmethod
    def _key_of(provider: Provider) -> str:
        """取 key：先看环境变量，再看配置里写死的。"""
        if provider.api_key_env:
            value = os.environ.get(provider.api_key_env, "").strip()
            if value:
                return value
        return provider.api_key

    @staticmethod
    def _billing_of(model: ModelEntry):
        """按配置造计费：峰谷 / 一口价 / 老字段，三选一。"""
        from .billing import FlatBilling, PeakBilling, Rates

        spec = model.billing or {}
        mode = str(spec.get("mode") or "").lower()
        currency = str(spec.get("currency") or "CNY")

        if mode == "peak":
            off = _rates(spec.get("off_peak") or {})
            peak = _rates(spec.get("peak") or {})
            if not (off or peak):
                return None
            return PeakBilling(
                off,
                peak,
                currency=currency,
                hours=tuple(
                    tuple(pair) for pair in spec.get("hours") or ((9, 12), (14, 18))
                ),
                holidays=tuple(spec.get("holidays") or ()),
                label=model.label,
            )
        if mode == "flat":
            rates = _rates(spec)
            return (
                FlatBilling(rates, currency=currency, label=model.label)
                if rates
                else None
            )
        if not (model.input_price or model.output_price or model.cached_price):
            return None
        return FlatBilling(
            Rates(
                input=model.input_price,
                output=model.output_price,
                cached=model.cached_price,
            ),
            currency=currency,
            label=model.label,
        )

    def _info(self, text: str) -> None:
        if self._log is not None:
            self._log.info(text)

    def _warn(self, text: str) -> None:
        if self._log is not None:
            self._log.warning(text)


def _rates(spec: dict):
    """{input, output, cached} → Rates（全 0 返回 None）。"""
    from .billing import Rates

    if not isinstance(spec, dict):
        return None
    rates = Rates(
        input=float(spec.get("input") or 0.0),
        output=float(spec.get("output") or 0.0),
        cached=float(spec.get("cached") or 0.0),
    )
    return rates if (rates.input or rates.output or rates.cached) else None


def _span_seconds(text: str) -> float:
    """`5h` / `7d` / `90m` -> 秒（跟 settings.parse_span 同一套写法）。"""
    from ..settings import parse_span

    return parse_span(text)
