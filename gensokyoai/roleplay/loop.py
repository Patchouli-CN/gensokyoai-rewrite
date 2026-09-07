""" 角色扮演主循环 """
import asyncio
import time

from ..eyes.perceiver import Perceiver
from ..core.brain.engine import BrainEngine, route
from ..core.brain.ooc_detector import OOCDetector
from ..core.responder.generator import Responder
from ..core.session_manager import SessionManager
from ..core.memorizer.manager import MemoryManager
from ..core.event_bus import EventBus
from ..utils.logger import LoggerManager
from ..schemas.memory_schema import MemoryItem
from ..schemas.model_schema import ModelConfig
from ..schemas.event_schema import BaseEvent
from ..schemas.event_schema import Topic
from .character import Character

class TouhouWorld:
    """ 
    扮演主循环 (Role-Play Loop)
    负责协调 Eyes, Brain, Responder 和 Memorizer 的完整生命周期。
    """
    
    def __init__(self,
        eye: Perceiver,
        character: Character,
        model_config: ModelConfig,
        provider_cls: type, # 从 Registry 获取的 Provider 类
    ) -> None:
        self.logger = LoggerManager.get_logger("TOUHOU WORLD")
        self.eye = eye
        self.character = character
        
        # 初始化核心组件
        self.bus = EventBus()
        self.sessions = SessionManager(provider_cls().config(model_config))
        self.memory = MemoryManager()
        
        # 初始化业务模块
        self.brain = BrainEngine(
            sessions=self.sessions, 
            persona=character.prompt, 
            ooc=OOCDetector(self.sessions)
        )
        self.responder = Responder(sessions=self.sessions, persona=character.prompt)
        
        # 注册记忆写入侧链
        self.bus.subscribe(Topic.MEMORY_WRITE, self._handle_memory_write)

    async def _handle_memory_write(self, event: BaseEvent) -> None:
        """ 异步处理记忆存储，不阻塞主链路 """
        if isinstance(event.payload, MemoryItem):
            self.memory.store(event.payload)

    async def start(self) -> None:
        """ 启动主循环 """
        self.logger.info(f"=== 幻想乡连接成功 | 角色: {self.character.name} ===")
        turn = 0
        
        while True:
            try:
                # 1. 感知阶段 (Eyes)
                snapshot = await self.eye.next_snapshot()
                if snapshot is None:
                    continue
                
                # 退出信号检查
                if snapshot.content.lower() in {"exit", "quit", "q"}:
                    break

                turn += 1
                t_start = time.monotonic()
                
                # 2. 决策阶段 (Brain)
                memories = self.memory.recent(5) # 获取热记忆
                effort = route(snapshot)         # 自动路由推理档位
                conclusion = await self.brain.think(snapshot, memories, effort)
                
                # 3. 表达阶段 (Responder)
                reply = await self.responder.respond(conclusion, snapshot, memories)
                
                # 4. 输出与记录
                print(f"\n{self.character.name}: {reply}\n")
                
                # 构造记忆项并投递到事件总线
                user_mem = MemoryItem(
                    topic="对话", content=f"{snapshot.sender}: {snapshot.content}", memory_type="dialogue"
                )
                char_mem = MemoryItem(
                    topic="对话", content=f"{self.character.name}: {reply}", memory_type="dialogue",
                    relate_ids={user_mem.memory_id} # 建立关联
                )
                
                for item in [user_mem, char_mem]:
                    await self.bus.publish(EventBus.new(Topic.MEMORY_WRITE, source="loop", payload=item))

                # 性能监控日志
                latency = time.monotonic() - t_start
                self.logger.info(f"回合 {turn} 完成 | 延迟: {latency:.2f}s | 档位: {effort.value}")

            except Exception as e:
                self.logger.exception(f"主循环异常: {e}")
                await asyncio.sleep(1) # 防止死循环报错
        
        await self.eye.close()
        self.logger.info("=== 幻想乡连接已断开 ===")