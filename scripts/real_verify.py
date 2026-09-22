"""真·业务验证 —— 驱动本地 llama-server 跑完整世界循环（无假后端）。

与 tests/ 的分工：tests 用假后端验证「管路接对了」；本脚本用**真模型**验证
「水能流、水压够不够」：门控（LocalJudge 真调模型打分）/ 思考链（卡片四步 +
结论）/ Responder / OOC 深审 / 记忆落账 / 健康指标，全部走真实路径。

用法：
    python scripts/real_verify.py                        # 内置演示场景（群聊混合私聊）
    python scripts/real_verify.py --log-level DEBUG      # 看每步思考 note 与裁判概率原文
    python scripts/real_verify.py --scenario s.json      # 自定义场景（见下）
    python scripts/real_verify.py --fresh                # 清空该会话记忆后重开一局
    python scripts/real_verify.py --initiative           # 末尾验证主动发言（破冰）路径

场景 JSON（消息数组；text 必填，其余可选）：
    [{"text": "哈哈哈哈哈", "gap": 3},
     {"text": "@幽幽子 在吗", "direct": true, "sender": "灵梦"},
     {"text": "晚上好呀", "private": true, "sender": "魔理沙"}]

记忆按 --session 持久化在 --storage（默认 data/verify，已 gitignore）：
同一个 session 反复跑，AI 会「记得」上次聊过什么 —— 跨运行的连续性也是验证项。
"""

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

# 从仓库根直接 `python scripts/xxx.py` 运行时，把根目录喂给 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gensokyoai.app import (  # noqa: E402
    DEFAULT_CONFIG,
    build_session_and_character,
    resolve_resource,
)
from gensokyoai.core.brain.judge import build_judge  # noqa: E402
from gensokyoai.core.config import load_config  # noqa: E402
from gensokyoai.eyes.queue import QueuePerceiver  # noqa: E402
from gensokyoai.mouth.broadcast import BroadcastMouth  # noqa: E402
from gensokyoai.roleplay.loop import TouhouWorld  # noqa: E402
from gensokyoai.schemas.scene_schema import SceneSnapshot  # noqa: E402
from gensokyoai.utils.logger import setup_logging  # noqa: E402

# 六条消息覆盖全部门控路径：收尾语规则 skip / 群聊 judge 决策 / 被@直判回复 /
# 深问题（HIGH 档 + 过渡语 + 四步思考链）/ 私聊必回 / 裸反应 skip
_DEFAULT_SCENARIO: list[dict] = [
    {"text": "哈哈哈哈哈哈", "sender": "灵梦", "gap": 3},
    {"text": "今天天气不错啊，适合睡午觉", "sender": "妖梦", "gap": 3},
    {"text": "@幽幽子 在吗？想问你个事", "sender": "灵梦", "direct": True, "gap": 3},
    {"text": "你觉得这个世界的本质是什么？", "sender": "帕秋莉", "direct": True, "gap": 3},
    {"text": "晚上好呀，今天也辛苦了", "sender": "魔理沙", "private": True, "gap": 3},
    {"text": "6", "sender": "灵梦", "gap": 3},
]


class _PrintSink:
    """把口层帧打印成对话形式（流式聚合为整条后打印）。"""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._speaker = ""
        self._buf: list[str] = []

    async def deliver(self, frame: dict) -> None:
        ftype = frame.get("type")
        if ftype == "message":
            self._emit(frame.get("speaker", "?"), frame.get("text", ""))
        elif ftype == "begin":
            self._speaker = frame.get("speaker", "?")
            self._buf = []
        elif ftype == "delta":
            self._buf.append(frame.get("text", ""))
        elif ftype == "end":
            self._emit(self._speaker, "".join(self._buf))
            self._speaker, self._buf = "", []

    def _emit(self, speaker: str, text: str) -> None:
        if not text:
            return
        self.messages.append(text)
        print(f"\n>>> 【{speaker}】{text}\n", flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="真模型业务验证（llama-server）")
    parser.add_argument("--config", default=None, help="配置文件（默认 config/settings.yaml）")
    parser.add_argument("--character", default=None, help="角色卡 YAML（默认包内幽幽子）")
    parser.add_argument("--session", default="verify", help="会话标识（记忆按它隔离，默认 verify）")
    parser.add_argument("--storage", default="data/verify", help="持久化根目录（默认 data/verify）")
    parser.add_argument("--scenario", default=None, help="场景 JSON 文件路径（缺省用内置演示场景）")
    parser.add_argument("--fresh", action="store_true", help="先清空该会话存储再跑")
    parser.add_argument(
        "--log-level", default="INFO", help="日志级别（DEBUG 看思考步骤与裁判原文）"
    )
    parser.add_argument("--turn-timeout", type=float, default=300.0, help="单回合等待超时（秒）")
    parser.add_argument(
        "--initiative", action="store_true", help="末尾验证主动发言路径（会临时放宽阈值）"
    )
    return parser


