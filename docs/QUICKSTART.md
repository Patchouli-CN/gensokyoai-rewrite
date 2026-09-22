# QUICKSTART —— 十分钟跑起来

## 1. 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **≥ 3.14** | 用了 PEP 695 泛型等新语法 |
| 推理后端 | llama-server 或任何 OpenAI 兼容端点 | 默认指向 `http://127.0.0.1:8080/v1` |
| 模型建议 |  instruct/chat 类，32K 上下文为佳 | 项目围绕 Qwen3.6-35B-A3B 调校，小模型也可跑 |

启动 llama-server 示例：

```bash
llama-server -m your-model.gguf --port 8080 --ctx-size 32768
```

可选：真 jev 裁判（[TypeSafe](https://typesafe.ai) API key）：

```bash
pip install '.[jev]'   # typesafe-sdk；装完在 settings.yaml 把 gate.judge 改成 "typesafe"
```

## 2. 安装

```bash
git clone https://github.com/Patchouli-CN/gensokyoai-rewrite.git && cd gensokyoai-rewrite
pip install -e .          # 常规
pip install -e .[dev]     # 带 pytest / ruff / mypy
```

## 3. 配置（三件套）

配置路径解析规则：**优先工作目录，回落到包内自带默认值**。所以仓库内开发直接用仓库根的 `config/`，安装后用 `--config` / `--character` 指定。

### 3.1 `config/settings.yaml` —— 引擎配置

```yaml
default_model:            # 默认模型（brain/responder/ooc/memorizer 未单独配时用它）
  provider: "llama_cpp"   # llama_cpp=llama-server | qwen_local=通用 OpenAI 兼容
  base_url: "http://127.0.0.1:8080/v1"
  model_name: "qwen"
  think: false            # 关闭模型模板自带思考（思考由应用层编排）
  streaming: true         # 流式投递
  context_window: 32768   # 会话预算

resource:                 # 资源闸门：保护本地单模型（串行稀缺资源）
  enabled: true
  max_concurrent: 1       # 全局并发；单卡建议 1

gate:                     # 发言门控 + 模型化档位路由
  enabled: true
  judge: "local"          # local=主模型当裁判 | typesafe=真 jev | none=纯规则
  group_threshold: 0.6    # 群里没人点名时，裁判 should_reply 达到该值才接话
  timeout_ms: 60000       # 裁判调用超时（本地模型单次 10~25s，别调太小）
  route_by_model: true    # 档位由裁判的 needs_deep 说了算
  deep_cuts: [0.3, 0.6, 0.85]   # 进 MID / HIGH / MAX 的切点
```

### 3.2 `config/roles/<角色>.yaml` —— 角色卡

```yaml
name: "西行寺幽幽子"
system_prompt: |
  你是西行寺幽幽子……（人设正文）
greeting: "「啊啦～，欢迎来到白玉楼～……」"
expression_base: 0.7          # 话痨度（主动发言用）
motivation_weights: {}       # 四维对话欲权重（可省）
think_chain:                  # 定制思考链：顺序即思考方向
  - emotion_check            # 内置步骤：emotion_check/relationship_scan/memory_link/stance_decide
  - relationship_scan
  - memory_link
  - stance_decide
example_dialogue: []          # few-shot 示例（可省）
```

> 思考步骤的提示词集中注册在 `gensokyoai/prompts/manager.py` 的 `think.<步骤名>`；新增步骤 = 在那加一个注册函数，卡片里引用名字即可。**留空 `think_chain` 则用内置接力思考。**

### 3.3 数据目录

运行时产物（记忆、会话快照、思考轨迹）写在 `data/`（已 gitignore）：`data/<session_id>/`。

## 4. 跑起来

### 4.1 控制台模式（单聊，最快验证）

```bash
python main.py
# 或安装后：gensokyoai
```

输入即聊；`exit` / `quit` 退出。控制台是私聊场景（每句必回，适合验证角色口吻与思考链）。

### 4.2 WebSocket 多路频道模式

```bash
python -m gensokyoai.backends.ws_server.server   # 或安装后：gensokyoai-ws
# 默认监听 ws://127.0.0.1:8081/ws/{channel}
```

连接参数：`?user=灵梦&type=group|private&channel=群名`。客户端发**纯文本**，服务端把该频道的所有连接合流成一条快照流，角色回复广播给频道内所有在线连接（流式逐帧下发）。

多频道 = 多角色会话（各自记忆/快照隔离），空闲 10 分钟自动回收。

### 4.3 真机验证（强烈推荐跑一次）

```bash
python scripts/real_verify.py --fresh        # 内置演示场景：收尾语/群聊裁判/@/深问题/私聊全路径
python scripts/real_verify.py --log-level DEBUG   # 看每步思考 note 与裁判概率原文
```

跑完出汇总报表（token 消耗、档位分布、上下文占用、最近记忆）。场景可用 JSON 自定义：

```json
[{"text": "哈哈哈哈哈哈", "sender": "灵梦", "gap": 3},
 {"text": "@幽幽子 在吗", "sender": "灵梦", "direct": true}]
```

```bash
python scripts/real_verify.py --scenario my.json
```

## 5. 调参指南

| 想要的效果 | 调什么 |
|---|---|
| 群里更活跃 | `gate.group_threshold` 调低（0.6 → 0.4） |
| 群里安静 | 调高，或 `gate.judge: "none"`（纯规则：只回点名/私聊/被@） |
| 寒暄更快 | `gate.deep_cuts` 第一个值调低；或卡片链只配 2 步 |
| 深问题更透 | 卡片链加到 4 步以上；`deep_cuts` 第三值调低让更多消息进 MAX |
| 角色更话痨 | 角色卡 `expression_base` 调高（主动发言频率） |
| 换思考方式 | 改角色卡 `think_chain` 的顺序/步骤 |
| 彻底关掉门控 | `gate.enabled: false`（维持「每条都回」的旧行为） |

## 6. 工具调用

引擎内置三个工具（`tools/builtin.py`）：`get_current_time` / `get_current_dateinfo` / `get_moon_phase`。加自己的工具就是在该文件里加一个 `@ToolRegistry.tool` 装饰的函数（参数 JSON Schema 从签名自动推导，同步函数自动下线程），重启生效。

两条路径都能用工具：

- **接力思考**（角色卡不配 `think_chain` 时）：全部工具自动挂上
- **思考链**：步骤级开关 `tools=True` 才挂（其余步骤不挂、省 token）；内置步骤 `time_anchor` 默认开启。代码里这样写：

```python
chain = ThinkPipeline("感知链") >> ThinkStep("time_check", instructions="先确认当前时间", tools=True) >> ...
```

**本地小模型的「文本喊话」约定**：不走原生 `tool_calls` 协议的模型（如 llama-server 上的 Qwen），在思考里写 `调用 get_current_time {}` 即可——系统识别后执行并把结果回填。可带 JSON 参数：`调用 get_weather {"city": "北京"}`，支持一次喊多个。真机实测：接力路径下 Qwen 能稳定喊对；思考链步骤路径下弱 quant 可能只写「调用工具」而不写全名（换更强 quant 或云端模型更稳）。

## 7. 模型计费（接云端 API 时）

接第三方模型（OpenAI / Claude / DeepSeek / Gemini / Qwen……）才会产生费用。引擎的计费是**自动 + 可覆盖**的：

- **自动**：内置价格表（`models/pricing.py`，2026-09 调研快照）按响应里的模型名计价——OpenAI / Claude / DeepSeek / Gemini / Qwen 主流型号已收录，未收录的模型**不计价**（不瞎猜）。
- **覆盖**：任何 `<模块>.price` 配置段优先于内置表，新模型/精确价/CNY 价都走这里：

```yaml
responder:
  provider: "qwen_local"
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  model_name: "qwen3.8-max"
  token: "sk-..."
  price:
    currency: "CNY"
    tiers:                      # 按单次请求输入量分档（平价模型留一档即可）
      - up_to: 1000000          # 本档上限（含），单位 token
        price_in: 2.0           # 每百万 token
        price_out: 6.0
        price_cached_in: 0.2    # 缓存命中价（没有就填 0）
        price_cache_write: 2.5  # 缓存写入价（没有就填 0）
```

计费口径（OpenAI 式）：`prompt_tokens` 为输入**总量**，缓存读/缓存写是它的子集，普通输入 = 总量 − 缓存读 − 缓存写。各家差异（DeepSeek 峰谷分时、阿里夜间折扣、Batch 半价）不在自动口径内，需要时用配置价自行建模。

**在哪看账**：每次模型调用日志带 `费用=USD 0.012345`；回合结束日志带本回合花费；健康指标 `turn.cost_<币种>`；`scripts/real_verify.py` 汇总打印累计费用与各模块费用。本地 llama-server 不计价（电费不在 token 口径内）。

## 7. 排错速查

| 症状 | 先看 |
|---|---|
| `KeyError: 'llama_cpp'` / 未注册 backend | 装配前少跑 `discover_all()`（app 入口已内置）；自定义入口需自跑 |
| 裁判永远降级、日志频现超时 | `gate.timeout_ms` 太小（本地模型 10~25s/次，保持 60000） |
| `turn.latency_s` 每回合告警 | 本地模型固有延迟；阈值在 `core/health/monitor.py` 的 `DEFAULT_THRESHOLDS` |
| 回复慢 | 正常（串行单模型，每回合多次调用）；见 [QA.md](QA.md)「为什么回复慢」 |
| 模型不按 JSON 输出 | 步骤会自动纠偏重试一次；仍失败则该步跳过（`optional=True`）或整链降级 |
| 费用对不上官网 | 内置表是 2026-09 快照，价格会变；用 `<模块>.price` 配置覆盖精确价 |

更多问题 → [QA.md](QA.md)。
