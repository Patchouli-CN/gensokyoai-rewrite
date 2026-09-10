
# 更新日志

本文件记录项目的所有重要变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，
本项目遵循 [语义化版本](https://semver.org/spec/v2.0.0.html)。

## [0.0.16] - 2026/9/9

### 新增
 - **启用 `ReasoningStep`：思考轨迹留档**（该 schema 此前**全项目从未被构造过**，是死代码）
   - `_relay_think` 每轮解析成功后记录一条 `ReasoningStep`
     （`round` / `thought` / `need_continue_think` / `action_hint` / `intent` / `emotion` / `confidence`），
     随 `BrainConclusion.reasoning_steps` 带出
     - 记录的是**模型自述**的 `need_continue_think`；若随后因工具调用被强制续轮，
       循环层面的强制**不改动本步** —— 保留「模型当时怎么想」的原貌
   - 每轮追加一条可读日志：`思考第 N 轮: 意图=… 情绪=… 续轮=… 思考=…`
   - 新增 **`roleplay/trace.py` 的 `ReasoningTrace`**：每回合一行 JSONL，含逐轮步骤、
     **两份 reasoning**（工程实现 / 模型原生）与本回合回复；
     落 `<storage_dir>/traces/<session_id>.jsonl`，**按 5MB 轮转**保留上一代（`.1`）
   - `TouhouWorld(trace_steps=True)` 可整体关闭；留档失败只记日志，**绝不影响主链路**

### 测试
 - 新增 10 例：轨迹 JSONL 结构（含 steps 与两份 reasoning）、逐回合追加、超限轮转、
   关闭开关不写、写盘失败隔离、会话文件隔离；Brain 侧每轮一条 `ReasoningStep`、
   OFF 快速路径无步骤、世界跑一回合后落轨迹、开关生效
 - 全部 205 例全绿（ruff check / ruff format --check / mypy / pytest）

## [0.0.15] - 2026/9/9

### 变更
 - **`BrainConclusion` 把两种「思考」分开存**（此前混为一谈、且原生思考被直接丢弃）：
   - `_reasoning` = **工程实现**的思考：接力思考协议**收束轮**的 `thought`，
     对外经只读属性 **`reasoning`** 暴露 —— 既有访问点（如测试断言）**无需改动**
   - `_raw_reasoning` = **模型原生 thinking**：逐轮拼接，`think: true` 时才有，
     对外经只读属性 **`raw_reasoning`** 暴露
   - `_relay_think` 此前只读 `result.content`、**从不读 `result.reasoning`**，
     原生思考段白丢；现在会逐轮收集
   - `BrainConclusion` 不参与序列化（仅进程内传递），故 `_` 私有名不会漏进文件

### 测试
 - 新增 3 例：两种思考默认 None 且互不污染、工程实现与原生思考分别落到各自字段、
   多轮接力时原生思考逐轮拼接而工程实现只留收束轮
 - 全部 195 例全绿（ruff check / ruff format --check / mypy / pytest）

## [0.0.14] - 2026/9/9

### 新增
 - **会话恢复补全：角色状态与世界跨回合状态入档**（`_SCHEMA_VERSION` 1 → 2）
   - 新增 **`StateCodec` 协议**：把「额外存什么」下放给装配层，`SessionPersister` 只负责编排，
     不必知道 `Character` / `TouhouWorld` 的内部字段
   - **`CharacterStateCodec`**：情绪 / 对话欲 / 出戏计数（`status.extra`）随会话存取；
     **恢复时校验角色名** —— 换了角色却复用同一 `session_id` 时拒绝把旧角色的状态套上来
   - **`_WorldRuntimeCodec`**：OOC 干预抬高的 `effort_floor`、过渡语冷却 `stall_last_turn`、蒸馏计数 `distill_counter`
     - **不持久化 `stall_last_time`**：它取 `time.monotonic()`，跨进程没有意义；
       恢复时置为「现在」，等价于重启后重新计时最小间隔（比存一个会误导的数值正确）
   - codec 恢复**逐个异常隔离**，单个失败不影响其他 codec 与主流程；
     旧档（v1）缺段时 `load(None)` 静默跳过 → **向后兼容**

### 说明：为什么不持久化「思考的中间状态」
 - 接力思考的中间态（上一轮 `thought` / 已得行动指令 / 轮次）是 `_relay_think` 内的**局部变量**，
   随调用结束即消失 —— 它属于「**一次计算的进行时**」，而不是「重启该接着用的状态」
 - 进程若在回合中途被杀，那一回合本就**未产出回复**，正确做法是**重跑该回合**
   （Brain 无状态是刻意设计，见架构文档 §11）；把半截思考持久化后「续上」会引出
   「用户消息是否已入记忆 / 是否补发回复」这类一致性问题，**收益为零、复杂度为正**
 - 附注：`schemas/brain_schema.py` 的 `ReasoningStep`（自称「单轮思考的中间状态」）
   **全项目从未被构造过**，是死代码。本次未动它 —— 它对应的是另一件事：
   可选的「思考轨迹留档」（可观测性），而非状态恢复

### 测试
 - 新增 6 例：角色状态往返、换角色拒绝套用、旧档/非法字段容错、codec 参与快照与恢复、
   单个 codec 失败隔离、世界重启恢复角色状态与跨回合旋钮
 - 全部 192 例全绿（ruff check / ruff format --check / mypy / pytest）

## [0.0.13] - 2026/9/9

### 修复
 - **`SessionPersister.flush()` 改为幂等**（会话恢复的静默丢数据隐患）：
   关闭流程的顺序是「先写最终快照 → 再 `sessions.reset_all()` 清空会话」。
   若之后**再 flush 一次**，就会拿**已清空**的状态覆盖刚写好的存档 ——
   快照里的 `responder_messages` 被写成空数组，**重启后对话历史全丢**（只剩工作记忆）。
   真实应用只 flush 一次所以没踩到，但调用方多 flush 一次（或二次关闭）就会静默丢数据。
   现已幂等：重复调用是空操作，并在日志里说明跳过。

### 测试
 - 新增 1 例：**重复 flush 不覆盖已有存档**（模拟「清空会话后再 flush」这一真实关闭顺序）
 - 全部 186 例全绿（ruff check / ruff format --check / mypy / pytest）

## [0.0.12] - 2026/9/9

### 变更
 - **落盘 JSON 改为缩进输出**：`JsonFilePersistence` 新增 `indent` 参数（默认 2 空格），
   落盘文件带缩进与末尾换行，便于人读与 diff；`indent=None` 可退回紧凑单行。
   此前 `session.json` 挤成一行没法看。
   - 实现要点：`msgspec.json.encode()` 只出紧凑格式，而且 **`encode` 与 `Encoder` 都不接受 `indent`**；
     要美化还得再过一道 `msgspec.json.format(content, indent=N)` —— 这才是 msgspec 的美化入口

### 测试
 - 新增 3 例：默认缩进且以换行结尾、`indent=None` 退回紧凑单行、缩进输出不改变读回语义
 - 全部 185 例全绿（ruff check / ruff format --check / mypy / pytest）

## [0.0.11] - 2026/9/9

### 新增
 - **装配入口收进包内 + console 入口（让「package 方式调用」真正可用）**：
   此前装配逻辑写在**仓库根目录**的 `main.py` 里，而 wheel 只打包 `gensokyoai/` ——
   `pip install` 之后既没有可执行命令、配置文件也不在包里，**装完根本跑不起来**。
   - `gensokyoai/app.py`：`build_session_and_character()`（会话 + 角色）、`build_world()`（完整世界），
     装配路径可被 CLI / WebSocket 后端 / 测试复用
   - `main(argv)`：控制台 CLI（`--config` / `--character` / `--session` / `--log-level` / `--log-file`），
     `[project.scripts]` 的 `gensokyoai` 指向它；资源缺失时返回退出码 2 并给可读提示，不抛裸栈
   - `resolve_resource()`：**优先工作目录**（仓库内开发用可随意改的 `config/`），
     **回落包内自带资源**（安装后），返回绝对路径
 - **`[project.scripts]`**：`gensokyoai`（控制台）与 `gensokyoai-ws`（WebSocket 多路频道）
 - **资源打包**：`[tool.hatch.build.targets.wheel.force-include]` 把仓库根 `config/`
   映射进 wheel 的 `gensokyoai/_resources/config/` —— **单一真相来源仍在仓库根**，不复制第二份
 - WebSocket 后端新增 `main()`（`--host` / `--port` / `--idle-ttl` / `--config` / `--character`），
   装配复用 `app.build_session_and_character()`，去掉原先复制的一份装配逻辑
 - dev extras 增 `build` + `hatchling`（可本地构建 wheel）

### 变更
 - `main.py` 变为薄壳（装配已收进包）：`from gensokyoai.app import main`

### 测试
 - 新增 9 例：资源解析优先工作目录且返回绝对路径、**回落包内自带资源（模拟安装后）**、
   缺失时报可读错误、会话+角色装配齐全、世界装配注入 IO 与 session_id、默认控制台 IO、
   CLI 默认值与覆盖、资源缺失返回退出码 2、WS 入口参数与复用
 - 全部 182 例全绿（ruff check / ruff format --check / mypy / pytest）

## [0.0.10] - 2026/9/9

### 变更
 - **架构债：合并重复的模型配置结构体** —— `schemas.model_schema.ModelConfig` 与 `core.config.ModelSettings` 是两份**近乎重复**的结构体（9 个同名字段），装配时把后者传给声明收前者的 `provider.config()`，靠**鸭子类型**蒙混过关。现合并为**单一 `ModelConfig`**：
   - 定义落在 **L0 契约层**（`schemas`）—— 跨层共享的数据本就该在此，`core/config.py` 只负责组合进 `GensokyoConfig`，`models/` 直接类型对齐
   - `session_factory._create_backend(settings: ModelConfig)` 类型正确，鸭子类型消除
 - **清掉两个零消费者死字段**：旧 `ModelSettings.reserve_for_output`（输出预留已由每次调用的 `max_new_tokens` 承担）与旧 `ModelConfig.extra`
 - **`context_window` 从死字段接活**：此前声明了却**无人读取**，`VirtualSession.max_tokens` 硬编码 8192（文档说 32K 也白写）。现在打通：
   - `SessionManager(default_context_window=...)` —— 新建会话的默认预算
   - `set_context_window(owner, tokens)` —— 逐 owner 覆盖（非法值忽略）
   - `register_backend(..., context_window=...)` —— 装配层一行下发
   - 于是 `settings.yaml` 里的 `context_window: 32768` **真的生效**（验证：responder 会话预算 = 32768）

### 修复
 - `config/settings.yaml` 移除已删除的 `reserve_for_output` 键（该键本就从未被读取）

### 测试
 - 新增 8 例：模型配置单一契约、YAML 键名匹配时真的生效、键名写错被静默忽略（固化上一版 bug 的成因）、仓库 settings.yaml 键名正确、装配层下发 `context_window`、默认预算生效、`set_context_window` 覆盖与非法值忽略
 - 全部 173 例全绿（ruff check / ruff format --check / mypy / pytest）

## [0.0.9] - 2026/9/9

### 新增
 - **统一工具执行器 `core/toolkit.py`**：`ToolExecutor` 成为全项目**唯一**的工具执行点 —— 此前 `models/base.py._execute_tools`（Provider 工具循环）与 `core/brain/engine.py._execute_tool_calls`（接力思考循环）**各写了一遍**执行逻辑，必然漂移。同时补齐四件事：
   - **结果截断**（`max_result_chars`）：工具返回大字符串会撑爆上下文窗口，超长一律截断标注
   - **超时**（`timeout`）：工具不再能永久挂住调用链
   - **同步工具下线程**：`asyncio.to_thread` 执行同步工具，不再阻塞事件循环
   - **结构化错误**：`ToolResult(ok/content/error/elapsed_s)` 取代裸字符串
 - **内置工具 `gensokyoai/tools/`**：`get_current_time` / `get_current_dateinfo`（七曜日）/ `get_moon_phase`（八相月），`@ToolRegistry.tool` 装饰器注册；`bootstrap.DEFAULT_PACKAGES` 加入 `gensokyoai.tools`，启动即自动注册
 - **`ToolSpec.name` / `tool_name`**：统一「注册名与函数名不一致」（`ToolRegistry` 支持 `name=` 覆盖，但 `register_external`/OpenAI 声明一律用 `__name__`）；OpenAI 声明、prompt、执行器索引统一走 `tool_name`
 - **HealthMonitor 重构**（补齐「监控 **+ 主动干预**」的后半截）：
   - **阈值带方向**：`MetricThreshold.lower_is_worse` 区分「越高越坏 / 越低越坏」
   - **边沿触发 + 冷却**：指标持续超限不再每回合刷告警、不再反复触发干预
   - **聚合摘要** `MetricSummary`：报告给 count/last/avg/min/max/p95，替代把上百条原始样本全 dump
   - **主动干预** `register_intervention(metric, handler)`：超限时回调；回调由**装配层**注册，core/health 不反向依赖 memorizer/roleplay，分层铁律不破
   - 推理档位分布聚合（`effort.*` → `reasoning_distribution`）
 - **SessionManager 计量**：`token_usage(owner)` / `total_usage()` / `context_usage(owner)` / `owners()`，`call` 与 `call_stream` 累计 token
 - 配置：`ModelConfig` / `ModelSettings` 增 `tool_timeout` 与 `tool_max_result_chars`

### 变更
 - **`TouhouWorld` 接线主动干预**：上下文占用超 80% → 触发一次记忆蒸馏腾窗口；OOC 出戏率超 0.5 → **抬高推理档位下限到 HIGH**（审计恢复健康后自动撤销，不长期烧算力）
 - 每回合向健康喂 `turn.tokens`（累计用量差值）与 `session.context_usage`；注入 `session_provider` 供报告采集

### 修复
 - **`config/settings.yaml` 键名不匹配**：原先写 `model:`，而 `GensokyoConfig` 字段是 `default_model:`，msgspec `strict=False` **静默忽略未知键** —— 等于配置文件什么都没配（能跑是因为默认值恰好一致）。已改正并补全 `brain` / `responder` / `resource` 段
 - HealthMonitor 移除**函数内 `import psutil`**（违反 CONTRIBUTING「不允许函数内导入」，且 psutil 并非依赖，该分支永远返回 error）
 - HealthMonitor `_get_session_summary()` 从写死的 `{"total": 0, "active": 0}` stub 改为注入式 provider

### 测试
 - 新增 24 例：工具执行器（同步/异步执行、未知工具、坏 JSON 拒绝、异常分类、结果截断、超时、**同步下线程不阻塞事件循环**、批量一一对应、命名覆盖、内置工具注册与调用）、健康监控（阈值双向、冷却抑制、恢复重告警、干预触发与异常隔离、聚合摘要、报告含分布与注入、总线广播）、SessionManager 计量（累计用量、上下文占用率、owners）
 - 全部 165 例全绿（ruff check / ruff format --check / mypy / pytest）

## [0.0.8] - 2026/9/9

### 新增
 - **资源闸门与限流**（`core/resource.py` + `schemas/quota_schema.py`）：设计锚点是「本地单模型是串行稀缺资源，多路的本质是排队+路由而非并行」——
   - `ResourceGate`：全局并发 + 每租户速率（rpm）/ 每日调用 / 每日 token 配额，超限抛 `QuotaExceeded`（带 `retry_after`），不静默丢弃
   - `IngressLimiter`：入口令牌桶，按用户早拒（不占模型资源）
   - `GatedBackend`：包住 `ChatBackend`，把每次调用汇入闸门（**装配处一行接入**，对 SessionManager 完全透明）
   - `tenant_scope()`（contextvars）：世界任务包在 `tenant_scope(channel_id)` 里，任务内模型调用自动归属租户，**无需把身份穿透 SessionManager**
   - `ResourceSettings` 配置段 + `build_resource_gate()`；`build_session_manager(config, gate=None)` 可选接线
 - **多路频道中枢**（`roleplay/hub.py`）：`ChannelHub` 以「频道 = 场景 = 世界」为单位做多路复用与世界注册表，连接挂载/退订、频道空闲 TTL 回收（走世界自身优雅关闭，落盘记忆与会话）；`WorldFactory` 可注入，默认工厂每频道一个 `session_id`（持久化天然隔离）
 - **`eyes/queue.py` `QueuePerceiver`**：N 路输入合流成一条快照流（多路复用的输入接合点），带背压保护与停止唤醒
 - **`mouth/broadcast.py` `BroadcastMouth`**：输出广播给频道所有在线连接（多路复用的输出接合点），支持流式 `begin/delta/end` 逐帧广播
 - **WebSocket 服务后端**（`backends/ws_server/`）：aiohttp 入口 `/ws/{channel}` 与 `/ws`，`WsSink` 作为连接订阅者，入口限流超速回 `notice` 帧；`serve()` 非阻塞启动、`_demo()` 可直接跑

### 变更
 - `GensokyoConfig` 增 `resource` 配置段
 - `build_session_manager` 支持传入资源闸门，逐个 backend 包 `GatedBackend`

### 测试
 - 新增 27 例：资源闸门（速率窗口/每日预算/被拒不计数/全局串行/租户记账/流式兜底）、频道中枢（多路合流/广播一致/频道隔离/空闲回收/端到端默认世界）、队列感知器（取用/停止/满队丢弃）、广播口（逐帧/失败摘除）、WS 服务（路由/投递/真实 TestServer 端到端/入口限流 notice）
 - 全部 141 例全绿（ruff check / ruff format --check / mypy / pytest 四项通过）

## [0.0.7] - 2026/9/9

### 新增
 - **工程护栏**：新增 `pyproject.toml`（项目元数据 + 依赖 + `[tool.ruff]`/`[tool.mypy]`/`[tool.pytest.ini_options]`）与 `.github/workflows/ci.yml`（ruff check + ruff format --check + mypy + pytest）；全代码库一次性 `ruff format` 归一、`ruff check` 清零、`mypy` 清零（16 处类型债修掉）；pytest 配置从 `pytest.ini` 并入 `pyproject`。`requires-python = ">=3.14"`（代码用了 `uuid.uuid7()`）
 - **口层（输出投递层）**：与 `eyes` 输入层对称的 `gensokyoai/mouth/` —— 眼睛看（eyes），脑子想（brain），嘴巴说（mouth）。`Mouth` 抽象（`send` 完整投递 + `begin`/`delta`/`end` 流式接口，基类提供缓冲兜底），内置实现 `ConsoleMouth`（控制台逐块即时打印，`supports_streaming=True`）作为默认
 - **主动发言优化**：`CharacterCard` 新增 `expression_base`（表达欲基线/话痨度 0~1），`evaluate_initiative` 用它替代写死的 0.5；新增 `describe_silence()`（零 token）按「悬着问题 / 长时间沉默 / 普通」三档生成自然的冷场环境描述，替换主动发言里写死的「（周围安静下来了）」——幽幽子卡设为 0.7
 - **提示词双模渲染**：`Prompt` 支持原生式 `renderer`（接收 `**params` 的 Python 函数），`PromptManager.prompt` 按 `inspect.signature` 自动识别——无参=数据式（`$var` + `string.Template`），有参=原生式（直接拼字符串，条件/循环/分支全程原生 Python）；现有模板全部转为原生 Python 函数
 - **模型流式能力（投递层地基层）**：`models/base.py` 的 `ModelProvider` 新增 `supports_streaming`（默认 False）与 `chat_stream()`（基类兜底实现整段一次性产出，子类覆盖做真流式）；`OpenAICompatProvider` 落实真 SSE 流式 `chat_stream()`（`_iter_sse_events` 逐块解析 `data:` 事件，测试可覆写），末块附 `finish_reason` 与累计 `usage`
 - `schemas/model_schema.py` 新增 `StreamEvent`（正文块 `delta` + 末块 `finish_reason`/`usage`），并在 `schemas/__init__.py` 导出
 - `_build_payload` 增加 `stream` 参数：**缓冲路径恒 `False`（不受配置影响），仅投递层 `chat_stream()` 置 `True`**
 - `models/llama_cpp.py`：`_build_payload` 兼容 `stream` 参数并保留 `think=False` 的模板思考开关

### 变更
 - **控制台真流式**：`SessionManager` 新增 `call_stream()`（路由到 `backend.chat_stream`，后端无 `chat_stream` 时回退 `chat()` 单块），`Responder` 新增 `respond_stream()`（逐块 yield 文本，含半截续写流式拼接与情绪润色尾缀）；`TouhouWorld` 抽出统一的 `_express()` 表达+投递入口（**主循环与主动发言共用**），按 `mouth.supports_streaming` 分流——流式走 `_deliver_stream`（`mouth.begin/delta/end` 逐块显示，OOC 预审因见不到完整文本而跳过、靠后置深审兜底），非流式保持原 `respond()`+OOC 守门+`send()`
 - **接力思考上限收紧（防打转）**：`_get_max_rounds` 从 LOW=4/MID=8/HIGH=12/MAX=999 收紧为 LOW=2/MID=3/HIGH=5/MAX=8 —— MAX 不再近乎无限打转，简单档缩短等待
 - `TouhouWorld` 输出改为走 `mouth`（新增可注入 `mouth: Mouth | None = None`，缺省 `ConsoleMouth`）：主回复 / 开场白 `_greet`（改 async）/ 过渡语 `_maybe_stall` / 主动发言 `_try_speak` 的 `print(...)` 全部替换为 `await self.mouth.send(...)`
 - `main.py` 注入 `ConsoleMouth()`（与 `eye` 对称）
 - `ModelConfig.streaming` 原本是「死字段」（配置里有、Provider 却硬编码 `stream: False`），现经 `OpenAICompatProvider.supports_streaming` 接活为「该后端是否允许流式投递」的真实开关
 - 内部模块（brain/ooc/记忆压缩）仍走 `chat()` 缓冲路径，一行未改；流式仅供投递层（给用户看的那一条）使用

### 修复
 - `utils/logger.py`：`LoggerManager._cache: dict[str, loguru.Logger]` 的注解改为惰性字符串 —— loguru 0.7.3 不再公开 `loguru.Logger`（仅私有 `_Logger`），原注解在模块导入时抛 `AttributeError`，导致整个包不可 import（此问题先于本次改动存在）
 - `conftest.py`（新增）：修复 `tmp_path` 用例在受限沙箱/CI 报 `PermissionError` 的问题 —— 覆写 `tmp_path` 夹具，把测试临时目录建在工作区内 `.ws_pytest_tmp`（`os.makedirs` 默认 mode，可写；绕开 `tempfile.mkdtemp` 的 0o700 只读 mode 与 pytest 默认写到系统 Temp 的限制），用后即删

### 测试
 - 新增口层用例：基类流式缓冲兜底 / ConsoleMouth 支持流式 / send 完整打印 / 流式逐块效果；新增模型流式用例：SSE 事件逐块解析与末块 finish/usage、基类 `chat_stream()` 兜底整段、`supports_streaming` 反映配置、`chat()` 缓冲路径 `stream=False`、LlamaProvider 流式保留 `think` 开关；非 tmp_path 用例 79 例全绿
 - `test_world_persistence_e2e` 的 `_EchoBackend` 升级为贴合当前架构：对 brain.think 接力思考返回合法 JSON（need_continue_think=false，一轮收尾，不再烧轮次）；最终回复用独立 reply 计数器，与大脑接力调用解耦，使「回复1/回复2」断言成立 —— 该测试此前因假后端未跟上接力思考协议而断言失败（非业务回归）
 - 新增流式投递用例：call_stream 走真流式逐块 + 末块 finish/usage、后端无 chat_stream 时回退 chat() 单块、respond_stream 逐块产出 + 情绪「愤怒」尾缀「！」、半截续写流式拼接、`_express` 流式投递（主动发言同款路径，控制台逐块显示）；brain 接力思考上限封闭断言（LOW=2 / MAX<=8）
 - 全部 114 例全绿（含此前受沙箱 tmp_path 限制的持久化用例）

## [0.0.6] - 2026/9/9

### 新增
 - **深思考过渡语**：HIGH/MAX 档（多轮接力思考，延迟肉眼可见）在 Brain 思考前让 Responder 以角色口吻先回一句过渡语（如"唔……让我想想"）；三重门控防人机感——冷却轮数（默认 3 回合）+ 时间间隔（默认 180s）+ 概率掷骰（默认 0.6）。过渡语与正式回复共用同一个 responder 有状态会话：先入历史，正式回复能看到自己说过它、自然承接不重复；小 token（48）+ 高温度秒回，只投递显示层不写记忆
 - **最终回复 OOC 守门**：Responder 生成后先过零成本规则快筛，命中自曝式话术才花一次纠偏重生成（`responder.correct` 指令，告知坏回复与原因后重新以角色身份回应）；纠偏后仍命中则原样输出，不死循环
 - **后置 OOC 深审接线**：`OOCDetector.audit()` 此前无人调用，现在回复发出后异步深审（不阻塞热路径，代际令牌防迟到回写）；结论回写角色状态（`ooc_audited`/`ooc_hits`）并喂 `ooc.rate` 健康指标——滚动出戏率超过 0.5 时 HealthMonitor 自动告警
 - 提示词模板 `responder.stall`（过渡语）/ `responder.correct`（纠偏重生成）
 - Responder 新增 `stall(snapshot)` / `correct(bad_reply, reason)`
 - `roleplay/__init__.py` 导出 `TouhouWorld`（此前包外无法 import）

### 变更
 - TouhouWorld 新增旋钮：`stall_probability` / `stall_cooldown_turns` / `stall_min_interval` / `ooc_retry` / `ooc_audit`（均可关闭对应行为）；OOCDetector 提为实例属性 `self.ooc` 便于替换/测试
 - 架构文档 §8.1 主链路数据流同步（过渡语分支 + OOC 守门/深审节点）

### 测试
 - 77 个测试全绿：新增过渡语门控（档位/首回合/冷却/时间间隔/概率开关/失败吞掉）、OOC 守门（放行零调用/纠偏/仍坏保留原文/开关）、后置深审（状态回写/指标喂食/代际令牌/异常隔离）13 例

## [0.0.5] - 2026/9/9

### 新增
 - **主动发言（对话欲）** `roleplay/initiative.py`：原版四维对话欲的零 token 规则化移植 —— expression（表达欲）/ emotional（情绪唤起）/ relational（关系牵引：点名、@）/ situational（情境时机：悬而未决的问题 + 空闲时长）加权求和，权重来自角色卡 `motivation_weights`；TouhouWorld 后台定时评估，空闲超阈值且对话欲达标时绕过 Brain 直接驱动 Responder 开口（省思考 token）
 - **记忆蒸馏侧链**：每 N 回合（默认 10）取最早一批工作记忆（默认 8 条，跳过核心保护项），经 Compressor 压成摘要条目（importance=0.7，入库自动落长期记忆）后遗忘原文 —— 控制工作记忆膨胀，保留剧情脉络
 - **半截续写** `Responder`：`finish_reason=length` 时自动发起一次接续生成并拼接完整回复（小模型长回复常见截断的兜底）
 - **开场白**：主循环启动时打出角色卡 `greeting`
 - **代际令牌**：TouhouWorld 关闭/重置时代际 +1，在途后台任务（蒸馏/主动发言）回写前校验，杜绝迟到写入污染新会话（原版 GenerationGuard 的移植）
 - **健康喂食**：回合粒度记录档位分布 / 记忆规模 / 回合延迟到 HealthMonitor
 - MemoryManager 新增 `oldest(n)`（FIFO 选取，跳过核心项）与 `forget(ids)`（蒸馏后清理原文）

### 变更
 - TouhouWorld 主循环重构：用户回复走 `_busy` 互斥（避免主动发言并发抢占 responder 会话）；记忆写入改为异步侧链不阻塞回合；投递档输出清洗控制字符（记忆档保留原文）；每回合把 Brain 结论的情绪同步进 `CharacterStats`
 - 记忆写入的会话内触发由 `await` 改为 `create_task`（回合延迟不再含记忆落盘）

### 测试
 - 64 个测试全绿：新增主动发言评估（6 例）、Responder 半截续写与情绪润色（3 例）、MemoryManager 蒸馏流程（4 例）

## [0.0.4] - 2026/9/8

### 新增
 - **命令子系统** `gensokyoai/command/`：标签（`<tag ...>`）与前缀（`/cmd`）双模式解析（`CommandParser`/`ParsedCommand`/`CommandType`），实例级注册表装饰器 `@command`（自动生成 usage、按签名解析参数），执行器 `CommandExecutor`、权限分级 `PermissionLevel`、结果对象 `CommandResult`
 - **生命周期管理** `core/lifecycle.py`：`LifecycleManager` 启动顺序执行 / 关闭逆序执行回调（资源栈语义），经 EventBus 发布 `STARTUP`/`SHUTDOWN` 事件，支持 `request_stop` 供信号处理器调用
 - **多模型路由** `core/session_factory.py`：`build_session_manager` 按配置装配 —— `default_model` 兜底，`brain`/`responder` 独立配置，`ooc`/`memorizer` 可选（缺省回落 brain），实现"Brain 用 DeepSeek、Responder 用 Kimi"式的按模块选模型
 - **全局工具注册表** `core/registry.py` 新增 `ToolRegistry`：`@ToolRegistry.tool` 装饰器注册并自动包装 `ToolSpec`，支持外部 `ToolSpec` 注入
 - **角色扮演主循环** `roleplay/loop.py`：`TouhouWorld` 统一组装 Eyes/Brain/Responder/Memorizer/Health/Lifecycle 与工具集，main.py 精简为纯装配入口
 - **长期记忆持久化** `core/memorizer/store.py`：`LongMemoryStore` JSON 文件落盘 + 按主题检索
 - **健康监控增强**：指标记录与历史查询（`HealthMetric`）、阈值告警（`HealthAlert` 事件广播）、记忆与会话占用摘要
 - 首个角色卡 `config/roles/SaigyoujiYuyuko.yaml`（西行寺幽幽子）

### 变更
 - **SessionManager 改为无参构造 + 多模型路由**：`register_backend(owner, backend)` / `set_default_backend(...)`，每个 owner 可绑定独立 ChatBackend；新增 `normalize_tool_calls` 代理
 - **Brain 推理升级为「接力思考」协议**：`brain.think` 提示词改为多轮 JSON 协议（`thought`/`intent`/`emotion`/`action_hint`/`confidence`/`need_continue_think`），`_relay_think` 按档位映射最大轮数（LOW=4 / MID=8 / HIGH=12 / MAX=999），前轮思考作为上下文接力；工具调用在思考循环内执行并强制消化（工具结果未消化则追加一轮）；`draft` 语义改为给 Responder 的行动指令
 - **工具调用格式标准化下沉 Provider**：`ModelProvider.normalize_tool_calls()` 抽象，LlamaProvider 实现从解析后的 JSON（`tool_calls` 字段）或思考文本中提取工具调用 —— 小模型不吐原生 tool_calls 时的兜底
 - MemoryManager 全面异步化：`store`/`recent`/`cascade_retrieve` 为 async，`recent` 支持关键词过滤；工作记忆 + 长期记忆双层（`work_mem_size`/`long_mem_size`）
 - `Topic` 更名 `EventTopic`，新增 `STARTUP`/`SHUTDOWN`/`STOP_REQUESTED`；EventBus 新增 `on()` 装饰器订阅
 - `roleplay/character.py`：新增 `Character`（包装 CharacterCard + `CharacterStats` 运行时状态 + 唯一 cid），`load_character` 返回 `Character`；示例对话结构化 `Dialogue`
 - eyes 感知器基类抽到 `eyes/base.py`，支持 `request_stop` 优雅退出

### 修复
 - 模型工具调用相关 bug 修复（配合 normalize_tool_calls 标准化链路）

### 测试
 - 51 个测试全绿：全部用例适配新 API（SessionManager 多 backend 构造、接力思考协议、Character 包装、PromptManager 新模板），新增多模型路由与缺 backend 报错用例；roleplay 作为装配层豁免跨模块 import 禁令（但反向依赖仍被禁止）

## [0.0.3] - 2026/9/7

### 新增
 - 模型工具调用（OpenAI function calling）全链路支持：
   - `ToolCall` 数据契约；`Message.tool_calls`（assistant 回传）/ `CompletionResult.tool_calls`（模型请求）
   - `ToolSpec.to_openai_tool()`：从函数签名自动推导 JSON Schema（标注类型映射 + 无默认值参数进 required）
   - `OpenAICompatProvider.chat(tools=...)` 多轮工具循环：请求 tool_calls -> 执行 -> 回填 tool 消息 -> 复请求，直至模型收尾或达轮数上限（`_MAX_TOOL_ROUNDS=8`）；多轮 token 用量累计入结果
   - 同步 / 异步工具自动分发（`invoke` / `ainvoke`）；未知工具、参数 JSON 损坏均兜底为错误文本回传模型，不崩调用链
   - 消息序列化支持 `assistant.tool_calls`；LlamaProvider 思考段分离时保留 `tool_calls`
   - 工具执行日志：工具名、参数、结果预览（截断 80 字）

### 测试
 - 48 个测试全绿：新增 schema 推导 / tool_calls 序列化与响应解析 / 工具循环（同步 + 未知工具 / 异步工具 / 参数损坏降级）

## [0.0.2] - 2026/9/7

### 新增
 - Phase 1 主链路端到端可用：Eyes(控制台) -> Brain -> Responder -> Memorizer，`python main.py` 即可对话
 - `models/base.py`：`ModelProvider` 抽象 + `OpenAICompatProvider` 通用 OpenAI 兼容 HTTP 基类
 - `models/llama_cpp.py`：`LlamaProvider`（llama-server 专用，经 Registry 注册）
 - `core/session_manager.py`：`SessionManager.call` 支持无状态（Brain 用完即弃）/ 有状态（Responder 滑动窗口）双模式，按 token 预算裁剪，system 前缀固定保留（利于 KV cache 复用）
 - `core/brain/engine.py`：四档推理（OFF 零 token 快速路径 / LOW / MID / HIGH / MAX），规则档位路由 `route()`，模型 JSON 输出容错解析，失败自动降级快速路径
 - `core/brain/ooc_detector.py`：OOC 前置规则快筛（零 token）+ 后置模型深审
 - `core/responder/generator.py`：Responder 表达层实现 + 情绪标点润色 `style.py`
 - `core/memorizer/`：`MemoryManager` 记忆图谱级联检索、最早淘汰 + 重要性保护、`recent()` 轻量检索；`Compressor` 记忆摘要压缩
 - `core/health/monitor.py`：HealthCenter 指标聚合实现
 - `eyes/perceiver.py`：`ConsolePerceiver` 控制台场景适配器；`eyes/parser.py`：OneBot11 / 通用字典消息解析
 - `roleplay/` 新子模块：`CharacterCard` 人设卡（字段兼容原版角色卡 YAML）+ `load_character()`
 - `prompts/` 提示词集中管理：`PromptManager` 装饰器注册 + `$var` 渲染 + 缓存，业务代码不再内联提示词
 - `core/event_bus.py` 事件总线（订阅者异常隔离）、`core/registry.py` 扩展注册表 + `auto_discover`、`core/config.py` YAML + msgspec 强类型配置
 - 请求全链路日志：档位路由 / Brain 思考 / SessionManager 调用（含耗时、token、历史变化）/ OOC / Responder / Provider（tok/s 速度、非 200 响应体）/ 回合汇总

### 变更
 - `main.py` 移至仓库顶层，`gensokyoai/` 作为纯库包；provider 按配置名（`model.provider`）经 Registry 动态选择
 - `brain/`、`responder/`、`memorizer/`、`health/` 收进 `core/` 成为其子包（平台无关引擎内核），eyes / roleplay / models 为可替换边缘层；架构文档 §7 已同步
 - `schemas/` 扩充数据契约：`Message` / `Usage` / `CompletionResult` / `SceneSnapshot` / `SceneEvent` / `BrainConclusion` / `OOCVerdict` / `BaseEvent`+`Topic` / `Prompt`
 - ModelProvider 协议保持「薄」：人设与上下文管理归属 SessionManager/上层，Provider 只做纯补全

### 修复
 - 消息序列化含 `name: null` / `tool_call_id: null` 导致 llama-server 报 `json.exception.type_error.302` 500 错误 —— None 字段一律省略
 - Qwen3 系模板默认 `thinking=1`，思考段烧尽生成预算导致结构化调用拿到空正文 —— `think=False` 时注入 `chat_template_kwargs` 关闭模板思考，并兜底剥离正文中的 `<think>` 段

### 测试
 - 42 个测试全绿：分层依赖 AST 检查（内核子包互禁 import）、全量导入冒烟、SessionManager 裁剪/双模式、Brain 路由/降级/OOC、PromptManager、消息序列化/思考段分离、roleplay 加载、eyes 解析

## [0.0.1] - 2026/9/4 21h

### 新增
 - 初始提交项目架构，文档等...