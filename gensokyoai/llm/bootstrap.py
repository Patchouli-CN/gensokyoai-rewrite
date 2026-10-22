"""统一模型来源：config/llm 优先，没有才退回 settings.yaml。

CLI 与 nb2 插件都走这里，避免两套相同的模型配置。
"""

import asyncio
from pathlib import Path

from ..character import load_card
from ..mind import Mind
from .manager import LLMManager

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_LLM = _REPO_ROOT / "config" / "llm"


def llm_root(explicit: str | Path | None = None) -> Path | None:
    """找 config/llm：显式给的最优先，其次仓库根的。

    Args:
        explicit: 调用方指定的目录

    Returns:
        Path | None: 可用目录；都没有就是 None
    """
    if explicit:
        path = Path(explicit)
        return path if path.is_dir() else None
    return CONFIG_LLM if CONFIG_LLM.is_dir() else None


async def build_mind(
    *,
    card_path: str,
    settings=None,
    root: str | Path | None = None,
    health=None,
    usage_path: Path | None = None,
    log=None,
):
    """按配置造心智：有 config/llm 就走管理器，否则用 settings 的老路径。

    Args:
        card_path: 角色卡路径
        settings: 老配置（只有退回老路径时才用）
        root: config/llm 目录（None = 自动找）
        health: 健康中心
        usage_path: 用量流水落点
        log: 日志器

    Returns:
        tuple[Mind, LLMManager | None]: 心智与管理器（老路径时为 None）
    """
    card = load_card(card_path)
    base = llm_root(root)
    if base is not None:
        manager = LLMManager(base, health=health, usage_path=usage_path, log=log)
        mind = Mind(await manager.get("chat"), card.name, card.persona, manager=manager)
        try:
            mind.use_cheap(await manager.get("cheap"))
        except LookupError:
            mind.use_cheap(None)
        if log is not None:
            log.info(f"用 config/llm：{base}")
            log.info(f"chat → {mind.model.name}，cheap → {mind.cheap_model.name}")
        return mind, manager
    if settings is None:
        raise SystemExit("没有 config/llm，也没有 settings.yaml 可用")
    mind = Mind(
        settings.build_model(health=health, usage_path=usage_path),
        card.name,
        card.persona,
        vision=settings.build_vision_model(health=health, usage_path=usage_path),
    )
    return mind, None


async def close_mind(mind: Mind, manager: LLMManager | None) -> None:
    """收尾：管理器关所有端点，老路径关主模型。"""
    if manager is not None:
        await manager.aclose()
        return
    await mind.close()


def build_mind_sync(**kwargs):
    """同步外壳（给不能 await 的地方用）。"""
    return asyncio.run(build_mind(**kwargs))
