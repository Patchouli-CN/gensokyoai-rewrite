# GensokyoAI（重写版）

**本地 LLM 驱动的小模型角色扮演引擎。** 核心理念只有一条：

> **把推理和记忆从上下文里挤出去，用外部工程手段管理，只在上下文里保留最精华的部分。**

角色一致性、记忆管理、推理判断全部由工程代码承载（而非塞进提示词），让 8K~32K 上下文的小模型也能稳定、快速地扮演角色。

**目录**：[特性](#特性) · [快速开始](#快速开始) · [五档推理深度](#五档推理深度) · [发言门控](#发言门控jev-式-system-1-层) · [目录结构](#目录结构) · [开发](#开发) · [文档导航](#文档导航) · [License](#license)

## 特性
### 推理系统（双系统架构）

- **规则反射 + 模型裁判（System 1）**：新消息先过零成本规则（收尾语/私聊/被 @ 直判），群聊模糊带才调裁判——一次模型调用同时回答「该不该接话」与「该想多深」。裁判后端可插拔：本地模型（`LocalJudge`）或真 [TypeSafe jev](https://typesafe.ai)（`TypeSafeJudge`，可选依赖）。
- **五档动态推理深度**（`NONE / LOW / MID / HIGH / MAX`）：档位决定**思考链跑多深、接力跑几轮**——寒暄只跑收尾步（1 次调用），深问题跑全链，MAX 档每步预算翻倍 + 低温。档位由裁判的 `needs_deep` 分数决定，规则路由兜底。
- **可定制思考链流水（ThinkPipeline）**：角色卡 `think_chain` 按名引用思考步骤（提示词集中注册在 `prompts/manager.py`），**换链 = 换角色的思维方式，零代码**。步骤用 `ThinkPipeline("标签") >> ThinkStep(...)` 拼装（泛型链式基类 `FluentAPI[T]`），步骤间以压缩 digest 交接结论。
- **工具调用**：接力思考与思考链（`ThinkStep(tools=True)`，内置 `time_anchor` 步骤默认开启）都支持；内置时间/日期/月相三个工具（`@ToolRegistry.tool` 即插即用）。执行统一走 `ToolExecutor`（超时/截断/同步下线程/结构化错误）；本地小模型不走原生协议，走**文本喊话**（`调用 工具名 {"参数": 值}`，参数 JSON 尽力提取，支持一次多个）。
- **接力思考（relay）**：未配思考链时走多轮接力——模型每轮自述 `need_continue_think`，按档位设轮数上限；工具调用穿插其中不计轮数。
- **过渡语掩延迟**：深思考前先垫一句角色口吻的过渡语（「唔……让妾身想想」），掩盖接力延迟。
- **OOC 守门**：初稿零成本规则预筛，命中花一次纠偏重生成；回复发出后跑异步深审，出戏率飙升自动抬高推理档位下限。

### 表达与投递

- **口层（Mouth）**：与控制台 / WebSocket 等平台对称的输出适配层；支持流式（begin/delta/end）与缓冲两种投递。
- **Eyes 感知层**：`Perceiver` 协议屏蔽平台差异，OneBot11 / 通用 dict 解析，`QueuePerceiver` 多路合流。
- **开场白 / 主动发言**：四维对话欲规则评估（表达欲/情绪唤起/关系牵引/情境时机），冷场时角色会主动冒泡。

### 记忆与工程

- **长期记忆**：按人按会话隔离，对话 + 内心想法摘要，分级存储 + 淘汰 + 蒸馏压缩，重启可恢复。
- **模型计费兼容层**：`Provider.costs()` 统一口径——输入 / 缓存读 / 缓存写 / 输出四条流 + 输入分档 + 多币种；内置价格表收录主流云端模型（OpenAI / Claude / DeepSeek / Gemini / Qwen，2026-09 快照），本地与未收录模型**明确不计价**（unpriced，绝不瞎猜）；配置价可覆盖。按 owner / 租户累计，回合结束时给出本回合花费。
- **健康监控**：token / 延迟 / 上下文占用 / OOC 率 / 费用等指标，超限告警 + 主动干预（上下文告急触发蒸馏）。
- **多模型路由 + 资源闸门**：一个模型实例多个虚拟会话，也可按模块（brain/responder/ooc/memorizer）路由到不同后端；并发/RPM/日预算限流保护本地单卡。
- **会话持久化**：记忆、会话快照、跨回合运行时状态事件驱动落盘，关闭前 flush。
- **思考轨迹留档**：每回合一行 JSONL（含逐步推理记录），便于复盘调参。

## 快速开始

```bash
pip install -e .          # 或 pip install -e .[dev]
python main.py            # 控制台模式，直接和角色对话
```

模型默认指向 `http://127.0.0.1:8080/v1`（llama-server 或任何 OpenAI 兼容端点）。完整步骤见 **[QUICKSTART](QUICKSTART.md)**，常见问题见 **[QA](QA.md)**。

## 五档推理深度

| 档位 | 思考链（配了卡片链） | 接力思考（未配卡） | 适用场景 |
|---|---|---|---|
| NONE | 不跑链 | 不跑 | 空输入 / 纯寒暄收尾 |
| LOW | 只留收尾步（1 次调用） | 2 轮 | 日常寒暄、简单问答 |
| MID | 首 + 尾（2 次调用） | 3 轮 | 一般对话，需看关系和情绪 |
| HIGH | 全链（N 次调用） | 5 轮 | 复杂剧情推进、多角色交互 |
| MAX | 全链 + 每步深思考（预算×2、低温） | 8 轮 | 重大剧情节点、情感转折 |

## 发言门控（jev 式 System-1 层）

```
新消息 → 收尾语/私聊/被@（规则直判，零成本）→ 群聊模糊带 → 裁判一次调用：
        {should_reply 该不该说, needs_deep 该想多深, addressed, needs_search}
```

- `should_reply ≥ gate.group_threshold`（默认 0.6）才接话；裁判输入含角色自身活跃度（近 5 分钟说了多少、距上次发言多久），**防刷屏靠模型自觉，代码不设硬冷却**。
- 裁判不可用时自动退化：不接的回合回落「点名才回」，档位回落规则路由。

## 目录结构

```
gensokyoai/
├── utils/          # L0 叶子：fluent 链式基类 / 日志 / token 计数
├── schemas/        # L0 数据契约：msgspec 结构体（消息/场景/记忆/决策/健康）
├── prompts/        # 提示词集中注册（think.* / gate.* / brain.* ...）
├── core/           # L1 引擎内核
│   ├── brain/      # 决策：engine（接力+档位路由）/ gate（发言门控）/ judge（裁判后端）/ pipeline（思考链）/ ooc_detector
│   ├── responder/  # 表达：生成 / 风格 / 过渡语 / OOC 纠偏
│   ├── memorizer/  # 记忆：分级存储 / 蒸馏压缩
│   ├── health/     # 监控：指标 / 告警 / 主动干预
│   └── ...         # session_manager（虚拟会话）/ event_bus / resource（闸门）/ persistence
├── models/         # L2 模型接入：llama_cpp（llama-server）/ qwen_local（通用 OpenAI 兼容）/ pricing（内置价格表）
├── eyes/           # 感知层：perceiver / queue（多路合流）/ parser（OneBot11）
├── mouth/          # 口层：console / broadcast（多路广播+流式）
├── roleplay/       # 领域：角色卡 / 主循环 / 频道中枢 / 持久化
├── command/        # 命令子系统：解析 / 执行 / 权限（供上层接线）
├── tools/          # 工具注册：内置时间/日期/月相等小工具
└── backends/       # 入口：ws_server（WebSocket 多路频道）
scripts/
└── real_verify.py  # 真机验证：驱动本地 llama-server 跑完整场景并出汇总报表
```

**分层铁律**：`utils`/`schemas` 是纯叶子；`core` 是引擎内核（brain/responder/memorizer/health 之间零直接 import，通信走事件总线或装配层注入）；`models`/`eyes`/`mouth`/`roleplay` 是可替换边缘层；依赖只能向下（CI 有 AST 检查兜底）。

## 开发

```bash
python -m pytest          # 测试（不读本地 .env）
python -m ruff check .    # lint
python -m mypy gensokyoai # 类型
python scripts/real_verify.py --fresh   # 真机验证（需要本地 llama-server）
```

提交遵循 [Conventional Commits](../CONTRIBUTING.md)，AI 提交请标注模型名。

## 文档导航

| 文档 | 内容 |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | 安装、配置、跑起来、调参、排错 |
| [QA.md](QA.md) | 常见问题：模型、门控、思考链、记忆、性能、故障 |
| [../CHANGES.md](../CHANGES.md) | 更新日志 |

## 相关项目

- [llama.cpp](https://github.com/ggml-org/llama.cpp) —— llama-server 推理后端
- [TypeSafe AI](https://typesafe.ai) —— jev 决策模型（可选裁判后端）
- [ayafileio](https://github.com/Patchouli-CN/ayafileio) —— 自家跨平台真异步文件 IO（持久化落盘，Windows IOCP / Linux io_uring / macOS GCD）
- [MaiBot](https://github.com/Mai-with-u/MaiBot) —— 回复时机 / 表情 / 记忆思路的来源

## License

[MIT](../LICENSE)
