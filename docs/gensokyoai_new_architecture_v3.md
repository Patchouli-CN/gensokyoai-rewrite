
---

# Gensokyo AI — 架构设计文档（v3）

> 本文档基于团队多轮讨论整理，记录 Gensokyo AI 的架构设计。在初代架构基础上，新增了项目目录结构、模型复用与上下文隔离机制、VirtualSession/SessionManager 设计、命名规范与分层依赖规则等内容。

---

## 1. 项目背景与目标

### 1.1 背景

在角色扮演（Role-Play）场景中，小模型的上下文窗口是有限的，而角色一致性、记忆管理、推理判断等需求会大量占用上下文资源。本项目计划本地部署 Qwen3.6-35B-A3B 模型（MoE 架构，总参 35B，激活仅 3B），模型上下文窗口为 8192 tokens。

### 1.2 核心思路

**将推理和记忆从上下文中剥离出来，用外部工程手段管理，只在上下文中保留最精华的部分。**

具体做法：

- 用外部工程代码（而非模型上下文）承载记忆
- 每个子模块拥有独立的虚拟会话（VirtualSession），共享同一个模型实例
- 采用最早淘汰架构（Earliest Eviction）管理记忆
- 声明式扩展（Python 装饰器）+ importlib 自动扫描注册（类似 Spring 的 ComponentScan）
- 模型复用：一个模型实例，多个"虚拟会话"，框架层管理每个会话的上下文生命周期

---

## 2. 整体架构概览

```
+------------------------------------------------------------------+
|                        Eyes（感知层）                             |
|  +----------------+  +----------------+  +------------------+      |
|  | QQ群聊场景      |  | 私聊场景       |  | 其他场景适配器    |      |
|  +-------+--------+  +-------+--------+  +--------+---------+      |
|          +------------------+---------------------+                |
|                   场景快照（结构化）                                 |
+--------------------------+-----------------------------------------+
                           |
                           v
+--------------------------+-----------------------------------------+
|                      Brain（决策层）                                |
|  +----------------+  +----------------+  +------------------+       |
|  | 推理链引擎      |  | OOC Verdict  |  | Knowledge Base   |       |
|  | (LOW/MID/      |  | (人设一致性   |  | (知识检索)        |       |
|  |  HIGH/MAX)     |  |  检测)         |  |                 |       |
|  +-------+--------+  +-------+--------+  +--------+---------+       |
|          +------------------+---------------------+                 |
|                   初稿 + 结构化元数据（压缩后）                        |
+--------------------------+-----------------------------------------+
                           |
                           v
+--------------------------+-----------------------------------------+
|                    事件总线（Event Bus）                             |
|           结构化事件：意图 / 情绪 / 记忆 / 初稿                      |
+--------------------------+-----------------------------------------+
                           |
                           v
+--------------------------+-----------------------------------------+
|                   Responder（表达层）                                |
|           润色 + 语气调整 + 最终输出                                 |
+--------------------------+-----------------------------------------+
                           |
                           v
                      用户可见回复

+------------------------------------------------------------------+
|                  Memorizer（记忆层）                                |
|  记录对话历史 + Brain 内心想法（摘要压缩后存储）                    |
|  分级存储：热记忆 / 温记忆 / 冷记忆 + 最早淘汰策略                  |
+------------------------------------------------------------------+

+------------------------------------------------------------------+
|                  HealthCenter（监控层）                             |
|  全局状态监控 + 主动干预（Token消耗 / 上下文使用率 / OOC频率        |
|  / 推理档位分布 / 自动调参）                                       |
+------------------------------------------------------------------+

+------------------------------------------------------------------+
|              SessionManager（上下文隔离层）                         |
|  管理所有 VirtualSession 的生命周期，每个 owner 拥有独立上下文      |
|  框架负责：会话创建/复用 -> 拼上下文 -> 调模型 -> 返回结果          |
|  模块自身完全不关心上下文管理                                      |
+------------------------------------------------------------------+
```

---

## 3. 核心模块定义

### 3.1 Eyes（感知层）

**职责：** 作为系统的"眼睛"，负责感知外部场景并产出标准化的场景快照。

**设计要点：**

- Eyes 不是真正的视觉模块，而是**抽象感知层**的概念
- 不同场景下"看"到的内容不同，Eyes 做一层适配
- 输出为结构化的场景快照，屏蔽底层平台差异

**场景适配示例：**

| 场景 | 感知内容 |
|------|---------|
| QQ 群聊 | 群消息流、@信息、上下文对话 |
| 私聊 | 两人对话历史 |
| 未来扩展 | 论坛、游戏内聊天等 |

**输出结构（场景快照）：**

| 字段 | 说明 |
|------|------|
| scene_type | 场景类型（群聊 / 私聊 / 频道） |
| context_snippet | 当前上下文片段 |
| participants | 参与者列表 |
| event_queue | 未读 / 待处理事件队列 |

### 3.2 Brain（决策层）

