"""余额查询测试 —— 解析、适配器不提供时返回 None、/balance 命令。"""

from gensokyoai.command.builtin import build_executor
from gensokyoai.command.core import CommandContext
from gensokyoai.llm import Balance
from gensokyoai.llm.deepseek import DeepSeekProvider, _balance_of

REAL_PAYLOAD = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "CNY",
            "total_balance": "41.78",
            "granted_balance": "0.00",
            "topped_up_balance": "41.78",
        }
    ],
}


class _Resp:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def json(self):
        return self._payload


def test_balance_payload_is_parsed():
    """官方响应：金额是字符串，要转成数"""
    balance = _balance_of(REAL_PAYLOAD)

    assert balance is not None
    assert balance.currency == "CNY"
    assert balance.total == 41.78
    assert balance.topped_up == 41.78
    assert balance.available is True
    assert "¥41.78" in balance.describe()


def test_balance_without_infos_is_none():
    """没有余额信息就当查不到，别瞎猜成 0"""
    assert _balance_of({"is_available": False, "balance_infos": []}) is None
    assert _balance_of({}) is None


def test_balance_describe_mentions_grants():
    """赠送额度单独说明"""
    balance = Balance(currency="CNY", total=12.5, granted=2.0, topped_up=10.5)
    assert "赠送" in balance.describe() and "充值" in balance.describe()


async def test_deepseek_balance_hits_the_right_url(monkeypatch):
    """DeepSeek 的余额接口在 /user/balance，不在 /v1 下面"""
    provider = DeepSeekProvider(
        "https://api.deepseek.com/v1", model="deepseek-flash", api_key="sk-test"
    )
    seen = {}

    class _Session:
        def get(self, url, headers=None):
            seen.update(url=url, headers=headers or {})
            return _Resp(REAL_PAYLOAD)

    monkeypatch.setattr(provider, "session", lambda: _Session())

    found = await provider.balance()

    assert seen["url"] == "https://api.deepseek.com/user/balance"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert found is not None and found.total == 41.78


async def test_balance_failure_is_none_not_an_error(monkeypatch):
    """查不到就是 None，别把余额查询的故障变成聊天故障"""

    class _Boom:
        def get(self, url, headers=None):
            raise OSError("断网")

    provider = DeepSeekProvider(api_key="sk-test")
    monkeypatch.setattr(provider, "session", lambda: _Boom())

    assert await provider.balance() is None


async def test_openai_provider_has_no_balance():
    """OpenAI 没这个接口：返回 None，由命令层说明"""
    from gensokyoai.llm import OpenAIProvider

    provider = OpenAIProvider("http://127.0.0.1:8080/v1")
    assert await provider.balance() is None


class _FakeModel:
    """只实现 /balance 需要的两样东西。"""

    def __init__(self, balance: Balance | None) -> None:
        self._balance = balance

    async def balance(self):
        return self._balance

    class ledger:
        @staticmethod
        def describe() -> str:
            return "¥1/4 空闲"


async def test_balance_command_reports_balance():
    """/balance（/余额）回一条余额说明"""
    model = _FakeModel(Balance(currency="CNY", total=41.78))
    executor = build_executor(None, model)

    reply = await executor.execute("/balance", CommandContext(permission="admin"))

    assert reply is not None
    assert "¥41.78" in reply
    assert "空闲" in reply, "顺带把计费口径说一句"


async def test_balance_command_explains_when_unsupported():
    """端点不提供余额：说清，而不是报错"""
    executor = build_executor(None, _FakeModel(None))

    reply = await executor.execute("/balance", CommandContext(permission="admin"))

    assert reply == "这个端点不提供余额查询"


async def test_balance_command_needs_admin():
    """余额算账户信息，访客看不见"""
    executor = build_executor(None, _FakeModel(Balance(currency="CNY", total=1.0)))

    ctx = CommandContext(permission="visitor")
    assert await executor.execute("/balance", ctx) is None
