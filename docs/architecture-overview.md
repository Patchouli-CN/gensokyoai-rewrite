# 整体架构（引擎 + 插件 + 外部）

`architecture.md` 讲的是引擎内核（房间、回合、器官）。这份讲**整套部署**：
消息从 QQ 到模型再回到 QQ，中间经过谁，状态存在哪里。

---

## 一、分层

```mermaid
flowchart TB
    subgraph outside["外部"]
        QQ["QQ 客户端<br/>小号 3779163297"]
        NC["NapCat<br/>注入 QQ 进程 · OneBot v11"]
        API["模型端点<br/>api.deepseek.com"]
        WEB["公网网页<br/>（抓取工具）"]
    end

    subgraph bridge["nb2_plugin（部署单元 · 独立 git）"]
        H["__init__.py<br/>消息入口 / 指令分流 / 日志 / 掉线告警"]
        MEDIA["images · quoted · forward<br/>图 / 引用 / 合并转发 / 语音"]
        GUARD["guards · repeat_guard · roles<br/>已答覆盖 / 复读冷却 / 权限"]
        WATCH["link · session<br/>在线检测 / 会话账本"]
        SUP["napcat.py<br/>进程守护（未接线）"]
        SCENE["scene.py<br/>QqScene：说话 / 连发 / 场景工具"]
        ENG["engine.py<br/>在 nb2 进程里装配引擎"]
    end

    subgraph core["gensokyoai（框架 · 独立 git）"]
        HUB["Hub<br/>一房一 Loop，盯着任务别死"]
        LOOP["Loop<br/>唤醒判定 → 组上下文 → 调模型 → 执行动作"]
        MIND["Mind<br/>人设 + 工具声明 + 能力路由"]
        ROOM["Room / RoomLog<br/>事件流 / 未读 / 上下文窗口"]
        MEM["Memory<br/>bigram 召回 · 静息压缩"]
        TOOLS["tools/<br/>lookup · web_search · fetch_url"]
        CMD["command/<br/>/doctor /balance /help"]
        HL["health<br/>计数器 / 延迟分位"]
    end

    subgraph llm["llm/（模型层）"]
        MGR["LLMManager<br/>按任务取端点 · 中文房间转述 · 语音转写"]
        CM["ChatModel<br/>provider 适配 · 账本 · 能力门控"]
        BILL["billing<br/>峰谷 / 一口价 · 窗口额度"]
        PROV["openai · anthropic · deepseek"]
    end

    subgraph store["存储"]
        ST["Storage<br/>队列 + worker + 空闲落盘 + 轮转"]
        FS["data/&lt;room&gt;/events.jsonl · media/<br/>memory.jsonl · knowledge/ · usage.jsonl"]
    end

    QQ --> NC -->|"反向 WS（对方连进来）"| H
    H --> GUARD --> MEDIA --> ENG --> HUB --> LOOP
    LOOP --> MIND --> MGR --> CM --> PROV --> API
    MGR --> BILL
    LOOP --> ROOM --> ST --> FS
    LOOP --> MEM --> FS
    LOOP --> TOOLS --> WEB
    H --> CMD
    H --> WATCH
    WATCH -.->|"实现完成、默认关闭"| SUP
    SUP -.-> NC
    MIND -->|"工具执行"| SCENE
    SCENE -->|"send_group_msg"| NC
    HL --- LOOP
```

---

## 二、一条消息的旅程

```mermaid
sequenceDiagram
    autonumber
    participant U as 群友
    participant N as NapCat
    participant P as 插件
    participant E as Engine
    participant L as Loop
    participant M as Mind
    participant G as LLMManager
    participant S as 场景

    U->>N: 发图 + @觉 + 「她是谁」
    N->>P: OneBot 事件（反向 WS）
    P->>P: 白名单 / 复读 / 已答
    P->>E: command(raw)　← 只看用户打的字，引用不参与
    alt 斜杠指令
        E-->>P: 「系统」回复
        P->>N: send_group_msg
    else 普通消息
        P->>P: 取图 / 解析引用 / 语音转写 / 转发渲染
        P->>E: ingest(room, sender, text, images)
        E->>L: notify → 点名？概率？冷却？
        L->>L: 未读 + 历史 + 记忆召回
        L->>M: step(feed, scene, images)
        alt 主模型没有视觉
            M->>G: describe(images)
            G-->>M: 「粉紫色头发、心形帽饰……」
            M->>M: 塞进 feed["images"]
        end
        M->>G: get("chat")
        G-->>M: Reply（正文 / 工具调用）
        M->>S: say(text)
        S->>N: send_group_msg
        N->>U: 回复出现
        L->>L: 事件与回复入队落盘
    end
```

---

## 三、模块职责

### 引擎