**职责：** 系统的大脑，负责思考、推理、判断等"大脑该做的事"，产出初稿回复。

**子模块：**

| 子模块 | 职责 |
|--------|------|
| 推理链引擎 | 管理 LOW / MID / HIGH / MAX 四档推理 |
| OOC Verdict | 检测回复是否偏离角色人设 |
| Knowledge Base | 知识检索，为推理提供资料支撑 |

**上下文隔离设计：**

Brain 内部的各子模块（意图分析、记忆检索、OOC 检测、最终决策）各自拥有独立的 VirtualSession，通过 SessionManager 复用同一个模型实例。每个子模块的推理过程不会污染其他子模块的上下文。

**工作流程：**

1. 接收 Eyes 的场景快照
2. 根据任务复杂度自动选择推理档位
3. 调用 Knowledge Base 检索相关知识
4. 执行推理链，产出初稿 + 结构化元数据
5. 经 OOC Verdict 检测（前置快速过滤 + 后置深度审计）
6. 通过事件总线传递给 Responder

### 3.3 Responder（表达层）

**职责：** 负责回复的润色和最终输出。

**设计要点：**

- **只做润色 + 回复**，不做思考和推理
- 接收 Brain 通过事件总线传来的初稿和结构化元数据
- 根据元数据中的意图、情绪、角色状态等信息进行语气和措辞调整

**输入（事件总线传来的结构化对象）：**

| 字段 | 说明 |
|------|------|
| intent | 意图判断结果 |
| emotion | 情绪状态 |
| memory_refs | 需要调用的记忆条目 |
| draft | Brain 产出的初稿回复文本 |
| confidence | 置信度 / 确定性标记 |

### 3.4 Memorizer（记忆层）

**职责：** 负责记忆管理，记录对话历史和 Brain 的内心想法。

**设计要点：**

- 记录内容包括：对话历史 + Brain 的内心想法
- 对内心想法做**摘要压缩后再存储**，只保留关键决策节点
- 采用**分级存储**策略：

| 记忆级别 | 说明 | 存储位置 |
|---------|------|---------|
| 热记忆 | 最近几轮对话 | 直接放上下文 |
| 温记忆 | 近期重要事件 | 摘要后放上下文 |
| 冷记忆 | 长期设定 | 按需检索 |

- 采用**最早淘汰架构（Earliest Eviction）**管理记忆淘汰

### 3.5 HealthCenter（监控层）

**职责：** 监控整体系统状态，支持主动干预。

**监控指标：**

| 指标 | 说明 |
|------|------|
| Token 消耗 | 各模块的 token 使用情况 |
| 延迟 | 各模块的响应延迟 |
| 上下文使用率 | 当前上下文窗口占用比例 |
| 记忆存储量 | 记忆模块的存储大小 |
| OOC 触发频率 | OOC Verdict 的触发情况 |
| 推理档位分布 | 各推理档位的请求占比 |

**主动干预能力：**

- 上下文使用率超过 80% -> 通知 Memorizer 做压缩清理
- OOC 触发频率突然升高 -> 自动调高推理档位
- 记忆模块膨胀 -> 自动触发冷记忆归档

---

## 4. ModelProvider 协议

### 4.1 设计目标

- 不绑定特定模型厂商，通过协议接口灵活接入
- 不同模型在角色扮演上的表现差异大，灵活切换可针对不同角色选最合适的模型
- 预留角色扮演相关的参数位

### 4.2 协议接口定义（草案）

```
class ModelProvider(Protocol):
    # 模型提供商统一协议
    async def chat_completion(
        self,
        messages: List[Message],
        model_config: ModelConfig,
        # 角色扮演专用参数
        system_prompt: str | None = None,       # system prompt 注入
        stop_tokens: List[str] | None = None,    # 停止 token
        temperature: float = 0.7,
        max_tokens: int = 2048,
        # 角色一致性相关
        consistency_mode: bool = False,          # 是否开启角色一致性模式
        character_id: str | None = None,         # 角色标识
        # 工具调用（OpenAI function calling）
        tools: List[ToolSpec] | None = None,     # 工具声明；Provider 层多轮循环执行
    ) -> CompletionResult:
        ...
```

> 实现说明：工具循环在 Provider 层完成（无状态多轮）——模型请求 tool_calls -> 执行 -> 回填 tool 消息 ->
> 复请求，直至模型收尾或达到轮数上限（8）。`ToolSpec.to_openai_tool()` 从函数签名自动推导 JSON Schema；
> 同步/异步工具自动分发，未知工具与损坏参数兜底为错误文本回传，不崩链。

### 4.3 推理链设计

**核心原则：** 不依赖模型自带的 thinking/reasoning 功能，在应用层自己编排推理步骤，精确控制推理深度和 token 开销。

**四档推理：**

| 档位 | 推理步骤 | 适用场景 |
|------|---------|---------|
| LOW | 1 步：意图分类 | 日常寒暄、简单问答 |
| MID | 2 步：意图分类 + 角色状态检查 | 一般对话，需检查角色情绪 |
| HIGH | 3 步：意图分类 + 角色状态检查 + 记忆检索 + 情绪推理 | 复杂剧情推进、多角色交互 |
| MAX | 4 步：完整多步 Chain-of-Thought | 情感转折、重大剧情节点 |

