# 架构

## 核心：房间即事实源

一间房（`Room`）是一条只追加的事件流。一个群、一个私聊、一个终端，都是一间房。
事件只有三种：`message`（别人的话）、`reply`（角色发言）、`note`（系统注记：记忆召回、工具结果）。

所有器官读房间，所有产出写房间。器官互相不见面，没有 event bus——日志本身就是状态。
房间事件经 `RoomLog` 落盘（ayafileio，append-only JSONL），重启回放；`note` 是运行时派生物，不落盘。

## 一个回合

```
ws 连接 → Gateway（握手：频道/token/风格/房间上限）
        → Loop.notify（事件入房，唤醒循环）
        → Mind.step（feed JSON → 模型 → 原生 tool_call）
        → 动作执行：say → 场景.say → Mouth 投递 → 入房落盘
                   wait → 等下一条消息
                   read_events → 展开跳过的消息段
                   场景工具 → 结果回填 feed 再来一轮
```

模型每轮拿到的是一段 JSON feed：`now / events（未读）/ gap（跳过段）/ memories / results`，
返回的是工具调用，不是正文——正文会被丢弃，要说话只能调 `send_message`。

## 器官

| 模块 | 干什么 |
| --- | --- |
| `room.py` | 事件流 + RoomLog 落盘回放 |
| `loop.py` | 房间循环：唤醒、喂模型、执行动作；单轮失败退避重试 |
| `hub.py` | 房间注册表，一间房一个 Loop |
| `mind.py` | 组提示词（人设 + 场景 + 机制），调模型，返回动作 |
| `model.py` | OpenAI 兼容端点客户端，解析 tool_calls |
| `mouth.py` | 投递出口：console 打印 / ws 广播；ReplyStyle 在这层生效 |
| `style.py` | 回复风格预设（free / pure_dialogue / tavern / rpg） |
| `memory.py` | 记忆：bigram 召回、窗口压力压缩（调模型写概要） |
| `dmn.py` | 静息整理：房间静默超阈值才跑整理任务 |
| `bgtasks.py` | 后台任务登记处：一次性 / 周期，统一监督与收尾 |
| `health.py` | 指标集散地：gauge / counter / sample，/doctor 出报告 |
| `ws_server.py` | Gateway：握手策略 + 事件消费 |
| `network/` | 传输层：async-iterable 事件源，不识应用层 |
| `command/` | 斜杠命令：注册表 + 权限，/doctor /help |
| `api.py` | 插件 API：`@scene` / `@tool`，不依赖内核 |
| `plugin.py` | 插件发现：entry points + 目录扫描 |

## 场景

场景声明一个房间是什么地方、模型能用哪些动作、连发几条泡。
内核动作只有三个：`say / wait / read_events`，场景可以只挑几个，再加自己的 `@tool`。

插件只需要 `import gensokyoai.api`，不需要 import 内核。

## 约束（写代码前读）

- 房间只追加，不改写——前缀稳定，缓存友好
- 风格（叙述许可、视角）归出口层，人设卡不参与
- 模型能报错教学就不写正则抢救；接口有真实消费者才存在
