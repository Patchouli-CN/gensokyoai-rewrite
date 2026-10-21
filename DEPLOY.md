# 部署

引擎是**库**，不是常驻服务。谁把它拉起来，谁就是宿主；宿主决定消息从哪来、发言回哪去。

## 先选宿主

| 宿主 | 适用 | 入口 |
|---|---|---|
| 控制台 | 本机试角色、调 prompt | `python -m gensokyoai` |
| ws 服务 | 外部 bot 框架、自研端，走 JSON 帧 | `python -m gensokyoai --serve` |
| 库 | NoneBot2 / 游戏 / 酒馆适配层 | 见「接入 NoneBot2」 |

前两种自带入口；第三种由你的进程自己建 `Loop`（参考 `nb2_plugin/gensokyoai_bridge/engine.py`）。

## 公共前置

- Python ≥ 3.12
- 依赖：`uv pip install -e .`（或 `pip install -e .`）
- 模型端点：OpenAI 兼容，**必须支持原生 tool_call**
  - 引擎请求带 `chat_template_kwargs.thinking / enable_thinking = false`。
    模板不认这两个键、思考没关掉时，输出会被思考段吃光，模型一个工具都不会调
  - 单次补全默认 120 s 超时（`model.timeout`，CLI 上可用 `--model-timeout` 临时覆盖）
- 角色卡：`version / name / detail` 三字段，缺任意一个 `load_card` 直接 `ValueError`
- 落盘（可选）：`--data` / `GSK_DATA` 指定根目录，每个房间两个文件

```
data/<房间名>/events.jsonl    聊天流水，4 MB 轮转到 .1
data/<房间名>/memory.jsonl    记忆条目
```

不给这个参数 = 纯内存，进程一退就忘。

## 模型与计费：`config/settings.yaml`

模型、单价、窗口额度都写在这一个文件里，**CLI 与 nb2 插件共用**。改完重启生效。

这个文件**不进 git**（里面可能有模型令牌），仓库里只有模板：

```bash
cp config/settings.example.yaml config/settings.yaml
```

默认位置锚在仓库根，从哪个目录启动都读得到；`--settings` 给的是别的路径
却不存在时会直接报错，不会静默回落到本地模型。

```yaml
model:
  provider: openai                      # openai（兼容端点）/ anthropic
  base_url: http://127.0.0.1:8080/v1
  name: ""                              # 云端必填：deepseek-chat / gpt-4o-mini / ...
  api_key: ""                           # 本地留空；云端填令牌
  think: false                          # null = 连 chat_template_kwargs 都不发
  timeout: 120
  extra: {}                             # 原样并进请求体（厂商专有参数）

billing:
  price:                                # USD / 百万 token；全 0 = 按模型名查内置表
    input: 0
    output: 0
    cached: 0
  quotas:                               # 滚动窗口额度（订阅制的墙）
    - span: 5h
      tokens: 200000
    - span: 7d
      tokens: 1000000
  usage: data/usage.jsonl               # 用量流水；空 = 不落盘
```

| 端点 | provider | base_url | name |
|---|---|---|---|
| llama-server / vLLM / Ollama | `openai` | `http://127.0.0.1:8080/v1` | 留空 |
| DeepSeek | `openai` | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` |
| OpenRouter | `openai` | `https://openrouter.ai/api/v1` | 它给的名字 |
| Claude | `anthropic` | `https://api.anthropic.com/v1` | `claude-sonnet-4-20250514` |

- 切换是全局的：改这个文件、重启进程
- CLI 用 `--settings` 指定别处的配置；`--model` 只是临时覆盖 base_url
- nb2 插件用 `GSK_SETTINGS` 指向它（默认 `../config/settings.yaml`）；流水落在它的 `GSK_DATA` 下
- 本地端点不需要模型名和令牌，留空即不发
- `provider` 不只是协议形状，也决定用哪套价格（见下一节）

## 计费：价格跟着适配器

计费是一个 Protocol（`currency` / `cost()` / `describe()`），各家适配器带自己的
价格，引擎只问「这一次花多少」：