**动态推理路由：**

- 不是所有任务都值得开高推理档
- 日常寒暄用 LOW 甚至跳过推理
- 涉及复杂剧情、多角色交互、情感转折时才用 HIGH / MAX
- 需要一个轻量级任务分类器，根据输入复杂度自动选档位

---

## 5. 扩展系统

### 5.1 声明式装饰器

使用 Python 装饰器 `@ext(name, ext_type)` 声明扩展，新增能力不需要改核心代码，符合开闭原则。

**设计要点：**

- 不同角色可以按需加载不同的扩展组合
- 装饰器需提供执行顺序机制，处理扩展间的依赖关系
- 代码可读性好，一眼就能看出扩展职责

**扩展声明示例（草案）：**

```
@ext(name="emotion_renderer", ext_type="brain_hook")
class EmotionRenderer:
    # 情绪渲染扩展 - 在推理后对回复进行情绪注入
    async def execute(self, context: BrainContext) -> BrainContext:
        ...

@ext(name="memory_retriever", ext_type="brain_hook")
class MemoryRetriever:
    # 记忆检索扩展 - 在推理前检索相关记忆
    async def execute(self, context: BrainContext) -> BrainContext:
        ...

@ext(name="ooc_checker", ext_type="brain_hook")
class OOCChecker:
    # OOC检测扩展
    async def execute(self, context: BrainContext) -> BrainContext:
        ...
```

### 5.2 扩展类型

| 扩展类型 | 执行时机 | 说明 |
|---------|---------|------|
| brain_hook | Brain 推理链中 | 插入推理步骤 |
| eyes_adapter | Eyes 感知层 | 适配新场景 |
| memorizer_policy | Memorizer 存储/检索 | 自定义记忆策略 |
| responder_middleware | Responder 前后 | 插件式处理 |

### 5.3 importlib 动态扫描注册

采用类似 Java Spring 的 `@ComponentScan` 机制，用 Python 的 `importlib` + `pkgutil` 实现自动包扫描注册。

**全局注册器（coreregistry.py）：**

```
class Registry:
    _modules = {}  # name -> {class, type}

    @classmethod
    def register(cls, name: str, ext_type: str):
        """装饰器：注册扩展模块"""
        def decorator(module_class):
            cls._modules[name] = {"class": module_class, "type": ext_type}
            return module_class
        return decorator

    @classmethod
    def get(cls, name: str): ...

    @classmethod
    def get_by_type(cls, ext_type: str): ...

def auto_discover(package_path: str):
    """扫描指定包下所有模块，触发装饰器注册"""
    package = importlib.import_module(package_path)
    for _, module_name, _ in pkgutil.walk_packages(
        package.__path__, package.__name__ + "."
    ):
        importlib.import_module(module_name)
```

**使用方式：**

```
# 在模块中声明注册
@Registry.register(name="qq_group", ext_type="eyes")
class QQGroupEyes: ...

# 启动时一行扫描
auto_discover("gensokyoai.eyes")
auto_discover("gensokyoai.extensions")
auto_discover("gensokyoai.models")
```

**设计要点：**

- 扫描范围可控：只扫描指定包，不会误扫核心代码
- 装饰器即注册：新增模块只需加装饰器，无需改配置文件
- 懒加载友好：importlib 触发模块加载时才执行装饰器
- 扩展目录热插拔：extensions/ 目录下的文件随时增删，下次启动自动发现

---

## 6. 上下文窗口管理策略

### 6.1 上下文预算管理（8K 窗口版）

针对 Qwen3.6-35B-A3B 模型的 8192 tokens 上下文窗口，推荐分配方案：

| 区域 | Token 预算 | 说明 |
|------|-----------|------|
| System Prompt（角色设定） | ~1000 | 人设、世界观、行为规则 |
| Brain 推理输出 | ~800 | 结构化思考结果，非完整 CoT |
| 记忆注入（Memorizer 检索结果） | ~1500 | 热记忆 + 温记忆摘要 |
| Eyes 场景快照 | ~500 | 最近几条群消息 |
| 对话历史 | ~2000 | 最近 5-8 轮对话 |
| Responder 生成预留 | ~2000 | 留给模型输出 |
| 安全余量 | ~392 | 防止溢出 |

总计 8192，其中**输入侧控制在 6000 以内，留 2000+ 给生成**。

### 6.2 模型复用与上下文隔离（VirtualSession）

**核心设计：** 一个模型实例，多个"虚拟会话"，框架层管理每个会话的上下文生命周期。每个子模块拥有独立的 VirtualSession，互不干扰。

**VirtualSession 数据结构：**

