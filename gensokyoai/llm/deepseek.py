"""DeepSeek 适配器 —— OpenAI 兼容，但计费是人民币 + 峰谷两档。

价格见官方「模型细节」：空闲是高峰的一半，缓存命中另有一档；
高峰是北京时间工作日 9-12、14-18，法定节假日全天按空闲算。
"""

import aiohttp

from .billing import Balance, Billing, PeakBilling, Rates
from .openai import OpenAIProvider

_FLASH = (
    Rates(input=1.0, output=4.0, cached=0.02),
    Rates(input=2.0, output=8.0, cached=0.04),
)
_PRO = (
    Rates(input=4.5, output=13.5, cached=0.15),
    Rates(input=9.0, output=27.0, cached=0.30),
)

# 模型名 -> (空闲价, 高峰价)，单位：元 / 百万 token
# 旧名 deepseek-v4-flash 按 flash 计费
_TABLE: dict[str, tuple[Rates, Rates]] = {
    "deepseek-flash": _FLASH,
    "deepseek-v4-flash": _FLASH,
    "deepseek-v4-pro": _PRO,
}


def billing_for(model: str, *, holidays: tuple[str, ...] = ()) -> Billing | None:
    """按模型名找 DeepSeek 价；表里没有就返回 None（不支持计费）。"""
    hits = [key for key in _TABLE if model and model.lower().startswith(key.lower())]
    if not hits:
        return None
    key = max(hits, key=len)
    off, peak = _TABLE[key]
    return PeakBilling(
        off,
        peak,
        currency="CNY",
        hours=((9, 12), (14, 18)),
        holidays=holidays,
        label=key,
    )


class DeepSeekProvider(OpenAIProvider):
    """走 OpenAI 协议，价格按 DeepSeek 的峰谷表。"""

    name = "deepseek"

    def __init__(
        self,
        base_url: str = "https://api.deepseek.com/v1",
        *,
        model: str = "deepseek-flash",
        holidays: tuple[str, ...] = (),
        **kwargs,
    ) -> None:
        """初始化。

        Args:
            base_url: 端点（https://api.deepseek.com/v1）
            model: 模型名（deepseek-flash / deepseek-v4-pro）
            holidays: 法定节假日日期串（这些天按空闲价）
            **kwargs: 传给 OpenAI 适配器（api_key / think / extra / timeout）
        """
        super().__init__(base_url, model=model, **kwargs)
        self.billing = billing_for(model, holidays=holidays)
        self._balance_url = f"{_root_of(base_url)}/user/balance"
        """ 余额接口不在 /v1 下面 """

    async def balance(self) -> Balance | None:
        """查账户余额（GET /user/balance）。"""
        try:
            async with self.session().get(
                self._balance_url, headers=self._headers
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json()
        except (aiohttp.ClientError, OSError, ValueError, TimeoutError):
            return None
        return _balance_of(data)


def _root_of(base_url: str) -> str:
    """`https://api.deepseek.com/v1` -> `https://api.deepseek.com`。"""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def _balance_of(data) -> Balance | None:
    """解析余额响应：取第一条 balance_infos。"""
    infos = (data or {}).get("balance_infos") or []
    if not isinstance(infos, list) or not infos:
        return None
    first = infos[0]
    return Balance(
        currency=str(first.get("currency") or ""),
        total=float(first.get("total_balance") or 0.0),
        granted=float(first.get("granted_balance") or 0.0),
        topped_up=float(first.get("topped_up_balance") or 0.0),
        available=bool((data or {}).get("is_available", True)),
    )