| 适配器 | 价格 | 说明 |
|---|---|---|
| `openai` | USD，模型名前缀查官方价 | `gpt-4o-mini` / `gpt-4o` |
| `anthropic` | USD | `claude-*` |
| `deepseek` | **CNY + 峰谷** | 空闲半价，缓存命中单独一档 |
| 其它 | 不提供 | 账本只记 token，摘要写「该厂商未提供计费」 |

换一家不只要改 `base_url`，`provider` 也要跟着换——价格挂在适配器上：

```yaml
model:
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  name: deepseek-flash
```

想覆盖适配器的价（比如拿到更低的协议价）：

```yaml
billing:
  price:
    currency: USD
    input: 1.0            # 平常价（峰谷模型里就是空闲价）
    output: 4.0
    cached: 0.02
    peak:                 # 给了高峰价就启用峰谷
      input: 2.0
      output: 8.0
      cached: 0.04
    peak_hours: ["9-12", "14-18"]      # 北京时间
    peak_weekdays: [1, 2, 3, 4, 5]
    holidays: ["2026-01-01"]           # 这些天全天按平常价
```

**DeepSeek 的峰谷**（官方口径，已内置）：高峰是北京时间工作日 9-12、14-18，
其余时间（含周末、法定节假日）算空闲：

| 模型 | 空闲（元/百万） | 高峰（元/百万） |
|---|---|---|
| `deepseek-flash` | 1 输入 / 4 输出 / 0.02 缓存 | 2 / 8 / 0.04 |
| `deepseek-v4-pro` | 4.5 / 13.5 / 0.15 | 9 / 27 / 0.30 |

节假日引擎猜不到，要准确就在 `holidays` 里列出来；不列就按「工作日 9-12、14-18」
判断。旧名 `deepseek-v4-flash` 按 flash 计费。

**窗口额度（订阅制的墙）** —— Claude Pro/Max 这种没有单价、只有 5h / 7d 的用量墙：

```yaml
quotas:
  - span: 5h          # 支持 m / h / d / w
    tokens: 200000
  - span: 7d
    tokens: 1000000
```

- 用量过 80% 打一条 warning，过 100% 再打一条，每个窗口每档只喊一次
- `/doctor` 里能看到累计 `llm.prompt_tokens` / `llm.completion_tokens`，
  以及每个窗口已用多少
- `billing.usage` 落一行一条流水，重启后窗口接着算；超出最长窗口的老流水会被丢掉

**查余额** —— 有的厂商有余额接口（DeepSeek 是 `GET /user/balance`），
`/balance`（别名 `/余额`，需要 admin 权限）会当场查一次：

```
/余额  ->  余额 ¥41.78（赠送 ¥0.00 / 充值 ¥41.78）（¥1/4 空闲，2/8 高峰…）
```

- 没有余额接口的（OpenAI 就没有）会回「这个端点不提供余额查询」，不报错
- 查询失败（断网、令牌错）也当不支持处理，不会影响聊天
- 余额是联网查的，跟本地算的花费（账本）是两回事：一个是别人账上还剩多少，
  一个是这次会话按单价累加了多少
- **同一个群里 3 秒内只查一次**：A、B、C 连着查，只有 A 那次真的发请求，
  回复在群里大家都看得见，后两条静默（`coalesce_s` 由命令自己声明；
  跨群不合并，因为彼此看不见）

**再加一家**：在 `gensokyoai/llm/<vendor>.py` 里实现 `Provider.complete`，
带一个 `billing`（`FlatBilling` / `PeakBilling` 够用，特殊口径自己实现 `Billing` 协议），
有余额接口就顺手覆盖 `balance()`，然后注册进 `PROVIDERS`。

### 关思考

思考开着会烧输出 token（实测 `deepseek-flash` 答「1+1」：38 tokens 里 36 是思考），
长一点的问题还可能把 content 挤空。各家开关不一样：

| 端点 | 写法 |
|---|---|
| llama-server / vLLM | `think: false`（发 `chat_template_kwargs`） |
| DeepSeek | `think: null` + `extra: {thinking: {type: disabled}}` |
| 其他 OpenAI 兼容 | 先跑一次：日志里出现「这个端点开着思考」就翻厂商文档 |