```
@dataclass
class VirtualSession:
    session_id: str          # 会话唯一标识
    owner: str               # "brain.intent" / "brain.ooc" / "responder" / ...
    messages: list           # 这个会话自己的消息历史
    max_tokens: int          # 这个会话的上下文预算
    created_at: float
    last_used_at: float
```

**SessionManager 核心接口：**

```
class SessionManager:
    _sessions: dict[str, VirtualSession] = {}
    _model: Llama              # 唯一的模型实例

    def call(self, owner: str, messages: list, max_tokens: int = 4096) -> str:
        """模块调用入口"""
        session = self._get_or_create(owner, max_tokens)
        session.messages.extend(messages)
        session.messages = self._trim(session.messages, session.max_tokens)
        response = self._model.create_chat_completion(
            messages=session.messages, max_tokens=512
        )
        session.last_used_at = time.time()
        return response["choices"][0]["message"]["content"]

    def reset(self, owner: str):
        """清空某个模块的会话"""
        ...

    def _get_or_create(self, owner: str, max_tokens: int) -> VirtualSession:
        ...
```

**各模块调用示例：**

```
# Brain 内部 - 意图分析（独立上下文）
intent_result = session_manager.call(
    owner="brain.intent",
    messages=[{"role": "system", "content": "判断用户意图..."}, ...],
    max_tokens=2048
)

# Brain 内部 - OOC 检测（独立上下文）
ooc_result = session_manager.call(
    owner="brain.ooc",
    messages=[{"role": "system", "content": "检测以下回复是否OOC..."}, ...],
    max_tokens=2048
)

# Responder（独立上下文）
final_reply = session_manager.call(
    owner="responder",
    messages=[{"role": "system", "content": character_prompt}, ...],
    max_tokens=8192
)
```

**关键设计点：**

- **一个模型实例**，显存只占一份
- **每个 owner 有自己独立的消息历史**，互不干扰
- **框架负责裁剪**，超预算时自动丢弃最早的消息（或做摘要压缩）
- **模块代码完全不碰上下文管理**，只管发请求拿结果
- **可以按需 reset**，比如新一轮对话开始时清空所有会话

### 6.3 推理档位与上下文联动

| 档位 | Brain 上下文占用 | 说明 |
|------|----------------|------|
| LOW | ~200 tok | 日常寒暄，几乎不占上下文 |
| MID | ~500 tok | 一般对话 |
| HIGH | ~800 tok | 复杂剧情，从记忆预算借空间 |
| MAX | ~1200 tok | 重大剧情节点，慎用 |

### 6.4 推理档位自动路由

用一个轻量级任务分类器，根据输入复杂度自动选择推理档位：

```
输入 -> 分类器 -> LOW/MID/HIGH/MAX -> 对应推理链
```

分类器特征：

| 特征 | 倾向 HIGH/MAX | 倾向 LOW/MID |
|------|-------------|-------------|
| 问题长度 | 长 | 短 |
| 包含角色名 | 是 | 否 |
| 情感词汇 | 多 | 少 |
| 剧情关键词 | 有 | 无 |
| 多角色提及 | 是 | 否 |

### 6.5 记忆淘汰策略

采用**最早淘汰架构（Earliest Eviction）**：

- 当记忆存储空间达到上限时，淘汰最早写入且未被访问的记忆条目
- 配合重要性分数，高重要性条目可延长存活时间
- 定期做记忆压缩，将多条对话摘要为一段概要

---

## 7. 项目目录结构与命名规范

### 7.1 命名规范

**核心原则：目录名表示"这个子系统是什么"，文件名表示"这个文件做什么"。** 禁止出现目录名和文件名完全重复的情况（如 `memorizermemorizer.py`）。

| 当前（不规范） | 建议（规范） | 理由 |
|---------------|-------------|------|
| `memorizermemorizer.py` | `memorizerstore.py` | 该文件负责记忆的存储和检索 |
| `brainbrain.py` | `brainengine.py` | 该文件是推理引擎的核心调度 |
| `responderresponder.py` | `respondergenerator.py` | 该文件负责生成回复 |
| `eyeseyes.py` | `eyesperceiver.py` | 该文件负责感知和接收输入 |
| `healthhealth.py` | `healthmonitor.py` | 该文件负责监控 |

### 7.2 完整目录结构

