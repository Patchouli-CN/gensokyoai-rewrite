"""配置加载 —— msgspec 结构化 + YAML。支持多模型路由配置。

模型配置**复用 `schemas.model_schema.ModelConfig`**（单一来源），
本模块只负责把它组合进顶层配置并解析 YAML。

YAML 文本在解析前过一次 `os.path.expandvars`：`token: "${MOONSHOT_API_KEY}"`
这类写法从环境变量取值，密钥不进仓库（未定义/畸形的 `$` 片段原样保留）。
"""

import os
from pathlib import Path

import msgspec
import yaml

from ..schemas.model_schema import ModelConfig

__all__ = [
    "EnergySettings",
    "GensokyoConfig",
    "KnowledgeSite",
    "ModelConfig",
    "OOCJudgeSettings",
    "ResourceSettings",
    "SearchSettings",
    "StyleSettings",
    "WsServerSettings",
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
    """发言门控配置（System-1 混合门控，见 core/brain/gate.py）

    门控是行为变更：代码默认 `enabled=False`（直接构造世界时维持「每条都回」的
    旧行为）；`config/settings.yaml` 显式打开。
    """

    enabled: bool = False
    """ 是否启用反应路径门控（私聊/被 @ 由规则直判，不受影响） """
    judge: str = "local"
    """ 裁判后端：local=主模型当裁判 | typesafe=TypeSafe system one（需装 .[typesafe]）| none=纯规则 """
    typesafe_api_key: str = ""
    """ TypeSafe API key（judge=typesafe；空则用 SDK 环境变量） """
    typesafe_model: str = "jev-latest"
    """ TypeSafe 模型名 """
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


class EnergySettings(msgspec.Struct, frozen=True):
    """精力模型配置（HumanLikeSystem 阶段二，见 core/brain/energy.py）

    精力值（0~1）由三个因子连乘：存在感惩罚（说多了会累）、冷场退避
    （连续不接话越来越懒）、生物钟（深夜困倦）。它调制群聊模糊带的发言
    阈值（精力低 -> 更难接话）与回复长度（精力低 -> 提示 Responder 简短）。

    行为变更：代码默认 `enabled=False`；`config/settings.yaml` 显式打开。
    """

    enabled: bool = False
    """ 是否启用精力模型（直连信号：私聊/被 @ 不受影响） """
    presence_free_ratio: float = 0.25
    """ 窗口内自己发言占比的免罚线，超过才开始扣精力 """
    presence_penalty: float = 0.5
    """ 存在感拉满（占比 100%）时最多扣多少精力 """
    skip_decay: float = 0.85
    """ 冷场退避衰减率：连续「本可接却没接」每回合精力乘这个 """
    skip_streak_cap: int = 8
    """ 冷场连胜上限（防衰减到无限小） """
    night_start: int = 1
    """ 深夜时段起始小时（本地时间，可跨午夜；与 night_end 相等则关生物钟） """
    night_end: int = 7
    """ 深夜时段结束小时（左闭右开） """
    night_factor: float = 0.7
    """ 深夜精力乘数（1.0 关闭生物钟影响） """
    threshold_span: float = 0.2
    """ 精力归零时发言阈值最多抬高这么多（精力只抬不压） """
    threshold_cap: float = 0.95
    """ 调制后阈值封顶 """
    brief_below: float = 0.4
    """ 精力低于该值时给 Responder 注入「回复简短」状态提示 """


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
    reply_window: int = 3
    """ 复读判定的回看窗口（最近几轮自己的回复）。实录照出相邻轮判定的破绽：
        隔一轮原句复读（A→B→A）会漏网，窗口内任一轮超阈值即触发 """


class OOCJudgeSettings(msgspec.Struct, frozen=True):
    """System-1 出戏审查配置（见 core/brain/ooc_judge.py）

    与旧 audit（单点布尔 JSON）的区别：state 带**诱发消息**，多问概率 +
    应用层接受规则——「服从了用户夹带指令」和「丢了角色口吻」分开打分，
    冷面接梗（形服从、魂没丢）不再被一刀切判死。
    """

    enabled: bool = False
    """ 是否启用 System-1 审计（关闭时回退旧单点 audit） """
    mode: str = "side_chain"
    """ side_chain=异步侧链不阻塞（默认， cheapest）；
        blocking=回复进最终缓冲区，审查通过才放行（治本但每回合加一次调用） """
    max_new_tokens: int = 192
    """ local 裁判输出预算（五个概率 + 题名） """
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
         revise 只信双高 + unsafe 两个在实录中被验证的信号。用 TypeSafe system one 并重新
        校准后可打开 """
    ai_like_threshold: float = 0.7
    """ ai_like 超过该值 = 「大概率是 AI 腔」（默认 flag 预警，记录不阻断） """
    ai_like_revise: bool = False
    """ ai_like 超线是否升级为 revise。默认关闭：AI 腔伤文风不伤角色魂，
        误杀比重写更伤体验；裁判校准后可打开让 AI 腔强制重写 """


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

    cache_ttl_s: float = 1800.0
    """ 知识缓存 L1（会话内精确缓存）有效秒数；L2 长期记忆不受此限 """


class WsServerSettings(msgspec.Struct, frozen=True):
    """ws_server 后端的部署策略（优先级：CLI 旗标 > 本节 > 世界默认值）

    这些是「部署怎么跑」的行为旋钮，不是进程参数——放配置文件而不是
    一股脑堆命令行（--host/--port/--token 这类进程绑定仍走 CLI）。
    """

    idle_ttl: float = 600.0
    """ 频道空闲回收秒数 """

    merge_window: float = 1.5
    """ 输入合并窗口秒数（治话没说完/多人同时@）；0 关闭 """

    stall_probability: float | None = None
    """ 过渡语触发概率；None = 世界默认值。QQ 群聊建议 0（过渡语多发一条气泡） """

    initiative_interval: float | None = None
    """ 主动发言评估周期秒数；None = 世界默认值 30。调巨大即关闭沉默碎碎念 """


class WorldSettings(msgspec.Struct, frozen=True):
    """世界的行为旋钮（蒸馏节奏 / 主动发言 / 过渡语 / OOC / 工具 / 关闭）。

    这些旋钮此前是 `TouhouWorld.__init__` 的 14 个裸关键字参数——构造器
    28 参，调用方（app.py / ChannelHub / ws_server / 测试）靠 dict 盲传、
    拼错键名要等到运行期才炸。收敛成一个结构体：单一来源、类型化、可整体
    透传（hub 的 `world_settings` 参数）。

    协作者（裁判 / 口层 / 配置结构体）**不在此列**——它们是注入的实例或
    已成结构体的配置，继续走构造器参数。
    """

    distill_every: int = 10
    """ 每 N 个回合触发一次记忆蒸馏 """
    distill_batch: int = 8
    """ 单次蒸馏压缩的记忆条数 """
    initiative_interval: float = 30.0
    """ 主动发言评估周期（秒）"""
    idle_threshold: float = 180.0
    """ 触发主动发言评估的最小空闲（秒）"""
    urge_threshold: float = 0.35
    """ 对话欲阈值，达到才开口 """
    stall_probability: float = 0.6
    """ 深思考前垫过渡语的概率（0 关闭该行为）"""
    stall_cooldown_turns: int = 3
    """ 两次过渡语之间的最小回合间隔 """
    stall_min_interval: float = 180.0
    """ 两次过渡语之间的最小时间间隔（秒）"""
    ooc_retry: bool = True
    """ 最终回复命中 OOC 规则时是否花一次纠偏重生成 """
    ooc_audit: bool = True
    """ 是否在回复发出后跑异步 OOC 审查（不阻塞热路径）"""
    trace_steps: bool = True
    """ 思考轨迹留档开关 """
    tool_timeout: float = 10.0
    """ 单次工具执行超时（秒）"""
    tool_max_result_chars: int = 2000
    """ 工具结果最大字符数 """
    shutdown_drain_timeout: float = 2.0
    """ 关闭时等待后台侧链收尾的秒数，超时则取消 """


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

    energy: EnergySettings = EnergySettings()
    """ 精力模型配置 """

    ooc_judge: OOCJudgeSettings = OOCJudgeSettings()
    """ System-1 出戏审查配置 """

    style: StyleSettings = StyleSettings()
    """ 文风防复读配置 """

    resource: ResourceSettings = ResourceSettings()
    """ 资源闸门 / 限流配置 """

    embedding: EmbeddingSettings = EmbeddingSettings()
    """ 记忆向量化（语义检索）配置 """

    search: SearchSettings = SearchSettings()
    """ 联网工具配置（可信知识站优先策略）"""

    ws: WsServerSettings = WsServerSettings()
    """ ws_server 后端部署策略 """


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
    data = yaml.safe_load(os.path.expandvars(Path(path).read_text(encoding="utf-8"))) or {}
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