引擎第一次看到思考 token 会打一条 warning，并把它记进账本
（`/doctor` 里的 `llm.completion_tokens` 含思考，摘要里会单独标出来）。

## 模型能力（看不看图、听不听声）

没有统一的标准可查，所以按这个顺序定：**端点自报 → 名字猜测 → 配置声明**。
启动时探一次，日志里会打一行 `模型能力: text, image`。

```yaml
model:
  capabilities: [text, image]     # 兜底声明
  auto_detect: true               # 启动时问一次 /v1/models
```

- **端点自报**：有的服务在 `/v1/models` 里给 `capabilities`（llama.cpp 这类）。
  这是最准的，能读到就用它
- **名字猜测**：`gpt-4o*` / `claude*` / `*-vl` 当图文；`deepseek-chat` 当纯文本；
  `whisper*` 当语音。猜不出就是「不知道」
- **配置声明**：上面两条都不成时用它

**降级行为**：模型不吃图（能力里没有 `image`）时，图片不会发出去，只在正文里
留 `[图片×n]` 标记——消息不丢，模型知道自己收到了图但看不到内容。

| 能力 | 状态 |
|---|---|
| `text` | 一直有 |
| `image` | 通了（收图 + 发图块） |
| `audio` / `video` | **能力名先留着，传输没做**——OpenAI 是 `input_audio`、Gemini 是 `inline_data`，各家形状不一样，等真要用再照 `Image` 扩一种 Part |

## 形态 A：控制台

```bash
python -m gensokyoai --character config/roles/KomeijiSatori.yaml
```

- `--user` 你的称呼（默认「你」）
- `--data` 落盘根目录（默认 `data/rooms`）；`--scene` 选场景（`--list-scenes` 看有哪些）
- Ctrl-C / 输入 EOF 退出：先把排队写盘收尾，再关文件句柄

看日志：`LOOP` 行有每回合耗时；出现 `模型只写了正文、没调工具`（debug 级）说明模型没按协议走。

## 形态 B：ws 服务

```bash
python -m gensokyoai --character config/roles/KomeijiSatori.yaml \
  --serve --host 127.0.0.1 --port 8082 --token 你的令牌
```

- 频道即房间：`ws://host:port/ws/{频道名}`
- 握手 query：`token`、`user`、`type=private`、`direct=1`、`style=free|pure_dialogue|tavern|rpg`
- `style` 与 `role=owner|admin` **只认回环连接**，公网自报无效
- 发：`{"text": "...", "user": "帕秋莉", "direct": true}`，纯文本也收
- 收：`{"type": "message", "speaker": "觉", "text": "..."}`
- `--max-rooms`（默认 64）到顶后，新频道握手直接回 503
- 斜杠指令只在这个形态生效：`/doctor`、`/help`（别名 `/体检`、`/帮助`）。
  回系统帧、不进房、不调模型
- 要接外网协议端就把 `--host` 改成 `0.0.0.0`，并且**务必给 `--token`**

## 形态 C：接入 NoneBot2

宿主是 nb2 进程，引擎跑在它里面。目录 `nb2_plugin/`（不在本仓库的追踪范围内）。

**1. 把引擎装进宿主 venv**（插件必须 `import gensokyoai`）

```powershell
cd nb2_plugin
.venv\Scripts\python.exe -m ensurepip
.venv\Scripts\python.exe -m pip install -e ..
```

装的时候有 `uv` 也可以用 `uv pip install --python .venv -e ..`。

验证：

```powershell
.venv\Scripts\python.exe -c "import gensokyoai, gensokyoai_bridge.scene; print('ok')"
```

**2. 配 `.env`**（`GSK_` 前缀，复杂值写 JSON）

```dotenv
DRIVER=~fastapi
HOST=127.0.0.1
PORT=8090

GSK_SETTINGS=../config/settings.yaml
GSK_CHARACTER=../config/roles/KomeijiSatori.yaml
GSK_DATA=../data
GSK_WINDOW=30

GSK_GROUP_WHITELIST=[364292616]
GSK_OWNER_IDS=[你的QQ]
```

