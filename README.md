# GensokyoAI v2

幻想乡 AI 角色扮演引擎 v2。一个房间 = 一条只追加的聊天流，它是全系统唯一事实源，
其余器官围着它转。

## 控制流

房间不是"来消息就回一句"的管线。模型每轮拿到的是 `{now, events, gap, history, memories, results}`，
它自己通过工具决定：说不说、说几条、等多久、要不要翻看错过的消息。

| 工具 | 作用 |
|---|---|
| `send_message` | 发一条。想连说几句就多调几次，出口按 `bubble_gap` 间隔逐条投递 |
| `wait` | 等 `ms` 毫秒或等下一条消息。没有人叫它时，它可以选择不说话 |
| `read_events` | 展开 `gap`（一次投递窗口之外被折叠的消息） |

模型输出的正文会被丢弃，只有工具调用生效。

## 插件与场景

场景 = 一个房间"怎么算像人"的那些策略：名词、连发几条、允许哪些动作、话怎么发出去。
机制（回合、未读、gap、记忆）全留在内核。场景可以写在插件里，库负责发现并挂载。

```python
# my_qq.py —— 不需要 import 内核，只需要这个模块
from gensokyoai.api import scene, tool

API_VERSION = "1"  # 和库握手；对不上就不挂载


@scene(
    name="qq",
    prompt="你在一个 QQ 群里，没人点你就别插话。",
    bubbles={"max": 3, "gap_s": 0.4},
    actions=("say", "wait", "read_events"),
)
class QQScene:
    def __init__(self, ctx):  # ctx.mouth / ctx.name / ctx.room / ctx.log
        self.ctx = ctx

    @tool("read_member", "读一个人的群名片", {"user_id": "string"})
    async def read_member(self, user_id):
        return [{"name": "帕秋莉", "text": "不动的大图书馆"}]  # 回填到下一轮 results

    def describe(self, event):  # 可选：改写成模型该看到的形状
        return event

    async def say(self, segments, reply_to=None):
        text = "".join(s["text"] for s in segments if s["type"] == "text")
        await self.ctx.mouth.send(self.ctx.name, text)
```

```powershell
python -m gensokyoai --list-scenes                              # 看有哪些
python -m gensokyoai --scene qq --plugin-dir ./plugins ...      # 用哪个
```

- 发现两条通道：`gensokyoai.plugins` 组的 entry points（pip 装的），和 `--plugin-dir` 里的 `.py`。
- 坏插件只记日志跳过；版本对不上会卸掉该插件的场景；重名直接报错。
- 场景工具的结果进下一轮 `results` 字段，不用自己维护对话状态。

## 设计约定

- **房间即状态。** 所有器官读它、所有产出写它，只追加不改写。房间自己记住哪些
  还没投递给模型；超出一轮窗口的折成 `gap`，由模型决定要不要展开。
- **机制在内核，策略在场景。** 回合、未读、gap、记忆、重试是机制；名词、连发条数、
  允许的动作、话怎么发出去是策略。插件只能提供策略，不许自己写循环。
- **器官之间不直接说话。** 只有 `loop.py` 认识所有器官；`mind` 不引用 `mouth`，
  `memory` 不引用 `hub`。
- **循环不因单轮失败退出。** 后端断线、投递失败、落盘失败都只是这一轮没成，
  按指数退避重试，没消费的消息退回未读；房间任务意外退出后 `Hub.get()` 会原地重启。
- **出口能力分层。** `Mouth` 只要 `send`；`BroadcastMouth` 另能挂/摘连接、声明风格。
  网关按能力判断，接错 mouth 只告警。
- **信任边界只在 `Gateway`。** token 用常量时间比较，`style`/`role` 只信回环地址，
  新频道受 `max_rooms` 限制，断开路径不创建房间。

## 模块

| 层 | 模块 | 职责 |
|---|---|---|
| 内核 | `room.py` | 事件流（append-only，有上界）+ 未读/gap + 落盘与回放 |
| | `mind.py` | 人设 + 场景 + 机制 → 提示词；一次 step 一次模型调用 |
| | `model.py` | OpenAI 兼容客户端，原生工具调用 |
| | `memory.py` | JSONL 记忆，bigram 召回，整理时压缩 |
| | `style.py` / `character.py` | 回复风格预设 / 角色卡加载 |
| | `utils/` | 日志桥接、文本清洗 |
| 插件 | `api.py` | 插件作者要的全部声明与装饰器（不依赖内核） |
| | `plugin.py` | 发现与挂载（entry points + 目录） |
| | `scenes/` | 内置场景，用同一套 API 写 |
| 组装 | `loop.py` | 唯一认识所有器官的地方 + agent 回合 |
| | `hub.py` | room_id → Loop 注册表 |
| | `health.py` | 计数 / 采样 / 读数 |
| | `bgtasks.py` | 后台任务登记与退出收尾 |
| | `dmn.py` | 静息期巡检（记忆压缩） |
| 边界 | `ws_server.py` | 握手与事件策略 |
| | `command/` | 斜杠指令（不进房、不调模型） |
| | `network/base.py` | 传输层 Protocol 声明 |
| | `network/aiohttp_ws.py` | aiohttp 适配器 |
| 入口 | `__main__.py` | 控制台 / ws 服务 |

## 运行

需要一个支持工具调用的 OpenAI 兼容端点，默认 `http://127.0.0.1:8080/v1`
（llama-server 需关掉思考，否则输出会被思考段吃光）。

```powershell
python -m gensokyoai --character config/roles/KomeijiSatori.yaml
python -m gensokyoai --character config/roles/KomeijiSatori.yaml --serve --port 8082 --token 你的令牌
```

参数：`--model URL`、`--model-timeout 秒`、`--data 落盘目录`、`--max-rooms 房间上限`、
`--window 一次投递多少条未读`。ws 协议是 `ws://host:port/ws/{channel}`。

## 测试

```powershell
python -m pytest -q          # 全离线，约 2 秒
python -m ruff check .
python -m ruff format --check .
```

`ChatModel` 在测试里用假会话替换，心智用脚本化的假 `Mind`，网关用假 `Pending`/事件
直接驱动。两个冒烟脚本需要真模型：

```powershell
$env:GENSOU_CARD = "path/to/KomeijiSatori.yaml"
$env:GENSOU_MODEL = "http://127.0.0.1:8080/v1"
python smoke_full.py
```

## 已知限制

- 房间无空闲回收，只受 `--max-rooms` 限制。
- 房间日志按 4 MB 轮转到 `.1`，只留一份备份；回放只读当前文件末尾一段。
- 落盘不逐条 fsync（一次 flush 约 60 ms），进程被强杀可能丢最后几条；
  正常退出会先把排队写入收尾再关句柄。
- 记忆召回的结果放在 `memories` 字段里，不进房间。
- 落盘的只有事件和记忆：未读/gap 状态、工具结果、`dmn` 的"这一觉整理过"都在内存里，
  重启后未投递的消息会以 `history` 形式补给模型一次。
- 静息整理由 `bgtasks` 每 60 秒巡检，房间静默 30 分钟且记忆过压力线才压缩。
- 只有动作类工具（说话/等待/翻看）。没有"查资料"类工具——要加得让工具结果
  回到对话里，`Mind` 现在每轮都是一次独立调用，不留对话状态。
- `ReplyStyle.pov` 只是预设里的标识，出口不据此改写文本。
- 没有 CI，也没有 mypy/pyright 配置。