| 模块 | 干什么 | 明确不做 |
|---|---|---|
| `hub.py` | 一房一 `Loop`，任务死了能重启 | 不懂平台 |
| `loop.py` | 一轮编排：唤醒、上下文、重试、说话 | 不懂 QQ |
| `mind.py` | 人设、工具声明、能力路由 | 不碰存储 |
| `room.py` | 事件流、未读、窗口、`RoomLog` | 不调模型 |
| `storage.py` | 追加写 + 队列 + worker + 空闲落盘 | 不懂业务 |
| `memory.py` | 召回与压缩 | — |
| `model.py` | provider 适配、重试、账本、能力门控 | 不做路由 |
| `llm/manager.py` | 任务 → 端点、转述、转写 | 不管单次调用 |
| `llm/bootstrap.py` | 建心智（CLI 与插件共用） | — |
| `command/` | 解析、权限、去重、内置指令 | 不进房 |
| `tools/` | 三层查找、抓网页 | 不懂 QQ |
| `bgtasks.py` | 一次性 / 周期 / 常驻任务 + 收尾 | — |

### 插件

| 模块 | 干什么 |
|---|---|
| `__init__.py` | 消息入口；指令分流；日志落地；掉线告警；启动/收尾 |
| `engine.py` | 装配引擎；`prepare()` 建心智；`transcribe()` 语音转写 |
| `scene.py` | `QqScene`：怎么说话、连发、唤醒参数、`read_member` |
| `images.py` | 取图 / 取语音 / 视频封面；失败留标记 |
| `quoted.py` | 引用（直接用 nb2 填好的 `event.reply`） |
| `forward.py` | 合并转发 → 文字 |
| `repeat_guard.py` / `guards.py` / `roles.py` | 复读冷却 / 已答覆盖 / 权限 |
| `link.py` / `session.py` | 在线状态、告警 / 会话账本（风控样本） |
| `napcat.py` | `bot_exit` → launcher → 等 WS 回连（未接线） |

---

## 四、状态放在哪

```text
config/
  llm/providers.yaml   厂商（base_url、key 从哪读、client_type）
  llm/models.yaml      模型（能力、峰谷价、extra）
  llm/tasks.yaml       chat / vlm / asr / cheap → 模型 id
  llm/billing.yaml     计费窗口
  llm/keys/            key（.gitignore）
  settings.yaml        只剩计费覆盖
  tools/<tool>/sites.yaml   权威站 allow / prefer / block
  roles/<角色>.yaml    人设

nb2_plugin/.env        GSK_* 运行参数

data/
  <room>/events.jsonl  事件流（图只在 media/）
  <room>/media/        最近 60 张
  <room>/memory.jsonl  长期记忆
  knowledge/global.jsonl   查过的资料
  usage.jsonl          计费流水
  link.jsonl           会话账本
  engine.log           引擎格式日志（只收 gensokyoai*）
```

房间名任意，落盘时经 `paths.storage_key()` 转义（非法字符编码、禁止穿越、超长加哈希）。

---

## 五、生命周期

| 东西 | 谁管 | 死了的影响 |
|---|---|---|
| 房间循环 | `Hub`（`add_done_callback` 监督） | 该房静默，下次收消息重启 |
| 引擎后台任务 | `BgTasks` | 退出时按序取消并清理 |
| 存储 worker | `Storage` + `BgTasks.spawn_worker` | 写入故障传给 Future / `sync` / `aclose`，不伪装成功 |
| 静息整理 | `DmnKeeper`（60s 巡逻） | 记忆不压缩，对话照常 |
| 协议端 | 插件 `link.py`（30s 巡检） | 只告警，不自动拉起 |

并发模型：单个事件循环 + 房间隔离；跨房间共享只有模型端点和任务登记处。
`Mind` 为每房派生独立 `PromptCache`，模型端点仍共享；缓存历史、压缩和未读状态不跨房。

---

## 六、四条不变量

1. **平台差异止步于插件**：引擎只认 `text` / `images` / `say`；转发、引用、@、语音全在插件翻译。
2. **能力缺了要降级**：无视觉 → vlm 转述；转述失败 → 当没图；媒体取不到 → 留标记；handler 出错 → 纯文本再入房，且两条路径都先过群白名单。
3. **队列 + 批量落盘**：`write()` 先提交队列，worker 写穿；`sync()` / `aclose()` 等待并报告故障。`flush()` 是 `FlushFileBuffers`（40–120 ms），所以空闲 6 秒或脏 30 秒才刷。
4. **配置只有一处**：模型在 `config/llm/`，`settings.yaml` 只剩计费覆盖、前缀缓存与静息参数。

---

## 七、三种宿主

```mermaid
flowchart LR
    A1["console<br/>stdin / stdout"] --> L["Loop"]
    A2["serve<br/>ws_server.Gateway"] --> L
    A3["nb2 插件<br/>OneBot 反向 WS"] --> L
    L --> SH["同一套 Room / Mind / LLMManager / Storage"]
```

同一个引擎，三种接法：终端、通用 WS 网关、QQ。插件是最厚的那层适配，引擎不知道 QQ 存在。
