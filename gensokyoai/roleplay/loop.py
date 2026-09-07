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
from ..core.lifecycle import LifecycleManager
from ..core.registry import ToolRegistry
from ..core.health import HealthMonitor
from ..utils.logger import LoggerManager
from ..schemas.memory_schema import MemoryItem
from ..schemas.model_schema import ToolSpec
from ..schemas.event_schema import BaseEvent, EventTopic
from .character import Character

class TouhouWorld:
    """ 
    扮演主循环 (Role-Play Loop)
    负责协调 Eyes, Brain, Responder 和 Memorizer 的完整生命周期。
    支持生命周期管理（启动/关闭回调）。
    """
    
    def __init__(
        self,
        eye: Perceiver,
        character: Character,
        sessions: SessionManager,
        external_tools: list[ToolSpec] | None = None,
    ) -> None:
        self.logger = LoggerManager.get_logger("TOUHOU WORLD")
        self.eye = eye
        self.character = character
        
        # 初始化核心组件
        self.bus = EventBus()
        self.lifecycle = LifecycleManager(self.bus)
        self.sessions = sessions
        self.memory = MemoryManager()
        self.health = HealthMonitor(bus=self.bus)
        
        # --- 组装工具：注册表（内置） + 外部传入 ---
        all_tools = self._setup_tools(external_tools)
        
        # 初始化业务模块
        self.brain = BrainEngine(
            sessions=self.sessions, 
            persona=character.prompt, 
            ooc=OOCDetector(self.sessions),
            tools=all_tools
        )
        
        self.responder = Responder(sessions=self.sessions, persona=character.prompt)
        
        # 注册记忆写入侧链
        self.bus.subscribe(EventTopic.MEMORY_WRITE, self._handle_memory_write)
        
        # 注册默认生命周期回调
        self._register_default_lifecycle()
    
    def _register_default_lifecycle(self) -> None:
        """ 注册默认的生命周期回调 """
        
        @self.lifecycle.on_startup
        async def _on_startup():
            self.logger.info(f"=== 幻想乡连接成功 | 角色: {self.character.name} ===")
        
        @self.lifecycle.on_shutdown
        async def _on_shutdown():
            self.logger.info("=== 幻想乡连接已断开 ===")
            await self.eye.close()
            self.sessions.reset_all()
    
    def _setup_tools(self, external_tools: list[ToolSpec] | None = None) -> list[ToolSpec]:
        """ 组装工具列表 """
        tools = []
        
        try:
            registered_tools = ToolRegistry.all()
            if registered_tools:
                tools.extend(registered_tools)
                self.logger.info(f"加载全局注册工具: {[t.tool_func.__name__ for t in registered_tools]}")
        except Exception as e:
            self.logger.warning(f"加载全局注册工具失败: {e}")
        
        if external_tools:
            for tool in external_tools:
                tools.append(tool)
                try:
                    ToolRegistry.register_external(tool)
                except ValueError:
                    self.logger.debug(f"工具已存在: {tool.tool_func.__name__}")
            self.logger.info(f"加载外部工具: {[t.tool_func.__name__ for t in external_tools]}")
        
        seen = set()
        unique_tools = []
        for tool in tools:
            name = tool.tool_func.__name__
            if name not in seen:
                unique_tools.append(tool)
                seen.add(name)
        
        self.logger.info(f"最终工具列表: {[t.tool_func.__name__ for t in unique_tools]}")
        return unique_tools

    async def _handle_memory_write(self, event: BaseEvent) -> None:
        """ 异步处理记忆存储，不阻塞主链路 """
        if isinstance(event.payload, MemoryItem):
            await self.memory.store(event.payload)

    async def start(self) -> None:
        """ 启动主循环（优雅关闭版本）"""
        await self.lifecycle.startup()
        turn = 0
        
        try:
            while True:
                # 检查是否收到停止请求
                if getattr(self.eye, '_stop_requested', False):
                    self.logger.info("感知器已停止，主循环退出")
                    break
                
                # 1. 感知阶段 (Eyes)
                snapshot = await self.eye.next_snapshot()
                if snapshot is None:
                    # 如果是因为停止请求返回 None，直接退出
                    if getattr(self.eye, '_stop_requested', False):
                        break
                    continue
                
                # 退出信号检查
                if snapshot.content.lower() in {"exit", "quit", "q"}:
                    break
                
                turn += 1
                t_start = time.monotonic()
                
                # 2. 决策阶段 (Brain)
                memories = await self.memory.recent(5)
                effort = route(snapshot)
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
                    relate_ids={user_mem.memory_id}
                )
                
                for item in [user_mem, char_mem]:
                    await self.bus.publish(EventBus.new(EventTopic.MEMORY_WRITE, source="loop", payload=item))
                
                # 性能监控日志
                latency = time.monotonic() - t_start
                self.logger.info(f"回合 {turn} 完成 | 延迟: {latency:.2f}s | 档位: {effort.value}")
        
        except asyncio.CancelledError:
            self.logger.info("主循环被取消，开始优雅关闭...")
            await self.lifecycle.shutdown()
            # 不 raise，直接返回，让 asyncio.run 正常结束
        except KeyboardInterrupt:
            self.logger.info("收到 Ctrl+C，开始优雅关闭...")
            await self.lifecycle.shutdown()
        finally:
            # 确保无论什么情况都执行关闭
            if not self.lifecycle._stopped:
                await self.lifecycle.shutdown()
