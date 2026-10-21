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
  - 单次补全默认 120 s 超时（`--model-timeout` / `GSK_MODEL_TIMEOUT`）
- 角色卡：`version / name / detail` 三字段，缺任意一个 `load_card` 直接 `ValueError`
- 落盘（可选）：`--data` / `GSK_DATA` 指定根目录，每个房间两个文件

```
data/<房间名>/events.jsonl    聊天流水，4 MB 轮转到 .1
data/<房间名>/memory.jsonl    记忆条目
```

不给这个参数 = 纯内存，进程一退就忘。

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

GSK_MODEL=http://127.0.0.1:8080/v1
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