完整键与默认值见 `nb2_plugin/gensokyoai_bridge/config.py`。

**3. 起 NoneBot2**

```powershell
cd nb2_plugin
.venv\Scripts\python.exe bot.py
```

日志里应有三条：`Succeeded to load plugin "gensokyoai_bridge"`、`gensokyoai 引擎已启动`、
`Running on http://127.0.0.1:8090`。

**4. 让 NapCat 连进来**（方向最容易搞反）

本进程是**反向 WS 服务端**。NapCat 那边配 **WebSocket 客户端**指向：

```
ws://127.0.0.1:8090/onebot/v11/ws
```

不是 nb2 去连 NapCat。

**5. 验证**：白名单群里 @ 它说一句话 → 收到回复，并且出现
`data/g364292616/events.jsonl` 与 `memory.jsonl`。

斜杠指令在 QQ 侧也走本地执行（`/help`、`/doctor`），回一条带「系统」前缀的消息，
不进房、不调模型。`/doctor` 目前对所有群员开放。

## 场景挂载

内置 `chat` 开箱可用。其它来源三条路：

- `--plugin-dir plugins/`：目录里每个 `.py` 当一个插件（CLI 宿主）
- entry point `gensokyoai.plugins`：打包分发时自动发现
- 宿主直接 import：nb2 就是这样，`@scene` 在 import 时注册进表

场景只提供策略：名词、连发条数与间隔、事件形状、出口、额外工具。
回合、未读、`gap`、记忆、重试都在引擎里——**插件不要自己写循环**，否则每个宿主的人味都会漂。

## 排错

| 症状 | 多半是 |
|---|---|
| nb2 启动报 `No module named 'gensokyoai'` | 引擎没装进那个 venv（第 1 步） |
| 加载成功但一句不回 | 群不在 `GSK_GROUP_WHITELIST` |
| 偶尔不回 | 复读冷却 / 已答覆盖吞了，debug 日志里有记录 |
| 一直不说话 | 模型没调工具；看 `模型只写了正文、没调工具`；确认思考已关 |
| 首条特别慢 | 模型冷启动 |
| 回复里带括号动作 | QQ 场景默认剥掉；`GSK_STRIP_NARRATION=false` 时保留 |
| `--serve` 起不来 | 端口占用——启动错误会抛出来，不会假装在跑 |
| ws 握手 403 / 503 | token 不对 / 房间数到顶 |

## 图片

**收图**：群友发的图会被抓下来（base64）随那一轮喂给模型，模型是"看过"的，
不是只知道"有张图"。前提是模型和模板支持多模态——能力探测见上一节；探测不到
又猜不出时，按 `model.capabilities` 的声明办，声明里没有 `image` 就只留文字标记。

配置（`.env`）：`GSK_VISION=true`、`GSK_IMAGE_MAX=2`、`GSK_IMAGE_MAX_BYTES=10000000`。
图**不落盘**，日志里只留正文里的 `[图片×n]` 锚点；重启后那段历史只剩文字。

抓不到图（URL 过期/超大小/非 http）时只留锚点，不会整条丢。

**体积上限怎么定**：瓶颈不是字节数，是像素折成的 token。实测同一个 llama-server：

| 原图 | 体积 | 耗时 | prompt tokens |
|---|---|---|---|
| 256² | 0.19 MB | 17.2 s | 2152 |
| 768² | 1.69 MB | 11.3 s | 2152 |
| 1536² | 6.75 MB | 11.5 s | 2340 |
| 2048² | 12.0 MB | 21.4 s | 4132 |

服务端会把图缩到固定档位，所以 6.7 MB 和 0.19 MB 的 token 数几乎一样；
到 2048² 才翻倍。默认 10 MB 覆盖群聊里的绝大多数原图。真嫌慢就调小 `GSK_IMAGE_MAX`（张数），
比调体积更有效。

**发图**：暂不支持。模型只能发文字——要发图得定一套"它能用哪些图"的规则，
不然它会瞎编地址。

## 模型只写正文时怎么办