```
gensokyoai/
├── utils/                    # L0 - 叶子层，零内部依赖
│   ├── __init__.py
│   ├── token_counter.py      # token 计数工具
│   ├── text.py               # 文本处理工具
│   └── logger.py             # 日志工具（LoggerManager）
│
├── schemas/                  # L0 - 数据契约层（跨层共享的 msgspec 结构体，纯叶子）
│   ├── __init__.py
│   ├── model_schema.py       # Message / ModelConfig / CompletionResult / ToolSpec
│   ├── scene_schema.py       # SceneSnapshot / SceneEvent
│   ├── memory_schema.py      # MemoryItem
│   ├── brain_schema.py       # BrainThinkEffort / BrainConclusion / OOCVerdict
│   ├── event_schema.py       # Topic / BaseEvent
│   ├── prompt_schema.py      # Prompt
│   └── health_schema.py      # HealthReport
│
├── prompts/                  # 提示词集中管理（PromptManager 装饰器注册，$var 渲染）
│   ├── __init__.py
│   └── manager.py            # 全部业务提示词模板注册于此，业务代码不写提示词
│
├── core/                     # L1 - 引擎内核（基建 + 与平台无关的业务流水线）
│   ├── __init__.py
│   ├── registry.py           # 注册表 + 装饰器
│   ├── event_bus.py          # 事件总线
│   ├── session_manager.py    # VirtualSession / SessionManager
│   ├── bootstrap.py          # 启动扫描
│   ├── config.py             # 配置加载
│   ├── brain/                # 决策：engine（含档位路由 route）/ ooc_detector
│   ├── responder/            # 表达：generator / style
│   ├── memorizer/            # 记忆：manager（级联检索）/ compressor
│   └── health/               # 监控：monitor
│
├── models/                   # L2 - 模型适配层
│   ├── __init__.py
│   ├── base.py               # ModelProvider / OpenAICompatProvider
│   ├── llama_cpp.py          # @Registry.register("llama_cpp") llama-server
│   └── qwen_local.py         # @Registry.register("qwen_local") 通用 OpenAI 兼容
│
├── eyes/                     # 边缘层 - 平台感知适配
│   ├── __init__.py
│   ├── perceiver.py          # Perceiver 协议 + ConsolePerceiver
│   └── parser.py             # 消息解析
│
├── roleplay/                 # 边缘层 - 角色扮演领域内容
│   ├── __init__.py
│   └── character.py          # CharacterCard 人设卡 + YAML 加载
│
├── extensions/               # 第三方/用户扩展（插件目录）
│   ├── __init__.py
│   └── ...
│
└── config/                   # 配置
    └── settings.yaml
```

> main.py 位于仓库顶层（包外），L4 入口：组装一切 + 编排主链路。gensokyoai/ 作为纯库包，不含入口。
>
> 结构原则：core = 与平台无关的引擎内核（基建 + 四大流水线子包）；eyes / roleplay / models 是可替换边缘层。
> **内核子包之间（core/brain、core/responder、core/memorizer、core/health）依然禁止互相直接 import**，
> 通信走事件总线或 L4 注入，否则 core 会退化成一锅巨石（原版 `_impl.py` 之前车）。

### 7.3 分层依赖规则

为避免循环导入，整个项目采用**单向依赖 + 树状分层架构**。依赖方向只能**从上往下**，禁止从下往上或横向依赖。

**依赖方向图：**

```
                    main.py（入口，组装一切）
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       eyes        brain      responder
          │           │           │
          │           ▼           │
          │      memorizer        │
          │           │           │
          └─────┬─────┴─────┬─────┘
                ▼           ▼
             core        models
                │           │
                └─────┬─────┘
                      ▼
                    utils
```

**分层定义：**

| 层级 | 模块 | 可以依赖 | 不能依赖 |
|------|------|----------|----------|
| **L0 叶子层** | `utils/`, `schemas/` | 标准库、第三方库 | 项目内任何模块 |
| **L0.5 提示词层** | `prompts/` | `schemas`, `utils` | 任何业务模块 |
| **L1 引擎内核** | `core/`（基建 + `brain`/`responder`/`memorizer`/`health` 子包） | `core` 顶层基建, `models`, `schemas`, `prompts`, `utils` | **内核子包之间零直接 import**；不依赖 eyes/roleplay |
| **L2 模型层** | `models/` | `core` 顶层基建, `schemas`, `utils` | 任何业务模块 |
| **边缘层** | `eyes/`, `roleplay/` | `core` 顶层基建, `models`, `schemas`, `prompts`, `utils` | 不依赖内核业务子包 |
| **L4 入口层** | `main.py`（仓库顶层） | 所有模块 | — |

**三条铁律：**

1. **只能向下依赖**：上层可以 import 下层，反过来不行
2. **业务子包之间零直接 import**：core/brain、core/responder、core/memorizer、core/health 互相禁 import，eyes/roleplay 也不得 import 内核业务子包；通信走事件总线（`core/registry.py`）或 L4 注入
3. **`utils` 是纯叶子**：不依赖项目内任何东西

**非法 import 示例：**

```python
# brainengine.py
from core.event_bus import emit       # ✅ 依赖 core（L1），合法
from core.registry import Registry    # ✅ 依赖 core（L1），合法

# 以下全部禁止：
from responder.generator import Generator   # ❌ 跨业务模块（L3 -> L3）
from memorizer.store import MemoryStore    # ❌ 跨业务模块（L3 -> L3）
from eyes.perceiver import Perceiver       # ❌ 跨业务模块（L3 -> L3）
```

**如何防止违反规则：**

**方法一：CI 检查（推荐）**

通过 AST 解析所有业务模块的 `.py` 文件，检查 import 语句是否指向禁止的横向模块：

