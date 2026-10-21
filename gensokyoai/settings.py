"""配置 —— 模型与计费写在 `config/settings.yaml`，两个宿主共用一份。

这个文件不进 git（仓库里只有 `settings.example.yaml` 当模板），密钥不会被提交。
"""

from pathlib import Path

import msgspec
import yaml

from .llm import Billing, FlatBilling, PeakBilling, Quota, Rates
from .model import DEFAULT_TIMEOUT_S, ChatModel


def _repo_root() -> Path:
    """从包目录往上找仓库根（有 pyproject.toml 的那层）；找不到就用 cwd。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


DEFAULT_PATH = _repo_root() / "config" / "settings.yaml"
""" 默认配置文件位置（锚在仓库根，不随 cwd 漂 ） """

EXAMPLE_PATH = _repo_root() / "config" / "settings.example.yaml"
""" 模板（在 git 里）；复制成 settings.yaml 再改 """

_SPANS = {"m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}


class ModelSettings(msgspec.Struct, frozen=True):
    """模型端点。"""

    provider: str = "openai"
    base_url: str = "http://127.0.0.1:8080/v1"
    name: str = ""
    api_key: str = ""
    think: bool | None = False
    timeout: float = DEFAULT_TIMEOUT_S
    extra: dict = msgspec.field(default_factory=dict)
    capabilities: tuple[str, ...] = ("text", "image")
    """ 能干哪些活：text / image（audio、video 只是占位，传输还没做） """
    auto_detect: bool = True
    """ 启动时问端点要一次能力（问不到就按名字猜，再不行用上面的声明） """


class RatesSettings(msgspec.Struct, frozen=True):
    """一档价（每百万 token）。"""

    input: float = 0.0
    output: float = 0.0
    cached: float = 0.0

    def to_rates(self) -> Rates:
        """转成计费层用的数字。"""
        return Rates(input=self.input, output=self.output, cached=self.cached)


class PriceSettings(msgspec.Struct, frozen=True):
    """自定义单价；全 0 = 用适配器自带的价（厂商官方价）。"""

    currency: str = "USD"
    input: float = 0.0
    output: float = 0.0
    cached: float = 0.0
    peak: RatesSettings | None = None
    """ 高峰价（不给就是不分时段的一口价） """
    peak_weekdays: tuple[int, ...] = (1, 2, 3, 4, 5)
    peak_hours: tuple[str, ...] = ("9-12", "14-18")
    holidays: tuple[str, ...] = ()
    """ 法定节假日（这些天按平常价，不分时段） """

    def to_billing(self) -> Billing | None:
        """全 0 表示没配，交给适配器自己的价。"""
        if not (self.input or self.output or self.cached or self.peak):
            return None
        off = Rates(input=self.input, output=self.output, cached=self.cached)
        if self.peak is None:
            return FlatBilling(off, currency=self.currency)
        return PeakBilling(
            off,
            self.peak.to_rates(),
            currency=self.currency,
            weekdays=self.peak_weekdays,
            hours=parse_hours(self.peak_hours),
            holidays=self.holidays,
        )


class QuotaSettings(msgspec.Struct, frozen=True):
    """一个滚动窗口的额度。"""

    span: str = "5h"
    tokens: int = 0
    cost: float = 0.0

    def to_quota(self) -> Quota:
        """跨度写法（5h / 7d / 90m）-> 秒。"""
        return Quota(span_s=parse_span(self.span), tokens=self.tokens, cost=self.cost)


class BillingSettings(msgspec.Struct, frozen=True):
    """计费与限额。"""

    price: PriceSettings = msgspec.field(default_factory=PriceSettings)
    quotas: tuple[QuotaSettings, ...] = ()
    usage: str = ""
    """ 用量流水路径（空 = 不落盘，重启后窗口从头算） """


class Settings(msgspec.Struct, frozen=True):
    """一份配置。"""

    model: ModelSettings = msgspec.field(default_factory=ModelSettings)
    billing: BillingSettings = msgspec.field(default_factory=BillingSettings)

    def billing_of(self) -> Billing | None:
        """配置里写死价就用它；没写就用适配器自带的官方价。"""
        return self.billing.price.to_billing()

    def quotas(self) -> tuple[Quota, ...]:
        """窗口额度（额度和钱都为 0 的条目忽略）。"""
        return tuple(
            quota.to_quota()
            for quota in self.billing.quotas
            if quota.tokens or quota.cost
        )

    def usage_path(self, base_dir: Path | None = None) -> Path | None:
        """用量流水路径（相对路径按 base_dir 解析）。"""
        if not self.billing.usage:
            return None
        path = Path(self.billing.usage)
        if base_dir is not None and not path.is_absolute():
            path = base_dir / path
        return path

    def build_model(
        self,
        health=None,
        *,
        base_url: str = "",
        usage_path: Path | None = None,
    ) -> ChatModel:
        """按配置造模型端点（顺带建账本）。"""
        model = self.model
        return ChatModel(
            base_url or model.base_url,
            provider=model.provider,
            name=model.name,
            api_key=model.api_key,
            think=model.think,
            timeout=model.timeout,
            extra=model.extra or None,
            capabilities=model.capabilities,
            billing=self.billing_of(),
            quotas=self.quotas(),
            usage_path=usage_path or self.usage_path(),
            health=health,
        )

    async def adopt_capabilities(self, model: ChatModel) -> frozenset[str]:
        """启动时探测一次能力（配了 auto_detect 才问）。"""
        if self.model.auto_detect:
            return await model.refresh_capabilities()
        return model.capabilities


def parse_span(text: str) -> float:
    """`5h` / `7d` / `90m` / `2w` -> 秒。

    Raises:
        ValueError: 写法不认识
    """
    raw = str(text).strip().lower()
    if not raw:
        raise ValueError("窗口跨度不能为空")
    unit = raw[-1]
    if unit not in _SPANS:
        raise ValueError(f"窗口跨度要带单位（m/h/d/w）: {text!r}")
    try:
        amount = float(raw[:-1])
    except ValueError as err:
        raise ValueError(f"窗口跨度不是数字: {text!r}") from err
    if amount <= 0:
        raise ValueError(f"窗口跨度要大于 0: {text!r}")
    return amount * _SPANS[unit]


def load_settings(path: str | Path | None = None) -> Settings:
    """读配置。

    没给路径时用默认位置，文件不存在就回落到默认值（本地 llama-server、
    不记钱）；**给了路径却不存在会报错**——省得你以为在读配置、其实在用默认。

    Args:
        path: 配置文件路径（None = `config/settings.yaml`）

    Returns:
        Settings: 解析好的配置

    Raises:
        FileNotFoundError: 显式指定的配置文件不存在
        ValueError: YAML 不是 mapping，或字段不对
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        if path is not None:
            raise FileNotFoundError(
                f"配置文件不存在: {target}"
                f"（模板见 {EXAMPLE_PATH}；不想用配置就别指定路径）"
            )
        return Settings()
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件不是 mapping: {target}")
    try:
        return msgspec.convert(raw, Settings, strict=False)
    except msgspec.ValidationError as err:
        raise ValueError(f"配置文件字段不对（{target}）: {err}") from err


def parse_hours(items) -> tuple[tuple[int, int], ...]:
    """`["9-12", "14-18"]` -> `((9, 12), (14, 18))`。

    Raises:
        ValueError: 写法不对，或起止反了
    """
    found: list[tuple[int, int]] = []
    for item in items or ():
        raw = str(item).replace("：", ":").replace("-", "-").strip()
        start, _, end = raw.partition("-")
        try:
            left, right = int(start), int(end)
        except ValueError as err:
            raise ValueError(f"时段写法不对（应形如 9-12）: {item!r}") from err
        if not (0 <= left < right <= 24):
            raise ValueError(f"时段不合法（0 ≤ 起 < 止 ≤ 24）: {item!r}")
        found.append((left, right))
    return tuple(found) or ((9, 12), (14, 18))
