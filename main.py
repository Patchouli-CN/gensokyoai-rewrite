"""GensokyoAI 入口 —— 组装一切 + 控制台调试主链路"""

import asyncio
import time
from pathlib import Path
from gensokyoai.brain.engine import BrainEngine, route
from gensokyoai.brain.ooc_detector import OOCDetector
from gensokyoai.core.bootstrap import discover_all
from gensokyoai.core.config import load_config
from gensokyoai.core.event_bus import EventBus
from gensokyoai.core.registry import Registry
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.eyes.perceiver import ConsolePerceiver
from gensokyoai.memorizer.manager import MemoryManager
from gensokyoai.responder.generator import Responder
from gensokyoai.roleplay import CharacterCard, load_character
from gensokyoai.schemas.event_schema import Topic
from gensokyoai.schemas.memory_schema import MemoryItem
from gensokyoai.schemas.model_schema import ModelConfig
from gensokyoai.utils.logger import LoggerManager, setup_logging

EXIT_WORDS = {"exit", "quit", "退出"}

async def run(config_path: str = "config/settings.yaml", character_path: str | None = None) -> None:
    """ Phase 1 主链路（架构文档 §8.1）：控制台版。

    snapshot -> memory 检索 -> brain.think -> responder.respond -> 异步记忆写入。
    编排放 L4 合法（L3 之间禁互 import），人设经 roleplay 加载后以字符串注入。
    """
    setup_logging("TRACE", False, Path("runtime.log"))
    log = LoggerManager.get_logger("MAIN")
    config = load_config(config_path)
    discover_all()

    character = load_character(character_path) if character_path else CharacterCard(
        name="未命名角色", system_prompt="你是一个用自然口语中文回复的角色。",
    )
    persona = character.to_system_prompt()

    bus = EventBus()
    provider_cls = Registry.get(config.model.provider)
    provider = provider_cls().config(ModelConfig(
        base_url=config.model.base_url,
        token=config.model.token,
        model_name=config.model.model_name,
        think=config.model.think,
        streaming=config.model.streaming,
        invoker_type=config.model.invoker_type,
        timeout=config.model.timeout,
    ))
    sessions = SessionManager(provider)
    memory = MemoryManager()

    async def _write_memory(event) -> None:
        """MEMORY_WRITE 侧链消费：落库，不阻塞主链路"""
        if isinstance(event.payload, MemoryItem):
            memory.store(event.payload)

    bus.subscribe(Topic.MEMORY_WRITE, _write_memory)

    brain = BrainEngine(sessions, persona=persona, ooc=OOCDetector(sessions))
    responder = Responder(sessions, persona=persona)
    perceiver = ConsolePerceiver()

    print(f"=== GensokyoAI 控制台 | 角色: {character.name} | 输入 exit 退出 ===")
    turn = 0
    while True:
        snapshot = await perceiver.next_snapshot()
        if snapshot is None:
            continue
        if snapshot.content.lower() in EXIT_WORDS:
            break

        turn += 1
        turn_started = time.monotonic()
        log.info(
            f"── 回合 {turn} 开始: 场景={snapshot.scene_type} "
            f"输入={snapshot.sender}: {snapshot.content!r}"
        )

        memories = memory.recent(5)
        effort = route(snapshot)
        t0 = time.monotonic()
        conclusion = await brain.think(snapshot, memories, effort)
        t1 = time.monotonic()
        reply = await responder.respond(conclusion, snapshot, memories)
        t2 = time.monotonic()
        print(f"\n{character.name}: {reply}\n")
        log.info(
            f"── 回合 {turn} 完成: 思考={t1 - t0:.2f}s 生成={t2 - t1:.2f}s "
            f"总计={t2 - turn_started:.2f}s 回复={len(reply)}字"
        )

        reply_item = MemoryItem(topic="对话", content=f"{character.name}: {reply}", memory_type="dialogue")
        user_item = MemoryItem(
            topic="对话",
            content=f"{snapshot.sender}: {snapshot.content}",
            memory_type="dialogue",
            relate_ids={reply_item.memory_id},
        )
        for item in (user_item, reply_item):
            await bus.publish(EventBus.new(Topic.MEMORY_WRITE, source="main", payload=item))

    await perceiver.close()
    report = await _health_report(sessions)
    log.info(f"会话结束, 上下文占用: {report}")

async def _health_report(sessions: SessionManager) -> dict:
    """ 退出时汇总各 owner 的上下文占用 """
    return {"responder": sessions.usage("responder"), "brain.think": sessions.usage("brain.think")}

if __name__ == "__main__":
    asyncio.run(run())
