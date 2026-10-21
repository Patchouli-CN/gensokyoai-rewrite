# GensokyoAI

幻想乡角色的扮演引擎，为本地小模型优化——不等云端大模型，一台 12G 显存的机器就能让觉大人开口。

终端、群聊、酒馆、跑团都是场景：场景是插件，房间是通用的——一个群、一个私聊、一个终端，都是一间房。房间是唯一事实源：所有消息进房间，所有产出回房间，记忆、发言、注记都是房间里的追加事件。没有剧本，没有决策管线，模型读到什么就接什么。

## 要求

- Python ≥ 3.12
- 一个 OpenAI 兼容端点（llama-server、vLLM 等，本地即可）
- 端点支持原生 tool_call（说话靠 `send_message` 工具调用）

## 安装

```bash
uv pip install -e .
# 开发依赖
uv pip install -e . pytest pytest-asyncio ruff
```

## 跑起来

```bash
# 控制台聊天
python -m gensokyoai --character config/roles/KomeijiSatori.yaml

# WebSocket 服务（每个频道一间房，接 bot 框架走这里）
python -m gensokyoai --character config/roles/KomeijiSatori.yaml --serve --port 8082
```

ws 接入：`ws://127.0.0.1:8082/ws/{频道名}?style=pure_dialogue&user=你的名字`，
发 `{"text": "...", "user": "...", "direct": true}`，收 `{"type": "message", "speaker": "...", "text": "..."}`。
`style` 可选 `free / pure_dialogue / tavern / rpg`——只投递、不投递叙述，这层管。

## 角色卡

一份 YAML 三个字段，设定全在 `detail` 里，不做花哨键值对：

```yaml
version: "0.0.1"   # 卡面版本号，自己改卡时自己 bump
name: 古明地觉
detail: |
  你是古明地觉，地灵殿的主人……
```

格式约束（纯对话/叙述许可）归场景层，卡只写人设。示例卡在 `config/roles/`。

## 场景插件

场景 = 这个房间是什么地方、能干什么。一个 `.py` 就是插件：

```python
from gensokyoai.api import scene, tool


@scene(name="qq", prompt="你在一个 QQ 群里。", bubbles={"max": 3, "gap_s": 0.4})
class QQScene:
    def __init__(self, ctx):
        self.ctx = ctx  # ctx.mouth / ctx.name / ctx.room / ctx.log

    @tool("read_member", "读一个人的群名片", {"user_id": "string"})
    async def read_member(self, user_id):
        return [{"name": "帕秋莉", "text": "红魔馆的魔法使"}]

    async def say(self, segments, reply_to=None):
        await self.ctx.mouth.send(
            self.ctx.name, "".join(s["text"] for s in segments if s["type"] == "text")
        )
```

`--plugin-dir plugins/` 扫目录挂载，或打包时用 entry point（`gensokyoai.plugins` 组）自动发现。
`--list-scenes` 看当前挂了哪些场景。内置 `chat` 场景开箱即用。

## 架构一句话

Room（追加事件流 + ayafileio 落盘回放）· Loop（房间循环）· Mind（组提示词，拿原生 tool_call）·
Mouth（投递出口，风格在这层生效）· Memory（bigram 召回，窗口压力压缩）·
DMN（房间安静了才整理）· BgTasks（后台任务登记处）· Gateway（ws 握手与信封）。

详见 [docs/architecture.md](docs/architecture.md)。

## 开发

```bash
pytest -q
ruff check . && ruff format --check .
```

## License

MIT
