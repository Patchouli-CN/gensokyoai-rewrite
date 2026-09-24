"""配置加载 —— msgspec 结构化 + YAML。支持多模型路由配置。

模型配置**复用 `schemas.model_schema.ModelConfig`**（单一来源），
本模块只负责把它组合进顶层配置并解析 YAML。
"""

from pathlib import Path

import msgspec
import yaml

from ..schemas.model_schema import ModelConfig

__all__ = [
    "GensokyoConfig",
    "KnowledgeSite",
    "ModelConfig",
    "OOCJudgeSettings",
    "ResourceSettings",
    "SearchSettings",
    "StyleSettings",
    "load_config",
    "load_eye_config",
    "resolve_eye_config_dir",
]


class ResourceSettings(msgspec.Struct, frozen=True):
    """资源闸门 / 限流配置（保护单模型稀缺资源）"""

    enabled: bool = True
    """ 是否启用资源闸门 """
    max_concurrent: int = 1
    """ 全局并发上限（本地单模型建议 1，避免并发打爆显存）"""
    rpm: int = 0
    """ 每租户每分钟模型调用上限；0 不限 """
    concurrency: int = 1
    """ 每租户并发上限 """
    calls_per_day: int = 0
    """ 每租户每日模型调用上限；0 不限 """
    tokens_per_day: int = 0
    """ 每租户每日 token 预算；0 不限 """
    ingress_rate: float = 0.0
    """ 入口令牌桶速率（条/秒）；0 表示关闭入口限流 """
    ingress_burst: int = 0
    """ 入口令牌桶容量（允许的突发条数）"""


class GateSettings(msgspec.Struct, frozen=True):
    """发言门控配置（jev 式混合门控，见 core/brain/gate.py）

    门控是行为变更：代码默认 `enabled=False`（直接构造世界时维持「每条都回」的
    旧行为）；`config/settings.yaml` 显式打开。
    """

    enabled: bool = False
    """ 是否启用反应路径门控（私聊/被 @ 由规则直判，不受影响） """
    judge: str = "local"
    """ 裁判后端：local=主模型当裁判 | typesafe=真 jev（需装 .[jev]）| none=纯规则 """
    typesafe_api_key: str = ""
    """ TypeSafe API key（judge=typesafe；空则用 SDK 环境变量） """
    typesafe_model: str = "jev-latest"
    """ jev 模型名 """
    group_threshold: float = 0.6
    """ 群聊未点名时，should_reply 达到该值才接话（调低更活跃） """
    search_threshold: float = 0.1
    """ needs_search 超过该值视为「可能要查证」（当前仅落日志） """
    timeout_ms: int = 60000
    """ 单次裁判调用超时（local 走 wait_for；typesafe 传给 SDK）。
        默认 60s：本地模型单次调用 10~25s，4s 量级会让裁判永远超时降级 """
    route_by_model: bool = True
    """ 档位路由模型化：有裁判时，连「私聊 / 被 @」也问一次裁判，用 needs_deep
        分数决定推理档位（弥补规则路由看不懂情绪/关系的短板）；
        False 或裁判不可用时回落规则 route() """
    deep_cuts: tuple[float, float, float] = (0.3, 0.6, 0.85)
    """ needs_deep 分数 -> 档位的三个切点（进 MID / HIGH / MAX 的线） """
    max_new_tokens: int = 128
    """ local 裁判的输出预算（三个概率 + 题名，128 足够） """
    temperature: float = 0.2
    """ local 裁判的采样温度（低温求稳） """
    presence_window_s: float = 300.0
    """ 活跃度统计窗口秒数（对齐 qqbot 的 5 分钟） """


class StyleSettings(msgspec.Struct, frozen=True):
    """文风防复读配置（治小模型「重复自己的模板/口头禅」）

    20 轮真机实录照出的问题：相邻两轮回复相似度 88%（换句话输入也吐同骨架）、
    同一个收尾梗（「来，张嘴——啊～」）连用 6 次。两项防御都是零/低成本：
    预防性提示（生成前告知避开）+ 兜底重写（相似度过阈值触发一次纠偏）。
    """

    dedup_endings: bool = True
    """ 收尾去重（ported qqbot「不复读结尾」规则）：同一收尾在最近窗口用了
        够多次就剥掉它，确定性规则零 token """
    ending_window: int = 6
    """ 收尾去重的回看窗口（最近几条自己的回复） """
    similarity_retry: float = 0.75
    """ 相邻轮回复相似度达到该值触发一次防复读重写；0 = 关闭。
        阈值参考：实录中骨架复用 88%、正常 callback 复用 <40% """


class OOCJudgeSettings(msgspec.Struct, frozen=True):
    """jev 化出戏审查配置（见 core/brain/ooc_judge.py）

    与旧 audit（单点布尔 JSON）的区别：state 带**诱发消息**，多问概率 +
    应用层接受规则——「服从了用户夹带指令」和「丢了角色口吻」分开打分，
    冷面接梗（形服从、魂没丢）不再被一刀切判死。
    """

    enabled: bool = False
    """ 是否启用 jev 化审计（关闭时回退旧单点 audit） """
    mode: str = "side_chain"
    """ side_chain=异步侧链不阻塞（默认， cheapest）；
        blocking=回复进最终缓冲区，审查通过才放行（治本但每回合加一次调用） """
    max_new_tokens: int = 192
    """ local 裁判输出预算（四个概率 + 题名） """
    temperature: float = 0.2
    """ local 裁判采样温度（低温求稳） """
    timeout_ms: int = 60000
    """ 单次审查调用超时（本地模型一次 10~25s） """
    unsafe_threshold: float = 0.5
    """ contains_unsafe 超过即一票否决（泄提示词/隐私/危险引导） """
    instruction_threshold: float = 0.6
    """ follows_embedded_instruction 超过即「大概率在服从注入指令」 """
    voice_threshold: float = 0.6
    """ breaks_voice 超过即「大概率丢了角色口吻」 """
    plausible_low: float = 0.35
    """ plausible_as_character 低于该值 = 判 revise（不是角色会说出口的话）——
        **仅在 plausible_revise=true 时生效** """
    plausible_high: float = 0.6
    """ plausible_as_character 低于该值（但不低于 plausible_low）= flag 黄色预警 """
    plausible_revise: bool = False
    """ plausible 低分是否参与 revise。默认关闭：20 轮实录回放照出本地裁判的
        plausible 不可信——好回复被打 0.10（非塌缩值的随机低分）造成误报；
         revise 只信双高 + unsafe 两个在实录中被验证的信号。用真 jev 并重新
        校准后可打开 """


