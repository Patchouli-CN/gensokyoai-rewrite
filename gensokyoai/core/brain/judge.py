"""裁判后端 —— LocalJudge（本地模型无状态小调用）与 TypeSafeJudge（真 jev，可选依赖）。

两个后端实现同一个 `Judge` 协议（见 `gate.py`），门控逻辑不感知差异：

- **LocalJudge**：零新依赖，复用主模型。无状态调用（`stateless=True`）不进任何
  会话历史，VirtualSession 天然隔离；带 `asyncio.wait_for` 超时保护主循环。
- **TypeSafeJudge**：官方 [typesafe-sdk](https://pypi.org/project/typesafe-sdk/)。
  可选依赖，未安装时给出可操作的报错；客户端可注入便于测试。
"""

import asyncio
import importlib
import json

import msgspec

from ...prompts import prompt_mgr
from ...schemas.model_schema import Message
from ...utils.logger import LoggerManager
from ..config import GateSettings
from ..session_manager import SessionManager
from .gate import Judge, Question


def _clamp01(value: object) -> float:
    """把任意值夹到 0~1；布尔/非数值按 0 处理（宁可保守不接话）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def parse_probabilities(text: str, questions: dict[str, Question]) -> dict[str, float]:
    """从模型输出里解析各题概率（容忍 ```json 包裹等噪声，取第一个 { 到最后一个 }）。

    Args:
        text: 模型原始输出
        questions: 题目表（决定要取哪些键）

    Returns:
        dict: 题名 -> 概率；至少解析出一个键

    Raises:
        ValueError: 输出不是合法 JSON 对象，或一个题都没取到
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"裁判输出不是 JSON: {text[:80]!r}")
    try:
        data = msgspec.json.decode(text[start : end + 1], type=dict)
    except msgspec.DecodeError as error:
        raise ValueError(f"裁判输出 JSON 解析失败: {text[:80]!r}") from error

    answers = {
        name: _clamp01(data.get(name))
        for name in questions
        if isinstance(data.get(name), (int, float)) and not isinstance(data.get(name), bool)
    }
    if not answers:
        raise ValueError(f"裁判输出缺少概率字段: {sorted(questions)}")
    return answers


class LocalJudge:
    """本地裁判：主模型的无状态小调用（VirtualSession 隔离、不进会话历史）。"""

    OWNER = "gate.think"
    """ 会话 owner 名（无状态调用不落会话，仅用于路由与用量记账） """

    def __init__(
        self,
        sessions: SessionManager,
        *,
        max_new_tokens: int = 128,
        temperature: float = 0.2,
        timeout_s: float = 4.0,
    ) -> None:
        """初始化。

        Args:
            sessions: 会话管理器（提供 backend 路由；stateless 调用不碰会话历史）
            max_new_tokens: 裁判输出预算（三个概率 + 题名，128 足够）
            temperature: 低温求稳（概率口径，不需要创造性）
            timeout_s: 单次裁判调用超时（保护主循环不被慢模型拖死）
        """
        self._sessions = sessions
        self._max_new_tokens = max_new_tokens
        self._temperature = temperature
        self._timeout_s = timeout_s
        self._logger = LoggerManager.get_logger("GATE")

    async def ask(
        self, state: dict[str, object], questions: dict[str, Question]
    ) -> dict[str, float]:
        """本地模型打一组概率分。"""
        question_text = "\n".join(
            f"- {name}：{question.instructions}" for name, question in questions.items()
        )
        user = prompt_mgr.render(
            "gate.user",
            state=json.dumps(state, ensure_ascii=False),
            questions=question_text,
        )
        result = await asyncio.wait_for(
            self._sessions.call(
                self.OWNER,
                [
                    Message(role="system", content=prompt_mgr.render("gate.system")),
                    Message(role="user", content=user),
                ],
                stateless=True,
                max_new_tokens=self._max_new_tokens,
                temperature=self._temperature,
            ),
            timeout=self._timeout_s,
        )
        answers = parse_probabilities(result.content or "", questions)
        self._logger.debug(f"本地裁判: {answers}")
        return answers


def _load_typesafe():
    """惰性加载 typesafe_sdk（可选依赖）；缺失时给出可操作的报错。"""
    try:
        return importlib.import_module("typesafe_sdk")
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "未安装 typesafe-sdk（真 jev 后端）。"
            "安装：pip install 'gensokyoai[jev]'；或把 gate.judge 改回 local/none。"
        ) from error


class TypeSafeJudge:
    """真 jev：官方 typesafe-sdk 的 system_one（Noul 是非题 -> 概率）。"""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "jev-latest",
        timeout_ms: int = 4000,
        client=None,
    ) -> None:
        """初始化。

        Args:
            api_key: TypeSafe API key；空则用 SDK 默认（环境变量 TYPESAFE_API_KEY）
            model: jev 模型名
            timeout_ms: SDK 调用超时（毫秒）
            client: 预构造的客户端（测试注入；None 时惰性构造）
        """
        self._api_key = api_key
        self._model = model
        self._timeout_ms = timeout_ms
        self._client = client
        self._logger = LoggerManager.get_logger("GATE")

    def _ensure_client(self):
        """惰性构造 AsyncTypeSafeClient（未装 SDK 时抛可操作错误）。"""
        if self._client is None:
            sdk = _load_typesafe()
            kwargs: dict[str, object] = {"default_model": self._model}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._timeout_ms > 0:
                kwargs["timeout"] = self._timeout_ms / 1000
            self._client = sdk.AsyncTypeSafeClient(**kwargs)
        return self._client

    async def ask(
        self, state: dict[str, object], questions: dict[str, Question]
    ) -> dict[str, float]:
        """调 TypeSafe system_one，取各 Noul 题的 yes 概率。"""
        sdk = _load_typesafe()
        client = self._ensure_client()
        ts_questions = {
            name: sdk.Noul(instructions=question.instructions)
            for name, question in questions.items()
        }
        response = await client.system_one(state=state, questions=ts_questions)
        answers = getattr(response, "answers", None) or {}
        out: dict[str, float] = {}
        for name, answer in answers.items():
            value = getattr(answer, "noul", None)
            if value is None:
                value = getattr(answer, "probability", None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[name] = _clamp01(value)
        if not out:
            raise ValueError(f"jev 未返回可用概率: {answers!r}")
        self._logger.debug(f"jev 裁判: {out}")
        return out


def build_judge(gate: GateSettings, sessions: SessionManager) -> Judge | None:
    """按配置造裁判。

    Args:
        gate: 门控配置（judge 字段决定后端）
        sessions: 会话管理器（local 后端用）

    Returns:
        Judge | None: 裁判实例；judge=none 或未知值时 None（decide 走规则+兜底）
    """
    if gate.judge == "typesafe":
        return TypeSafeJudge(
            api_key=gate.typesafe_api_key,
            model=gate.typesafe_model,
            timeout_ms=gate.timeout_ms,
        )
    if gate.judge == "local":
        return LocalJudge(
            sessions,
            max_new_tokens=gate.max_new_tokens,
            temperature=gate.temperature,
            timeout_s=gate.timeout_ms / 1000,
        )
    return None
