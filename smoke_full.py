"""v2 全器官冒烟：Loop 热路径（含工具回合）+ ws 服务（插件协议）。

用法（llama-server 在线时）：
  $env:GENSOU_CARD = "path/to/KomeijiSatori.yaml"   # 缺省找 config/roles/ 下的觉卡
  $env:GENSOU_MODEL = "http://127.0.0.1:8080/v1"
  python smoke_full.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gensokyoai.__main__ import _build_mind, _loop_factory
from gensokyoai.character import load_card
from gensokyoai.hub import Hub
from gensokyoai.mouth import WsMouth
from gensokyoai.network.aiohttp_ws import AiohttpWebsocket, AiohttpWebsocketServer
from gensokyoai.network.base import Json
from gensokyoai.ws_server import Gateway

CARD = Path(os.environ.get("GENSOU_CARD", "config/roles/KomeijiSatori.yaml"))
MODEL = os.environ.get("GENSOU_MODEL", "http://127.0.0.1:8080/v1")
TURN_WAIT_S = float(os.environ.get("GENSOU_TURN_WAIT", "40"))


class _PrintSink:
    async def send(self, frame: dict) -> None:
        print(f"{frame.get('speaker')}: {frame.get('text')}")


def _load():
    if not CARD.exists():
        raise SystemExit(
            f"角色卡不存在: {CARD}\n"
            "用环境变量 GENSOU_CARD 指定卡面路径，例如：\n"
            '  $env:GENSOU_CARD = "D:/path/to/KomeijiSatori.yaml"'
        )
    return load_card(CARD)


async def main() -> None:
    card = _load()
    mind = _build_mind(MODEL, card.persona, card.name)

    # --- 热路径（含工具回合） ---
    loop = _loop_factory(mind, lambda _id: WsMouth(), None)("smoke")
    loop.mouth.attach(_PrintSink())
    task = asyncio.create_task(loop.run())
    for text in ("你好啊，觉", "现在几点了？"):
        print(f"帕秋莉: {text}")
        loop.notify("message", "帕秋莉", text, user_id="paqiuli", mentioned=True)
        await asyncio.sleep(TURN_WAIT_S)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # --- ws 服务（插件协议；客户端也吃自己的声明） ---
    hub = Hub(_loop_factory(mind, lambda _id: WsMouth(), None))
    gateway = Gateway(hub)
    server = AiohttpWebsocketServer("127.0.0.1", 8099, validate=gateway.validate)
    serving = server.run()
    consumer = asyncio.create_task(gateway.run(server))
    await asyncio.sleep(1)
    client = AiohttpWebsocket(
        "ws://127.0.0.1:8099/ws/wscheck?style=pure_dialogue&user=测试员"
    )
    try:
        await client.send({"text": "觉，在吗", "direct": True})

        async def _first_message():
            async for frame in client:
                if isinstance(frame, Json) and frame.data.get("type") == "message":
                    return frame.data

        frame = await asyncio.wait_for(_first_message(), timeout=90)
        print(f"[ws] {frame.get('speaker')}: {frame.get('text')}")
    except TimeoutError:
        print("[ws] 超时")
    finally:
        await client.close()
        consumer.cancel()
        serving.cancel()
        await asyncio.gather(consumer, serving, return_exceptions=True)
        await hub.shutdown()
        await mind.close()


if __name__ == "__main__":
    asyncio.run(main())