class EmbeddingSettings(msgspec.Struct, frozen=True):
    """记忆向量化（embedding）配置：enabled=False 时长期记忆检索退回子串匹配"""

    enabled: bool = False
    """ 是否启用语义检索（需要可用的 OpenAI 兼容 embedding 端点）"""

    base_url: str = "http://127.0.0.1:8081/v1"
    """ embedding 服务地址（llama-server 需另起 --embedding 实例，与 chat 端口分开）"""

    model: str = "bge-small-zh"
    """ embedding 模型名 """

    token: str | None = None
    """ 访问 token（本地服务通常不需要）"""

    timeout: float = 30.0
    """ 单次向量化请求超时（秒）"""

    min_score: float = 0.35
    """ 语义检索相似度下限：低于此分的结果视为噪声丢弃（bge 分数带窄，实测噪声 ~0.30）"""


class KnowledgeSite(msgspec.Struct, frozen=True):
    """一个可信知识站点"""

    site: str
    """ 域名（如 thbwiki.cc）"""
    desc: str = ""
    """ 一句话说明（站点定位 / 是否支持站内 API，进工具指令）"""


class SearchSettings(msgspec.Struct, frozen=True):
    """联网工具配置：可靠站优先、web_search 兜底（老项目的资料策略）

    站点表会被拼进脑内【可用工具】块（tool_directive），告诉模型查领域知识
    先 fetch_url 抓这些站，查不到再 web_search。
    """

    knowledge_sites: list[KnowledgeSite] = []
    """ 可信知识站点（东方 Project 场景默认 thbwiki，见 settings.yaml）"""


class GensokyoConfig(msgspec.Struct, frozen=True):
    """顶层配置"""

    default_model: ModelConfig = ModelConfig()
    """ 默认模型（未指定模块时使用）"""

    brain: ModelConfig = ModelConfig()
    """ Brain 决策模块使用的模型 """

    responder: ModelConfig = ModelConfig()
    """ Responder 表达模块使用的模型 """

    ooc: ModelConfig | None = None
    """ OOC 审计使用的模型（可选，默认用 brain）"""

    memorizer: ModelConfig | None = None
    """ 记忆压缩使用的模型（可选，默认用 brain）"""

    gate: GateSettings = GateSettings()
    """ 发言门控配置 """

    ooc_judge: OOCJudgeSettings = OOCJudgeSettings()
    """ jev 化出戏审查配置 """

    style: StyleSettings = StyleSettings()
    """ 文风防复读配置 """

    resource: ResourceSettings = ResourceSettings()
    """ 资源闸门 / 限流配置 """

    embedding: EmbeddingSettings = EmbeddingSettings()
    """ 记忆向量化（语义检索）配置 """

    search: SearchSettings = SearchSettings()
    """ 联网工具配置（可信知识站优先策略）"""


def load_config(path: str | Path) -> GensokyoConfig:
    """读取 YAML 配置并校验为强类型配置对象。

    Args:
        path: 配置文件路径（YAML）

    Returns:
        GensokyoConfig: 缺省字段自动补全为 Struct 默认值

    Raises:
        FileNotFoundError: 配置文件不存在
        msgspec.ValidationError: 配置字段类型不符
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return msgspec.convert(data, GensokyoConfig, strict=False)


def load_eye_config[S](
    eye_name: str, schema: type[S], *, base_dir: str | Path = "config/eyes"
) -> S | None:
    """加载眼层平台配置：`{base_dir}/{eye_name}.yaml`，缺失返回 None。

    每个 eye 模块自带自己的 msgspec schema（单文件平台）；多文件平台
    （如 nb2 要吃自己的 `.env`）用 `resolve_eye_config_dir` 拿目录自行解析。
    只找工作目录——眼层配置本质是部署方的东西，包内不自带；缺失时
    调用方直接用 schema 默认值兜底即可。

    Args:
        eye_name: 平台名（文件名，不含后缀）
        schema: 该 eye 的 msgspec.Struct 配置类型
        base_dir: 眼层配置根目录（测试可指向临时目录）

    Returns:
        S | None: 解析出的强类型配置；文件不存在为 None

    Raises:
        msgspec.ValidationError: 配置字段类型不符
    """
    path = Path(base_dir) / f"{eye_name}.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return msgspec.convert(data, schema, strict=False)


def resolve_eye_config_dir(eye_name: str, *, base_dir: str | Path = "config/eyes") -> Path | None:
    """多文件平台的配置目录：`{base_dir}/{eye_name}/` 存在则返回，否则 None。

    Args:
        eye_name: 平台名（目录名）
        base_dir: 眼层配置根目录

    Returns:
        Path | None: 配置目录；不存在为 None
    """
    path = Path(base_dir) / eye_name
    return path if path.is_dir() else None
