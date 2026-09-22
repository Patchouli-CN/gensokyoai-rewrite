"""WS 游戏客户端 —— 与运行中的 gensokyoai-ws 服务里的角色真实对话（真机马拉松测试）。

用途：
- 长对话压力/体验测试：风格是否单调、记忆能否跨轮召回、延迟趋势；
- 结果写 JSONL（每轮：发出内容、角色回复、耗时、回复长度），供事后分析。

用法：
    python scripts/ws_play.py --user 灵梦 --channel play1 --rounds 5        # 内置话题池
    python scripts/ws_play.py --scenario my.json --out logs/play.jsonl      # 自定义消息序列
    python scripts/ws_play.py --rounds 200 --max-seconds 7200               # 马拉松（带总时限）

场景 JSON：[{"text": "..."}] 或纯字符串数组。注意：WS 服务端只按连接类型区分
群聊/私聊（?type=），不对文本里的 @ 做解析——想测「被直球」场景请用 --scene private。
"""

import argparse
import asyncio
import contextlib
import json
import time
from pathlib import Path

import aiohttp

# 内置话题池：寒暄/食物/哲学/情感/往事/调侃/知识混合，避免话题单调
_TOPICS = [
    "在吗？",
    "早上好呀",
    "今天冥界的天气怎么样？",
    "你平时都喜欢吃什么点心？",
    "我觉得博丽神社的团子一般般啦",
    "给你讲个笑话：有一天妖怪也去排队买团子",
    "你说死亡是什么感觉？",
    "西行妖今年会开花吗？",
    "妖梦又惹你生气了吗？",
    "紫最近在睡大觉吗？",
    "你怎么看永远这个词？",
    "给你猜个谜语：什么东西越吃越饿？",
    "夜晚的白玉楼是什么样子的？",
    "你会寂寞吗？",
    "给你讲个今天遇到的事：我又把赛钱箱里的钱用完了",
    "你怀念活着的时候吗？",
    "幽幽子你今年多大了呀？",
    "你说幻想乡之外会是什么样子？",
    "如果让你选，你想做普通人还是亡灵公主？",
    "春雪和冬雪你更喜欢哪个？",
    "白玉楼里你最爱的角落是哪里？",
    "你会做什么噩梦吗？",
    "死亡和睡觉有什么相似之处？",
    "你饲养过幽灵吗？",
    "你最喜欢哪个季节的宴会？",
    "我们来玩词语接龙吧，我先来：白玉楼",
    "你能给我讲个一千年前的故事吗？",
    "时间对亡灵来说有意义吗？",
    "你觉得幸福是什么？",
    "如果朋友忘了你，你会难过吗？",
]


class _Player:
    """WS 对话客户端：发一条、收一轮（流式帧聚合成整条回复）、记账。"""

    def __init__(self, url: str, user: str, scene_type: str = "group") -> None:
        self._url = url
        self._user = user
        self._scene_type = scene_type
        self.rounds: list[dict] = []

    async def __aenter__(self) -> _Player:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._url, heartbeat=30)
        self.startup = await self._drain_startup()
        return self

    async def __aexit__(self, *_) -> None:
        await self._ws.close()
        await self._session.close()

    async def _drain_startup(self) -> list[str]:
        """排空连接后的启动广播（角色开场白）——不排掉，它会冒充第 1 轮回复。

        世界 attach 时立即启动并广播 greeting，与客户端「连上就发消息」竞争，
        谁先到帧谁赢。这里用短窗口收干启动帧再开测。

        Returns:
            list[str]: 被排掉的启动帧文本（仅作记录）
        """
        drained: list[str] = []
        while True:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=3.0)
            except TimeoutError:
                break
            if msg.type is aiohttp.WSMsgType.TEXT:
                with contextlib.suppress(json.JSONDecodeError):
                    drained.append(str(json.loads(msg.data).get("text", "")))
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        return drained

    async def play(self, text: str, timeout: float | None = None, probe: str = "") -> str:
        """发一条消息，等角色回复完整返回。timeout 到点记「无回复」而不是干等。"""
        started = time.monotonic()
        await self._ws.send_str(text)

        parts: list[str] = []
        try:
            async for msg in self._ws:
                if timeout is not None and time.monotonic() - started > timeout:
                    raise TimeoutError
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                frame = json.loads(msg.data)
                ftype = frame.get("type")
                if ftype == "message":
                    parts = [frame.get("text", "")]
                    break
                if ftype == "begin":
                    parts = []
                elif ftype == "delta":
                    parts.append(frame.get("text", ""))
                elif ftype == "end":
                    break
        except TimeoutError:
            parts = ["[本轮无回复，疑似被门控跳过]"]
        reply = "".join(parts)
        elapsed = time.monotonic() - started
        self.rounds.append(
            {
                "text": text,
                "probe": probe,
                "reply": reply,
                "elapsed_s": round(elapsed, 1),
                "chars": len(reply),
            }
        )
        return reply


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WS 真机马拉松客户端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--channel", default="play1")
    parser.add_argument("--user", default="灵梦")
    parser.add_argument("--scene", default="group", choices=["group", "private"])
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument(
        "--round-timeout", type=float, default=180.0, help="单轮等待秒数，到点记无回复继续下一轮"
    )
    parser.add_argument("--max-seconds", type=float, default=3600.0, help="总时限，到点收尾")
    parser.add_argument("--scenario", default=None, help="JSON 消息文件")
    parser.add_argument("--out", default=None, help="JSONL 结果输出（缺省打屏）")
    return parser


