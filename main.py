""" 程序入口：加载配置、角色卡、初始化 SessionManager 并启动 """

import asyncio

from gensokyoai.core.config import load_config
from gensokyoai.core.bootstrap import discover_all
from gensokyoai.core.session_factory import build_session_manager
from gensokyoai.eyes.perceiver import ConsolePerceiver
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.roleplay.character import load_character
from gensokyoai.utils.logger import setup_logging, LoggerManager

async def main() -> None:
    # 1. 日志
    setup_logging("TRACE", True, "runtime.log")
    logger = LoggerManager.get_logger("MAIN")
    
    # 2. 扫描扩展
    discover_all()
    
    # 3. 加载配置
    config = load_config("config/settings.yaml")
    
    # 4. 装配 SessionManager（多模型路由在这里完成）
    sessions = build_session_manager(config)
    
    # 5. 加载角色卡
    character = load_character("config/roles/SaigyoujiYuyuko.yaml")

    # 6. 初始化感知器
    eye = ConsolePerceiver(sender="你")
    
    # 7. 启动世界
    world = TouhouWorld(
        eye=eye,
        character=character,
        sessions=sessions,  # 直接传入装配好的
    )
    
    logger.info("启动幻想乡...")
    await world.start()

if __name__ == "__main__":
    asyncio.run(main())