"""v2 最小内核冒烟：觉卡 + 一个房间，看它怎么接话。

用法（llama-server 在线时）：
  $env:GENSOU_CARD = "path/to/KomeijiSatori.yaml"   # 缺省找 config/roles/ 下的觉卡
  $env:GENSOU_MODEL = "http://127.0.0.1:8080/v1"
  python smoke.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gensokyoai.character import load_card
from gensokyoai.loop import Loop
from gensokyoai.mind import Mind
from gensokyoai.model import ChatModel
from gensokyoai.room import Room

CARD = Path(os.environ.get("GENSOU_CARD", "config/roles/KomeijiSatori.yaml"))
MODEL = os.environ.get("GENSOU_MODEL", "http://127.0.0.1:8080/v1")
TURN_WAIT_S = float(os.environ.get("GENSOU_TURN_WAIT", "40"))


class _PrintMouth:
    async def send(self, name: str, text: str) -> None:
        print(f"{name}: {text}")


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
    async with ChatModel(MODEL) as model:
        loop = Loop(
            Room("console"), Mind(model, card.name, card.persona), _PrintMouth()
        )
        task = asyncio.create_task(loop.run())
        try:
            for text in ("你好啊，觉", "最近有什么好看的推理小说推荐吗？"):
                print(f"帕秋莉: {text}")
                loop.notify(
                    "message", "帕秋莉", text, user_id="paqiuli", mentioned=True
                )
                await asyncio.sleep(TURN_WAIT_S)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
