"""模型能力 —— 它看不看得懂图、听不听得懂声。

没有统一标准可查，所以三层：**配置声明**优先，其次**端点自报**（有的服务在
`/v1/models` 里写 capabilities），最后按模型名猜。全都不靠谱时按纯文本处理，
图片降级成文字标记，不报错。
"""

import aiohttp

TEXT = "text"
IMAGE = "image"
AUDIO = "audio"
VIDEO = "video"

ALL = frozenset({TEXT, IMAGE, AUDIO, VIDEO})
""" 认得的能力名（audio / video 只是占位，传输还没做） """

BY_NAME: dict[str, frozenset[str]] = {
    # 纯文本
    "deepseek-chat": frozenset({TEXT}),
    "deepseek-reasoner": frozenset({TEXT}),
    "qwen2.5": frozenset({TEXT}),
    "llama": frozenset({TEXT}),
    # 图文
    "gpt-4o": frozenset({TEXT, IMAGE}),
    "gpt-4.1": frozenset({TEXT, IMAGE}),
    "claude": frozenset({TEXT, IMAGE}),
    "gemini": frozenset({TEXT, IMAGE, AUDIO, VIDEO}),
    "qwen-vl": frozenset({TEXT, IMAGE}),
    "qwen2-vl": frozenset({TEXT, IMAGE}),
    "qwen2.5-vl": frozenset({TEXT, IMAGE}),
    "-vl": frozenset({TEXT, IMAGE}),
    "llava": frozenset({TEXT, IMAGE}),
    # 语音
    "whisper": frozenset({AUDIO}),
    "gpt-4o-audio": frozenset({TEXT, AUDIO}),
}
""" 按模型名前缀猜（长的优先） """

_WORDS = {
    "multimodal": IMAGE,
    "vision": IMAGE,
    "image": IMAGE,
    "audio": AUDIO,
    "video": VIDEO,
    "completion": TEXT,
    "text": TEXT,
}


def normalize(names) -> frozenset[str]:
    """把配置里写的能力名规整一遍（认不出的丢掉）。"""
    found = {str(name).strip().lower() for name in names or ()}
    return frozenset(found & ALL) or frozenset({TEXT})


def detect_by_name(model: str) -> frozenset[str] | None:
    """按模型名猜能力；猜不出来返回 None。"""
    lowered = (model or "").lower()
    if not lowered:
        return None
    hits = [key for key in BY_NAME if key in lowered]
    if not hits:
        return None
    return BY_NAME[max(hits, key=len)]


def from_models_payload(payload, model: str = "") -> frozenset[str] | None:
    """从 `/v1/models` 里读能力（llama.cpp 这类会自报）。"""
    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return None
    wanted = (model or "").lower()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("id") or entry.get("model") or "")
        if wanted and name and wanted not in name.lower():
            continue
        reported = entry.get("capabilities")
        if not isinstance(reported, list):
            continue
        found = {
            _WORDS[str(word).strip().lower()]
            for word in reported
            if str(word).strip().lower() in _WORDS
        }
        if found:
            return frozenset(found)
    return None


async def detect(
    base_url: str,
    *,
    model: str = "",
    api_key: str = "",
    headers: dict | None = None,
    timeout: float = 10.0,
) -> frozenset[str] | None:
    """问端点要能力；问不到就按名字猜，都猜不出返回 None。

    Args:
        base_url: 端点根（http://host:port/v1）
        model: 模型名
        api_key: 令牌（有就带上）
        headers: 额外请求头
        timeout: 探测时限（秒）

    Returns:
        frozenset[str] | None: 能力集合，或者 None（不知道）
    """
    url = f"{base_url.rstrip('/')}/models"
    request_headers = dict(headers or {})
    if api_key:
        request_headers.setdefault("Authorization", f"Bearer {api_key}")
    try:
        client = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client) as session:
            async with session.get(url, headers=request_headers) as response:
                if response.status == 200:
                    found = from_models_payload(await response.json(), model)
                    if found:
                        return found
    except (aiohttp.ClientError, OSError, ValueError, TimeoutError):
        pass
    return detect_by_name(model)
