"""GensokyoAI 入口 —— 组装一切 + 控制台调试主链路"""

import asyncio
from gensokyoai.core.bootstrap import discover_all, startup
from gensokyoai.core.config import load_config
from gensokyoai.core.registry import Registry
from gensokyoai.eyes.perceiver import ConsolePerceiver
from gensokyoai.roleplay.character import load_character
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.schemas.model_schema import ModelConfig

async def run(config_path: str = "config/settings.yaml", character_path: str | None = None) -> None:
    """ Phase 1 主链路：通过 TouhouWorld 编排所有组件。 """
    startup()
    
    # 1. 基础初始化
    config = load_config(config_path)
    discover_all()

    # 2. 加载人设与感知器
    character = load_character(character_path or r"config\roles\SaigyoujiYuyuko.yaml")
    perceiver = ConsolePerceiver()

    # 3. 准备模型配置
    provider_cls = Registry.get(config.model.provider)
    model_conf = ModelConfig(
        base_url=config.model.base_url,
        token=config.model.token,
        model_name=config.model.model_name,
        think=config.model.think,
        streaming=config.model.streaming,
        invoker_type=config.model.invoker_type,
        timeout=config.model.timeout,
    )

    # 4. 实例化世界并启动
    world = TouhouWorld(
        eye=perceiver,
        character=character,
        model_config=model_conf,
        provider_cls=provider_cls
    )
    
    await world.start()

if __name__ == "__main__":
    asyncio.run(run())