```python
# teststest_no_circular_import.py
import ast
from pathlib import Path

FORBIDDEN_CROSS_IMPORTS = {
    "eyes": ["brain", "responder", "memorizer", "health"],
    "brain": ["eyes", "responder", "memorizer", "health"],
    "responder": ["eyes", "brain", "memorizer", "health"],
    "memorizer": ["eyes", "brain", "responder", "health"],
    "health": ["eyes", "brain", "responder", "memorizer"],
}

def test_no_cross_business_imports():
    """确保业务模块之间不直接 import"""
    for module, forbidden in FORBIDDEN_CROSS_IMPORTS.items():
        module_dir = Path(f"gensokyoai/{module}")
        for py_file in module_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            tree = ast.parse(py_file.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    import_str = (
                        node.module if isinstance(node, ast.ImportFrom)
                        else node.names[0].name
                    )
                    for f in forbidden:
                        assert f not in import_str, (
                            f"❌ {py_file} 非法导入 {f} 模块"
                        )
```

**方法二：`__init__.py` 里做 import guard**

在 `coreregistry.py` 中注入运行时检查，拦截向上层或横向的 import 尝试：

```python
# coreregistry.py 中注入 import hook
import sys

def check_layer_violation(importer: str, imported: str):
    """运行时检查依赖方向"""
    layer_map = {
        "utils": 0, "core": 1, "models": 2,
        "eyes": 3, "brain": 3, "responder": 3,
        "memorizer": 3, "health": 3,
    }
    importer_layer = layer_map.get(importer.split(".")[1], -1)
    imported_layer = layer_map.get(imported.split(".")[1], -1)
    
    if imported_layer > importer_layer:
        raise ImportError(
            f"依赖方向违规: {importer} (L{importer_layer}) "
            f"不能导入 {imported} (L{imported_layer})"
        )
```

---

## 8. 模块间数据流

### 8.1 主链路数据流

```
Eyes 产出场景快照（~500 tok）
    |
    v
Memorizer 检索 + 摘要（~1500 tok）
    |
    v
深思考档位（HIGH/MAX）？ --是--> Responder 垫一句角色过渡语
    |                        （同会话小 token 生成，冷却+概率门控，
    |                         掩盖接力思考延迟；正式回复可见、自然承接）
    否
    |
    v
Brain 接收 [System Prompt + 场景 + 记忆 + 历史]
  -> SessionManager 分配独立 VirtualSession
  -> 各子模块在各自独立上下文中推理（不污染主上下文）
  -> 输出结构化结论（~800 tok）
    |
    +-- OOC Verdict 前置检测（快速规则过滤）
    |
    v
事件总线（Event Bus）
    |
    v
Responder 接收 [System Prompt + 角色设定 + Brain结论 + 最近对话]
  -> SessionManager 分配 responder 会话
  -> 润色生成回复（~2000 tok 预留）
    |
    v
OOC 规则守门（零成本快筛；命中才一次纠偏重生成）
    |
    v
输出
    |
    +-- 后置 OOC 深审（异步侧链，不阻塞输出；结论回写角色状态与 ooc.rate 健康指标）
    |
    v
Memorizer 记录（对话 + 内心想法摘要）
```

### 8.2 事件总线数据结构

```
@dataclass
class BrainEvent:
    # Brain 通过事件总线传给 Responder 的结构化事件
    intent: IntentResult           # 意图判断结果
    emotion: EmotionState          # 情绪状态
    memory_refs: List[MemoryRef]   # 需要调用的记忆条目
    draft: str                     # Brain 产出的初稿回复文本
    confidence: float              # 置信度 (0.0 ~ 1.0)
    reasoning_level: str           # 使用的推理档位
    ooc_flag: bool                 # OOC 检测是否触发
    timestamp: datetime            # 事件时间戳
```

---

## 9. 预期运行行为

本章描述系统在真实运行场景下的完整行为流程，包括简单消息的快速路径、复杂消息的深度推理路径、后续对话的记忆接续，以及异步模型执行的时序特征。

### 9.1 场景一：简单寒暄（LOW 档位快速路径）

**用户输入：**「你好啊？」

**运行时序（总延迟约 850ms）：**

| 时间 | 模块 | 行为 | 耗时 |
|------|------|------|------|
| 0ms | Eyes | 收到 QQ 群消息事件，打包 SceneSnapshot | 2ms |
| 2ms | Brain | 意图分类 → LOW（简单寒暄，无需推理） | 50ms |
| 52ms | Brain | 输出极简结论：`{verdict: pass_through, emotion: 友好, intent: 回应打招呼}` | — |
| 54ms | 事件总线 | 广播 BrainConclusion 事件 | — |
| 54ms | Responder | 接收结论，在独立 VirtualSession 中生成回复 | 800ms |
| 54ms | Memorizer | 异步开始记录（不阻塞回复） | 200ms |
| 854ms | Responder | 输出最终回复 | — |
| 1054ms | Memorizer | 记录完成（用户已看到回复） | — |

**各模块上下文快照：**

