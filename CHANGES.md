
# 更新日志

本文件记录项目的所有重要变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，
本项目遵循 [语义化版本](https://semver.org/spec/v2.0.0.html)。

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