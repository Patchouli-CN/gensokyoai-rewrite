
# 更新日志

本文件记录项目的所有重要变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，
本项目遵循 [语义化版本](https://semver.org/spec/v2.0.0.html)。

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