- **Eyes 输出：**
  - scene_type: group_chat
  - sender: "小明"
  - content: "你好啊？"
  - context_snippet: 最近3条群消息（闲聊）
  - is_direct: false

- **Brain 结论（极简，约 50 token）：**
  ```json
  {
    "verdict": "pass_through",
    "emotion_hint": "友好",
    "intent": "回应打招呼",
    "draft": null,
    "reasoning": null
  }
  ```

- **Responder 上下文（干净，约 300 token）：**
  - system: 角色设定
  - user: "小明: 你好啊？"
  - assistant_hint: "友好地回应打招呼"

- **Memorizer 记录（异步）：**
  - 时间: 2026-09-04 19:22
  - 参与者: 小明
  - 内容: "你好啊？"
  - 角色回复: "嗯？哦，是你啊。有什么事吗？"
  - 情感标签: 友好/平淡
  - 重要性: low（打招呼，不值得深度记忆）

### 9.2 场景二：复杂问题（HIGH 档位深度推理）

**用户输入：**「你觉得这个世界的本质是什么？」

**运行时序（总延迟约 3-5 秒）：**

| 时间 | 模块 | 行为 | 耗时 |
|------|------|------|------|
| 0ms | Eyes | 收到消息，打包 SceneSnapshot | 2ms |
| 2ms | Brain | 意图分类 → HIGH（涉及世界观、哲学思考） | 50ms |
| 52ms | Brain | 调用 Knowledge Base 检索世界观设定 | 200ms |
| 252ms | Brain | 调用 Memorizer 检索角色之前聊过的相关话题 | 300ms |
| 552ms | Brain | 深度推理（独立上下文，~2000 token） | 1500ms |
| 2052ms | Brain | OOC Verdict 检测回复是否符合人设 | 300ms |
| 2352ms | Brain | 输出结构化结论 | — |
| 2354ms | 事件总线 | 广播 BrainConclusion 事件 | — |
| 2354ms | Responder | 接收结论，在独立 VirtualSession 中润色生成回复 | 2000ms |
| 2354ms | Memorizer | 异步开始记录 | 200ms |
| 4354ms | Responder | 输出最终回复 | — |
| 4554ms | Memorizer | 记录完成 | — |

**关键差异 vs 简单路径：**

| 维度 | LOW 路径 | HIGH 路径 |
|------|---------|----------|
| 推理档位 | LOW（意图分类） | HIGH（完整推理链） |
| Knowledge Base | 不调用 | 调用，检索世界观设定 |
| 记忆检索 | 不调用 | 检索角色相关历史话题 |
| Brain 上下文占用 | ~200 token | ~800 token |
| OOC 检测 | 跳过 | 执行前置快速过滤 + 后置深度审计 |
| 总延迟 | ~850ms | ~3-5 秒 |
| 用户体感 | 即时回复 | 稍等片刻（用户可接受） |

### 9.3 后续对话的记忆接续

**核心机制：三层记忆链路保证对话连续性。**

**第 1 轮：**「你好啊？」
- Responder 上下文：[角色设定 + 这一条]
- Memorizer 记录：mem_001

**第 5 轮：**「你还记得我最开始说了什么吗？」
- Responder 上下文：[角色设定 + 最近 5 轮原文]
- Brain 识别到需要回溯 → 调用 Memorizer 检索 → 找到 mem_001
- Brain 结论：「用户最开始说了'你好啊？'」
- Responder 生成：「你第一句不是跟我打招呼来着？」

**第 50 轮（长对话）：**
- Responder 上下文：[角色设定 + 前 45 轮摘要 + 最近 5 轮原文]
- Memorizer 里有完整的 50 条记忆，随时可检索
- 早期对话已被压缩为摘要，只保留关键信息

**三层记忆链路：**

| 层级 | 内容 | 作用 | 更新时机 |
|------|------|------|---------|
| Responder 滑动窗口 | 最近 N 轮原文 | 即时对话上下文 | 每轮追加，超窗则淘汰最早 |
| Memorizer 持久化 | 所有对话 + 内心想法摘要 | 长期记忆，按需检索 | 每轮异步写入 |
| 历史摘要 | 早期对话的压缩概要 | 超长对话的上下文补充 | 定期压缩 |

**上下文清空范围说明：**

- **Brain 子模块**：用完即弃（临时推理空间，不影响对话记忆）
- **Responder**：滑动窗口（保留最近几轮原文，不清空）
- **Memorizer**：永久存储（所有对话都记下来，受存储上限约束时做淘汰）
- **隔离的本质**：隔离的是临时推理空间，不是对话记忆

### 9.4 异步模型执行

**核心设计：一个模型实例 + 任务队列，异步不阻塞。**

模型推理是 CPU/GPU 密集型的阻塞操作，通过 `asyncio + 任务队列` 丢到后台跑，主事件循环保持畅通：

```
Brain 请求 ──┐
             ├──→ [任务队列] ──→ 模型实例（串行处理）──→ 结果回调
Responder ──┤
             │
Memorizer ──┘
```

