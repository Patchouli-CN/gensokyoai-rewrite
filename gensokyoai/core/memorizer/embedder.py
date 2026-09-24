"""记忆向量化 —— Embedder 协议、OpenAI 兼容实现与余弦排序。

设计要点：

- **协议保持薄**：embedder 只负责「文本 -> 向量」，存储与检索是 memorizer 层的
  职责（与 ModelProvider 的「薄协议」同一哲学）；
- **OpenAICompatEmbedder**：POST {base_url}/embeddings，llama-server（--embedding
  实例）/ vLLM / 云端兼容端点通用；与 chat 走同一套 aiohttp 口径；
- **检索用 numpy 暴力余弦**：记忆量级（千级）下毫秒完成，不引入向量库；
  真上十万级再换 pgvector / sqlite-vec，接口不变。
"""

from typing import Protocol

import aiohttp
import numpy as np

from ...utils.logger import LoggerManager
from ..config import EmbeddingSettings


class Embedder(Protocol):
    """文本向量化器协议（结构化类型，任何实现 embed() 的对象都算）"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本编码为向量（返回顺序与输入一致）。

        Args:
            texts: 待编码文本列表

        Returns:
            list[list[float]]: 每条文本对应的向量，维度由模型决定
        """
        ...


def build_embedder(settings: EmbeddingSettings) -> Embedder | None:
    """按配置构建向量化器；未启用时返回 None（检索退回子串匹配）。

    Args:
        settings: embedding 配置节

    Returns:
        Embedder | None: 启用则返回 OpenAI 兼容向量化器，否则 None
    """
    if not settings.enabled:
        return None
    logger = LoggerManager.get_logger("EMBED")
    logger.info(f"记忆向量化已启用: {settings.model} @ {settings.base_url}")
    return OpenAICompatEmbedder(
        base_url=settings.base_url,
        model=settings.model,
        token=settings.token,
        timeout=settings.timeout,
    )


class OpenAICompatEmbedder:
    """OpenAI 兼容 /embeddings 端点的向量化器。

    注意 llama-server 的 --embedding 实例不能同时服务 chat——本地部署需要
    另起一个实例（端口与主模型分开），配置见 settings.yaml 的 embedding 节。
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """初始化。

        Args:
            base_url: OpenAI 兼容服务地址（如 http://127.0.0.1:8081/v1）
            model: embedding 模型名
            token: 访问 token（本地服务通常不需要）
            timeout: 单次请求超时（秒）
        """
        self._logger = LoggerManager.get_logger("EMBED")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._token = token
        self._timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = {"model": self._model, "input": texts}
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with (
            aiohttp.ClientSession(timeout=timeout) as http,
            http.post(f"{self._base_url}/embeddings", json=payload, headers=headers) as resp,
        ):
            if resp.status != 200:
                body = await resp.text()
                self._logger.error(f"向量化请求失败: status={resp.status} 响应体={body[:300]!r}")
            resp.raise_for_status()
            data = await resp.json()
        # OpenAI 协议不保证 data 顺序与输入一致，按 index 归位
        items = sorted(data["data"], key=lambda d: d["index"])
        vectors = [[float(v) for v in item["embedding"]] for item in items]
        self._logger.debug(f"向量化完成: {len(texts)} 条 维度={len(vectors[0]) if vectors else 0}")
        return vectors


def cosine_rank(
    query: list[float], candidates: dict[str, list[float]], limit: int = 10
) -> list[tuple[str, float]]:
    """按余弦相似度对候选向量排序。

    Args:
        query: 查询向量
        candidates: 键 -> 向量的候选集
        limit: 返回条数上限

    Returns:
        list[tuple[str, float]]: (键, 相似度) 按相似度降序；零范数向量得 0 分
    """
    if not candidates:
        return []
    keys = list(candidates)
    matrix = np.array([candidates[k] for k in keys], dtype=np.float32)
    q = np.asarray(query, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0.0:
        return []
    norms = np.linalg.norm(matrix, axis=1)
    valid = norms > 0
    scores = np.zeros(len(keys), dtype=np.float32)
    scores[valid] = (matrix[valid] @ q) / (norms[valid] * q_norm)
    order = np.argsort(-scores, kind="stable")[:limit]
    return [(keys[i], float(scores[i])) for i in order]
