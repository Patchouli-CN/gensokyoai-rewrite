"""TouhouWorld 会话持久化端到端：跑两回合 -> 重启 -> 恢复续聊"""

import asyncio

from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.loop import TouhouWorld
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
