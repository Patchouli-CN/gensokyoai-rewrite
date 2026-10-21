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
- 再加一家：实现 `Provider.complete`，塞进 `gensokyoai/llm/__init__.py` 的 `PROVIDERS`

## 计费：两种口径

**按 token 计费（按量付费）** —— 每次补全按厂商回的 `usage` 记账，输入 / 输出 /
缓存命中分开算。价格按模型名前缀查内置表（`gpt-4o-mini`、`deepseek-chat`、
`claude-sonnet`…），也可以在 `billing.price` 里写死。本地模型没有价格 = 只记
token 不算钱。

**滚动窗口额度（订阅制的墙）** —— Claude Pro/Max 这种没有单价、只有 5h / 7d 的
用量墙。写 `billing.quotas` 就能看到自己离墙多远：

```yaml
quotas:
  - span: 5h          # 支持 m / h / d / w
    tokens: 200000
  - span: 7d
    tokens: 1000000
```

- 用量过 80% 打一条 warning，过 100% 再打一条，每个窗口每档只喊一次
- `/doctor` 里能看到累计 `llm.prompt_tokens` / `llm.completion_tokens` / `llm.cost_usd`，
  以及每个窗口已用多少
- `billing.usage` 落一行一条流水，重启后窗口接着算；超出最长窗口的老流水会被丢掉

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
不是只知道"有张图"。前提是模型和模板支持多模态——判断方法：发一张纯色图问它颜色，
能答对就是通的。

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