**并发 vs 并行：**

| 方式 | 原理 | 延迟 | 吞吐量 |
|------|------|------|--------|
| 并发（单实例+队列） | 请求排队，模型逐个处理 | 累加 | 够用 |
| 并行（多实例） | 每个实例独立处理 | 取最大值 | 更高 |

**为何单实例完全够用：**

- 群聊场景天然串行：用户发消息、等回复、再发下一条，不存在并发压力
- 子模块有依赖关系：Brain 必须先跑完，Responder 才能接话，本身无法并行
- Memorizer 已异步：记录记忆不阻塞回复，用户感知不到延迟
- Qwen3.6-35B-A3B 推理极快：MoE 架构激活参数仅 3B，单次推理几百毫秒

**实现方案（asyncio + 任务队列）：**

```
class ModelWorker:
    - __init__: 加载模型实例 + 创建 asyncio.Queue
    - start: 后台循环，从队列取任务 → 调模型 → 回调结果
    - call: 异步提交任务，返回 future（不阻塞主线程）

各模块调用：
    result = await worker.call(messages, max_tokens)  # 异步不阻塞
```

**何时需要多实例：**

- 多用户同时发消息且要求低延迟
- 显存充足（如 24GB+ 显卡）
- Brain 和 Responder 无依赖可并行（当前架构有依赖，不适用）

### 9.5 性能预期总结

| 场景 | 预期延迟 | 上下文占用 | 用户体验 |
|------|---------|-----------|---------|
| 简单寒暄（LOW） | ~850ms | Brain 5% / Responder 35% | 即时回复，无感知延迟 |
| 一般对话（MID） | ~1.5-2s | Brain 15% / Responder 45% | 自然对话节奏 |
| 复杂剧情（HIGH） | ~3-5s | Brain 30% / Responder 55% | 稍等片刻，可接受 |
| 重大剧情（MAX） | ~5-8s | Brain 45% / Responder 65% | 明显等待，但内容深度高 |

**核心原则：** 简单的事快速过，复杂的事慢慢想，算力花在刀刃上。

---

## 10. 落地路线图

### Phase 1：最小可用版本

优先跑通主链路：

1. **Eyes** - 实现 QQ 群聊场景适配
2. **Brain** - 实现基础推理链（LOW / MID 两档）
3. **事件总线** - 实现 Brain -> Responder 的数据传递
4. **Responder** - 实现基础润色输出
5. **ModelProvider** - 实现至少一个模型接入（llama-cpp-python）
6. **SessionManager** - 实现 VirtualSession 基础功能

### Phase 2：能力增强

1. **Memorizer** - 实现分级记忆存储 + 最早淘汰
2. **OOC Verdict** - 前置规则过滤 + 后置模型审计
3. **Knowledge Base** - 基础知识检索
4. **推理档位自动路由** - 轻量分类器
5. **importlib 自动扫描注册** - Registry + auto_discover

### Phase 3：系统完善

1. **HealthCenter** - 全局监控 + 主动干预
2. **扩展系统** - 声明式装饰器 + 多种扩展类型
3. **上下文预算管理** - 精细化 token 分配 + 自动裁剪
4. **多场景 Eyes 适配** - 私聊、论坛等
5. **模型复用优化** - 并行/串行调用策略调优

---

## 11. 关键设计决策记录

| 决策 | 理由 |
|------|------|
| 不绑定特定模型 | 灵活切换，跟进新模型能力 |
| 不用模型自带 thinking | 应用层精确控制推理深度和 token 开销 |
| Brain 输出做压缩 | 避免挤占上下文，只传结构化结论 |
| 内心想法摘要存储 | 控制记忆模块膨胀 |
| 声明式装饰器扩展 | 符合开闭原则，按需加载 |
| importlib 自动扫描注册 | 类似 Spring ComponentScan，零配置热插拔 |
| 最早淘汰架构 | 简单有效，配合重要性分数可优化 |
| 事件总线解耦 | Brain 和 Responder 独立迭代 |
| Eyes 抽象感知层 | 屏蔽平台差异，方便扩展新场景 |
| OOC 前置 + 后置结合 | 快速过滤 + 深度审计兼顾性能和质量 |
| 上下文预算 75% | 留 25% 余量给模型生成 |
| 模型复用 + 上下文隔离 | 一个模型实例，多个虚拟会话，显存友好 |
| SessionManager 统一管理 | 模块不关心上下文管理，框架层统一裁剪 |
| VirtualSession 按 owner 隔离 | 每个子模块独立上下文，互不污染 |
| 8K 窗口对角色扮演够用 | 精打细算 + 摘要压缩 + 滑动窗口 |
| 工具循环在 Provider 层 | 无状态多轮，模块经 `SessionManager.call(tools=...)` 透传，无需关心协议细节 |
| 单向依赖分层架构 | 彻底杜绝循环导入，依赖方向一目了然 |
| 目录名=子系统，文件名=职责 | 避免命名重复，文件职责清晰 |

---
