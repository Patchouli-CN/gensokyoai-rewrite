# QA —— 常见问题

## 通用

### Q：这个项目是什么？和原版 GensokyoAI 什么关系？
本地 LLM 角色扮演引擎的**重写版**：把推理、记忆、时机判断全部工程化，小上下文模型也能稳定扮演。设计思路见 [README](../README.md)，跑法见 [QUICKSTART.md](QUICKSTART.md)。

### Q：接 QQ 吗？
引擎本身平台无关：输入走 `eyes`（`Perceiver` 协议 + OneBot11 解析器），输出走 `mouth`。当前内置入口是**控制台**与**WebSocket 文本频道**；接 QQ 需要在外面做一层协议转发（把 OneBot11 事件过 `eyes/parser.py` 打成快照），或直接以 WS 客户端身份推文本。

### Q：`command/` 目录是干嘛的？
命令子系统（解析 / 执行 / 权限分级），供上层把自己平台的命令（如 `/reset`）接进来；主循环默认不启用。

## 模型与推理

### Q：支持哪些模型后端？
通过 Registry 注册 Provider：`llama_cpp`（llama-server，带思考段分离与模板参数处理）与 `qwen_local`（任何 OpenAI 兼容端点：vLLM / 云端网关等）。`brain / responder / ooc / memorizer` 可分别路由到不同模型（共享或分模型实例都行）。

### Q：真 jev（TypeSafe）必须装吗？
不必须。门控裁判默认用**本地模型**（`LocalJudge`，复用主模型的无状态小调用）。想用真 jev 就 `pip install '.[jev]'`，然后把 `gate.judge` 改成 `"typesafe"`——协议不变，行为无缝切换。

### Q：五档推理是什么？和普通 temperature/max_tokens 有什么区别？
档位（`NONE/LOW/MID/HIGH/MAX`）决定**思考链跑几步 / 接力跑几轮**，是结构性深度，不是采样参数。例如 LOW 只跑收尾步（1 次调用），HIGH 跑全链，MAX 每步预算翻倍且降温。

### Q：门控（gate）是什么？关掉会怎样？
「该不该接话」的 System-1 层：规则预筛（收尾语/私聊/被@）+ 群聊模糊带交裁判（`should_reply` 概率 vs 阈值）。关掉（`gate.enabled: false`）回到「每条都回」的旧行为，日志更简单但会变吵。

### Q：思考链怎么定制？
角色卡 `think_chain` 按序写步骤，每项两种形式：**内置步骤名**（字符串，提示词在 `prompts/manager.py` 的 `think.<名>` 注册）或**内联自定义步骤**（字典，`name`/`instructions` 必填，可选 `max_tokens`/`temperature`/`timeout_s`/`optional`/`tools`）——作者直接写「这一步想什么」，无需改代码。代码里也可以用 `ThinkPipeline("标签") >> ThinkStep(...)` 拼装，两条链还能 `>>` 合并复用前缀。

### Q：工具调用怎么工作？
接力思考路径里模型可发起 tool_calls，由 `core/toolkit.py` 统一执行（超时/截断/同步工具丢线程池），结果回填继续思考。内置工具：时间/日期/月相（`tools/builtin.py`，`@ToolRegistry.tool` 即插即用）。注意：思考链（pipeline）路径的 `tools` 开关是预留位，v1 未接线。

## 记忆与状态

### Q：记忆存在哪？重启会丢吗？
`data/<session_id>/`：对话记忆（按人隔离）、会话快照、跨回合运行时状态（档位下限/蒸馏计数等），事件驱动落盘 + 关闭 flush，重启自动恢复。

### Q：记忆会无限膨胀吗？
不会：分级存储 + 最早淘汰 + 蒸馏压缩（定期把最早一批对话压成摘要），并有健康监控在上下文占用过高时主动触发蒸馏。

## 性能

### Q：为什么回复要几十秒？
本地单模型**串行**处理：门控裁判 + 思考链各步 + 结论 + Responder + OOC 深审，每步都是一次完整推理（实测单次 8~25s）。降延迟的手段：调低 `gate.deep_cuts`（更多消息走浅档）、把卡片链配短、或让裁判/Responder 用更快的模型端点。

### Q：`turn.latency_s` 一直告警正常吗？
本地模型下属正常——阈值 30s 是按旧快速路径定的。在意就调 `core/health/monitor.py` 里 `DEFAULT_THRESHOLDS`。

### Q：裁判和主回合为什么会互相拖慢？
后台侧链（OOC 深审等）与主回合共享同一个串行资源闸门，FIFO 先到先得。已知影响，缓解中（步骤超时已放宽到 90s 覆盖排队）。

## 故障排查

### Q：`KeyError: 'llama_cpp'`？
装配前需要 `discover_all()` 扫描注册 Provider/工具（`app.py` 入口已内置；自定义入口自己调一次）。

### Q：缺 API key 类报错？
只有 `gate.judge: "typesafe"` 才需要 TypeSafe key；本地模型模式只需要能访问你的推理端点。