async def _wait_turn_done(world: TouhouWorld, timeout: float) -> bool:
    """等一个回合真跑完：感知队列排空 + 不在忙 + 至少沉降半秒。

    不能用「忙沿检测」：规则 skip 的回合 <300ms 就结束，忙标志可能在第一次
    轮询前就走完全程，那样会白等整个超时（表现为整场验证诡异地静止）。
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        queue_empty = world.eye.queue.qsize() == 0
        if queue_empty and not world._busy and time.monotonic() - started > 0.5:
            await asyncio.sleep(0.5)  # 记忆写入等是异步侧链，给半秒沉降
            return True
        await asyncio.sleep(0.3)
    return False


async def run(args: argparse.Namespace) -> int:
    setup_logging(args.log_level, log_console=True)

    scenario: list[dict] = _DEFAULT_SCENARIO
    if args.scenario:
        scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))

    config = load_config(args.config or resolve_resource(DEFAULT_CONFIG))
    sessions, character = build_session_and_character(
        config_path=args.config, character_path=args.character
    )
    judge = build_judge(config.gate, sessions)
    chain = character.card.think_chain
    print(
        f"[verify] 角色={character.name} 裁判={type(judge).__name__ if judge else '无（纯规则）'} "
        f"思考链={' >> '.join(chain) if chain else '（内置接力）'} 会话={args.session}",
        flush=True,
    )

    storage = Path(args.storage)
    if args.fresh and storage.exists():
        shutil.rmtree(storage)
        print(f"[verify] 已清空 {storage}", flush=True)

    sink = _PrintSink()
    mouth = BroadcastMouth()
    mouth.attach(sink)
    world = TouhouWorld(
        eye=QueuePerceiver(),
        character=character,
        sessions=sessions,
        mouth=mouth,
        judge=judge,
        gate=config.gate,
        storage_dir=storage,
        session_id=args.session,
        trace_steps=True,
        **(
            # 仅验证路径用：临时放宽主动发言阈值（默认 180s/0.35 在短会话里几乎不可能触发）
            {"initiative_interval": 5.0, "idle_threshold": 12.0, "urge_threshold": 0.15}
            if args.initiative
            else {}
        ),
    )
    await world.lifecycle.startup()
    task = asyncio.create_task(world.start())

    context: list[str] = []  # 滚动上下文（真实场景由适配器填，这里模拟）
    try:
        for index, item in enumerate(scenario, 1):
            sender = item.get("sender", "灵梦")
            text = item["text"]
            direct = bool(item.get("direct"))
            scene_type = "private_chat" if item.get("private") else "group_chat"
            print(
                f"\n=== [{index}/{len(scenario)}] {sender}{'（私聊）' if item.get('private') else ''}: {text} ===",
                flush=True,
            )
            world.eye.push(
                SceneSnapshot(
                    scene_type=scene_type,
                    sender=sender,
                    content=text,
                    is_direct=direct or scene_type == "private_chat",
                    context_snippet=context[-5:],
                    timestamp=time.time(),
                )
            )
            if not await _wait_turn_done(world, args.turn_timeout):
                print(f"[verify] 警告: 第 {index} 条超时未完成，继续下一条", flush=True)
            context.append(f"{sender}: {text}")
            context = context[-10:]
            await asyncio.sleep(item.get("gap", 2.0))

        if args.initiative:
            print("\n=== 静默期：等待主动发言评估（idle_threshold=12s）===", flush=True)
            before = len(sink.messages)
            await asyncio.sleep(30.0)
            if len(sink.messages) == before:
                print("[verify] 静默期内未触发主动发言（对话欲未过阈值，属正常）", flush=True)
    finally:
        world.eye.request_stop()
        await asyncio.wait({task}, timeout=30)
        await world._tasks.drain(timeout=5.0)

    usage = sessions.total_usage()
    print("\n========== 验证汇总 ==========", flush=True)
    print(f"模型 token: prompt={usage.prompt_tokens} completion={usage.completion_tokens}")
    cost = sessions.total_cost()
    if cost:
        print("累计费用: " + ", ".join(f"{c} {a:.6f}" for c, a in cost.items()))
    else:
        print("累计费用: （无 —— 本地模型不计价，或后端未实现 costs()）")
    per_owner = {owner: sessions.cost_by_owner(owner) for owner in sessions.owners()}
    per_owner = {owner: value for owner, value in per_owner.items() if value}
    print(f"各模块费用: {per_owner or '（无）'}")
    print(f"推理档位分布: {world.health.reasoning_distribution()}")
    print(f"responder 上下文占用: {world.sessions.context_usage('responder'):.1%}")
    recent = await world.memory.recent(10)
    print(f"最近记忆 {len(recent)} 条:")
    for item in recent:
        print(f"  - {item.content[:64]}")
    print(f"角色发言 {len(sink.messages)} 条；轨迹/快照在 {storage}/{args.session}/")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[verify] 手动中断", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
