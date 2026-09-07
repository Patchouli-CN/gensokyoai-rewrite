
# 更新日志

本文件记录项目的所有重要变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，
本项目遵循 [语义化版本](https://semver.org/spec/v2.0.0.html)。

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