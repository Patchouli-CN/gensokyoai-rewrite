"""TouhouWorld 会话持久化端到端：跑两回合 -> 重启 -> 恢复续聊"""

import asyncio

from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.schemas.brain_schema import BrainThinkEffort
from gensokyoai.schemas.model_schema import CompletionResult
from gensokyoai.schemas.scene_schema import SceneSnapshot


class _ScriptedEye:
    """依次吐出预置输入，耗尽后请求停止"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._stop_requested = False
        self.greeted = False

    async def next_snapshot(self) -> SceneSnapshot | None:
        if not self._lines:
            self._stop_requested = True
            return None
        text = self._lines.pop(0)
        return SceneSnapshot(
            scene_type="private_chat",
            sender="灵梦",
            content=text,
            is_direct=True,
        )

    async def close(self) -> None:
        pass


class _EchoBackend:
    """贴合当前架构的假后端。

    - brain.think 的接力思考协议要求返回合法 JSON：识别到其 system 模板
      （"决策模块"）时吐一条 need_continue_think=false 的结论，让接力一轮收尾。
    - respond 的最终回复用单独的 reply 计数器：标签（回复1/回复2）只随
      「最终回复」调用递增，与大脑的接力调用解耦，不再被烧掉的轮次污染。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.reply_n = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        sys_text = next((m.content for m in messages if m.role == "system"), "")
        if "决策模块" in sys_text:
            # brain.think 接力协议：1 轮收尾的合法 JSON 结论
            return CompletionResult(
                content=(
                    '{"thought": "简单寒暄", "intent": "回应", "emotion": "平淡", '
                    '"action_hint": "回应", "confidence": 0.8, "need_continue_think": false}'
                )
            )
        self.reply_n += 1
        return CompletionResult(content=f"回复{self.reply_n}")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


def _make_world(storage_dir, lines: list[str]) -> TouhouWorld:
    sessions = SessionManager()
    sessions.set_default_backend(_EchoBackend())
    eye = _ScriptedEye(lines)
    character = Character(
        CharacterCard(name="幽幽子", system_prompt="白玉楼的主人", greeting="哟~")
    )
    return TouhouWorld(
        eye=eye,
        character=character,
        sessions=sessions,
        storage_dir=storage_dir,
        session_id="e2e",
        stall_probability=0.0,
        ooc_retry=False,
        ooc_audit=False,
        distill_every=1000,  # 关掉蒸馏避免干扰断言
    )


async def _run(world: TouhouWorld) -> None:
    task = asyncio.get_running_loop().create_task(world.start())
    await asyncio.wait_for(task, timeout=5.0)


async def test_world_restart_restores_session(tmp_path, capsys):
    """第一轮：两回合对话落盘；第二轮：不重放开场白、记忆接续、回合号续计"""
    # --- 第一次运行：两个回合 ---
    world1 = _make_world(tmp_path, ["第一个问题", "第二个问题"])
    await _run(world1)
    assert world1.restored is False, "首次启动无存档"
    out1 = capsys.readouterr().out
    assert "哟~" in out1, "全新会话应打开场白"
    assert "回复1" in out1 and "回复2" in out1

    # 合并窗口内的保存需要等事件循环跑完落盘任务
    await asyncio.sleep(0.3)
    data = await world1.persistence._backend.load(world1.persistence._key)
    assert data["turn_count"] == 2
    assert len(data["work_memory"]) >= 4, "两回合各存用户输入+角色回复"

    # --- 第二次运行：重启恢复 ---
    world2 = _make_world(tmp_path, ["恢复后的第三个问题"])
    await _run(world2)
    assert world2.restored is True, "应识别到存档"
    out2 = capsys.readouterr().out
    assert "哟~" not in out2.split("第三个问题")[0].split("回复2")[-1] or "会话已恢复" in out2
    assert world2.persistence.turn_count == 3, "回合号从存档续计"

    # 恢复的记忆能被检索到
    recent = await world2.memory.recent(10)
    contents = [m.content for m in recent]
    assert any("第一个问题" in c for c in contents), "上一轮的用户输入应在记忆里"


async def test_world_restart_restores_character_and_runtime_state(tmp_path):
    """重启后角色运行时状态与世界跨回合旋钮都接续（不只是记忆与回合号）

    这些状态会喂给语气 / 主动发言 / 干预档位，重启归零等于行为断层。
    第二个世界只调 `restore()` 不跑主循环 —— 避免「每回合 +1」这类正常递增
    干扰断言的算术（`_distill_counter` 每回合自增）。
    """
    world1 = _make_world(tmp_path, ["第一句"])
    # 摆好「运行期会产生的状态」，交给关闭时的 flush 落盘
    world1.character.status.update(motivation=0.8)
    world1.character.status.extra["ooc_audited"] = 2
    world1._effort_floor = BrainThinkEffort.HIGH
    world1._distill_counter = 7
    await _run(world1)  # 跑一回合 -> 蒸馏计数变 8，随之落盘

    data = await world1.persistence._backend.load(world1.persistence._key)
    assert data["schema_version"] == 2
    assert data["character_state"]["motivation"] == 0.8
    assert data["character_state"]["extra"]["ooc_audited"] == 2
    assert data["world_runtime"]["effort_floor"] == "high"
    assert data["world_runtime"]["distill_counter"] == 8, "7 + 本回合 1"

    # 重启：新世界直接恢复存档
    world2 = _make_world(tmp_path, [])
    assert await world2.persistence.restore() is True

    assert world2.character.status.motivation == 0.8, "对话欲应续上"
    assert world2.character.status.extra["ooc_audited"] == 2, "出戏计数应续上"
    assert world2._effort_floor is BrainThinkEffort.HIGH, "干预抬高的档位下限应续上"
    assert world2._distill_counter == 8, "蒸馏计数应续上（若归零会是 0）"
    # monotonic 时刻跨进程无意义：恢复时应重置为「现在」而非沿用旧值
    assert world2._stall_last_time != float("-inf")