### Q：模型不按 JSON 协议输出怎么办？
每步解析失败会自动带坏输出纠偏重试一次；仍失败则：`optional=True` 的步骤跳过继续，必要步骤 / 结论步失败则整链降级为关键词快速路径——回合不会死，只是变「浅」。

### Q：Windows 上有什么注意？
项目基于 Python 3.14 开发（用了 PEP 758 无括号多异常等新语法）；日志/文件路径均跨平台处理，`data/` 与测试临时目录都在工作目录内。

## 工具调用

### Q：模型不支持 function calling 也能用工具吗？
能。本地小模型（llama-server 上的 Qwen 等）走**文本喊话**：在思考里写 `调用 工具名 {"参数": 值}`，系统识别、执行、回填结果。参数 JSON 会尽力提取（工具名后 120 字符窗口内找 JSON 对象），找不到按无参工具执行。

### Q：模型忘了写参数怎么办？
三层防护，从前往后：① **upfront 签名暴露**：工具步骤的 system 提示和接力思考的 [可用工具] 段会把每个工具的完整签名（`名字(参数: 类型) — 说明`）摊开，不用等报错；② **自教学错误**：缺参失败时回给模型的错误附带标准调用格式（`调用 days_until {"target_date": "..."}`），真机验证 Qwen 能照着下一轮纠正重试成功（冬至倒计时 46→91 天）；③ **多轮重试**：接力思考按档位有 2~8 轮，每轮都有纠正机会。原生 `tool_calls` 协议的模型（云端）不依赖这些（Provider 直接解析参数）。已知局限：IQ3 级别的弱 quant 可能「懂参数语义但用中文散写不写 JSON」（如「目标日期设为冬至」）——换更强 quant 或云端模型更稳。

### Q：为什么思考链步骤里模型不调工具？
两种常见情况：① 弱 quant 在单步指令下只写「调用工具」不写全名（relay 多轮上下文更容易喊对）——换更强 quant 或云端模型；② 强人设角色会「角色化地」回避（幽幽子答「分不清今夕何夕」）——这恰是人设起作用，可在步骤指令里强调「必须先获取再结论」。

### Q：工具会在流式回复里调用吗？
不会。工具只在内部调用（接力思考 / 思考链步骤）执行；流式只负责把最终回复逐字投递给用户。

### Q：怎么让角色知道现在几点？
角色卡 `think_chain` 里放 `time_anchor`（内置步骤，默认挂工具）：涉及时间/日期/月相的问题会先调工具再回答。

## 计费

### Q：接第三方 API 后怎么算钱？
自动：`Provider.costs()` 按内置价格表（`models/pricing.py`）计价——输入 / 缓存读 / 缓存写 / 输出四条流，支持输入分档（Qwen max 按请求长度切价）与多币种。未收录模型明确 `unpriced`（费用 0 并注明「不是免费」），本地 llama-server 同样不计价。

### Q：内置表准吗？内置了哪些？
2026-09 调研快照，收录 OpenAI（GPT-5.6 系）、Claude（Opus/Sonnet 5、Haiku 4.5）、DeepSeek（flash / v4-pro，忙时价）、Gemini 3.x、Qwen（max/plus 分档）。**价格随时会变**，对账以官网为准；要精确就在 `settings.yaml` 的 `<模块>.price` 覆盖（优先于内置表）。

### Q：为什么费用里没有 Batch 半价 / DeepSeek 闲时半价 / 阿里夜间折扣？
这些是**时间/模式相关**的折扣（同一请求不同时刻不同价），自动口径算不了，没有蒙混成标准价。需要就用配置价把自己时段的实际单价填进去。

### Q：后台 OOC 深审的钱算谁的？
算到**触发它的那个回合之后**的下一个回合增量里（异步侧链在回合结束后才跑完）。这是差值计费的固有特性，看单次调用日志（每次 `补全完成` 都带费用）是最准的。

### Q：`Usage.cache_write_tokens` 是什么？
缓存写入计费的 token（Anthropic cache_creation / 阿里显式缓存创建，通常是输入的 1.25×）。OpenAI 兼容协议里大多厂商不单列，保持 0；Provider 解析时会从 `prompt_tokens_details.cache_write_input_tokens` 等字段尽力提取。

## 开发相关

### Q：质量门有哪些？
`pytest` 全绿 + `ruff check` / `ruff format --check` / `mypy`；提交遵循 Conventional Commits，AI 提交标注模型名（见 [CONTRIBUTING.md](../CONTRIBUTING.md)）。

### Q：架构上的硬规矩？
`utils`/`schemas` 纯叶子；`core` 内核四个业务子包（brain/responder/memorizer/health）之间**零直接 import**，通信走事件总线或装配层注入；CI 有 AST 检查兜底，详见测试 `test_no_circular_import.py`。

### Q：文档以后要多语言怎么办？
README 放在仓库根（主页门面），其余文档统一在 `docs/` 下（QUICKSTART / QA），按语言建子目录即可（如 `docs/en/`），文件名保持稳定。