async def run(args: argparse.Namespace) -> int:
    texts: list[str] = []
    probes: list[str] = []
    if args.scenario:
        items = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
        texts = [item["text"] if isinstance(item, dict) else item for item in items]
        probes = [item.get("probe", "") if isinstance(item, dict) else "" for item in items]
    else:
        texts = [(_TOPICS * 20)[i] for i in range(args.rounds)]
        probes = [""] * len(texts)

    url = f"http://{args.host}:{args.port}/ws/{args.channel}?user={args.user}&type={args.scene}"
    started = time.monotonic()
    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    async with _Player(url, args.user, args.scene) as player:
        for line in player.startup:
            print(f"--- [启动广播已排空] {line}", flush=True)
        for index, text in enumerate(texts, 1):
            if time.monotonic() - started > args.max_seconds:
                print(f"[play] 到总时限，收尾（已完成 {index - 1} 轮）")
                break
            print(f"\n=== [{index}/{len(texts)}] 我: {text}", flush=True)
            if index <= len(probes) and probes[index - 1]:
                print(f"    探针: {probes[index - 1]}", flush=True)
            reply = await player.play(
                text, args.round_timeout, probes[index - 1] if index <= len(probes) else ""
            )
            if out_path:
                # 逐轮追加：长跑中途被杀也保住已完成轮次（马拉松跑了数小时，尾部才写等于裸奔）
                with out_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(player.rounds[-1], ensure_ascii=False) + "\n")
            elapsed = player.rounds[-1]["elapsed_s"]
            print(f"--- 她: {reply}", flush=True)
            print(f"--- ({elapsed}s, {len(reply)}字)", flush=True)

    # 汇总
    rounds = player.rounds
    if rounds:
        avg = sum(r["elapsed_s"] for r in rounds) / len(rounds)
        avg_chars = sum(r["chars"] for r in rounds) / len(rounds)
        print(f"\n===== {len(rounds)} 轮汇总 =====")
        print(f"平均延迟: {avg:.1f}s | 平均回复长度: {avg_chars:.0f}字")
        uniq = len({r["reply"][:20] for r in rounds})
        print(f"回复开头去重: {uniq}/{len(rounds)}")

    # 重复输入复读检测：同一文本发多次时，回复是否原样复读
    by_text: dict[str, list[str]] = {}
    for r in rounds:
        by_text.setdefault(r["text"], []).append(r["reply"])
    repeats = {t: rs for t, rs in by_text.items() if len(rs) > 1}
    if repeats:
        print("\n===== 重复输入复读检测 =====")
        for text, rs in repeats.items():
            variants = len(set(rs))
            verdict = "复读!" if variants == 1 else f"{variants} 种不同回复"
            print(f"[{text}] x{len(rs)} -> {verdict}")
    if out_path:
        print(f"结果已写: {out_path}（{len(rounds)} 轮，逐轮追加）")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[play] 手动中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