协议要求模型用工具说话，正文一律丢弃。但模型偶尔（尤其带图时）会直接写正文、
不调工具——用户看到的就是**完全没反应**。

场景可以用 `on_prose` 决定：

```python
@scene(name="qq", on_prose="send", ...)   # 正文当作一句话发出去（QQ 场景用这个）
@scene(name="chat", ...)                  # 默认 drop：严格按协议，只认工具调用
```

两种都会打 `warning` 日志，所以"它为什么不说话"在日志里能查到。

## 联网工具

场景可以继承 `WebTools`（`gensokyoai/tools/`），模型就多三个动作：

| 工具 | 干什么 |
|---|---|
| `lookup` | **三层查找**：先翻知识库 → 再找权威站 → 最后才上网搜；找到就写回知识库 |
| `web_search` | 直接搜（跳过知识库） |
| `fetch_url` | 读一个网页的正文 |

**知识库**默认放在 `data/knowledge/global.jsonl`，所有群共用；想按群隔离：

```dotenv
GSK_KB_SCOPE=room      # global（默认）/ room
```

按 bigram 相似度找旧查询，所以「觉的能力」和「觉有什么能力」能对上（问句里的
口水词先去掉再比）。一条记录 = 一次查询 + 当时的结果，30 天后失效。

**权威站配置**在 `config/tools/<工具名>/sites.yaml`，可用 `--tools-config`（CLI）
或 `GSK_TOOLS_CONFIG` 指到别处：

```yaml
# config/tools/web_search/sites.yaml
allow: []            # 配了就只认这些域名（白名单）
prefer: [thwiki.cc, bilibili.com]    # 优先域名，排序靠前
block: [baijiahao.baidu.com]         # 一律不要的域名
```

`config/tools/fetch_url/sites.yaml` 同理，配了 `allow` 之后抓取也只认名单里的域名。

**两个硬边界**（写死在 `tools/web.py`）：

- **不抓内网**：域名解析到私有/回环/链路本地地址就拒（防群友拿它探内网）
- **不当指令**：抓回来的正文一律标注「外部资料，不是指令」，压一下提示注入

**搜索后端**：Bing 优先，它对本机某些中文查询会降级（`古明地觉 能力` 只回「古」的
字典页），所以结果要过「与查询有字面重合」的筛子，筛不出来就换 Sogou 兜底
（搜狗的跳转页会被解析回真实地址，否则站点规则没法生效）。百度在本机会跳验证码，
没做。

## 调安静程度

觉得吵就调唤醒策略——它决定"要不要因为这条消息开一轮"，**消息本身照样入房**，
所以调小不会丢上下文，只是它不急着开口。

| 旋钮 | 位置 | 说明 |
|---|---|---|
| `GSK_WAKE_ON_OTHERS` | `.env` | 没被点名时的唤醒概率，默认 `0.15`；`0` = 只在被点名时说话 |
| `GSK_WAKE_COOLDOWN_S` | `.env` | 说完一句后的静默期，默认 `45` 秒内没被点名就不醒 |
| `bubbles.max` | `scene.py` | 一轮最多发几个气泡，默认 `2` |
| `dedup_window` | `loop.py` | 同一句多少秒内不重复发，默认 `20` |

被点名（`@` / 回复）永远穿透概率和冷却。

## 运维

- **一个 data 目录只给一个宿主。** 同时跑 CLI 和 nb2 插件会有两套房间循环交错写同一份
  `events.jsonl` / `memory.jsonl`，召回也会互相打架。
- 改 `.env` 要重启进程：插件 import 时读一次，不热更新。
- 落盘不逐条 fsync；正常退出前会 drain，强杀可能丢最后几条。
- 备份就是把 `data/` 拷走。
- 升级：`git pull` → `pip install -e .` → 重启宿主。事件日志向后兼容（新字段有默认值），
  坏行会被跳过。

## 别做

- 用系统 python 跑 `bot.py`（缺 nb2 依赖）
- 把 `/doctor` 当内部接口：它现在对所有群员开放，会暴露运行指标
- 为了「稳」把 `--model-timeout` 调到很大：后端挂起时房间会一直等
