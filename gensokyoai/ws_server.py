"""ws 网关 —— 消费传输层事件流，应用层策略全在这里。

不识 aiohttp、不识回调：`validate` 管握手（频道/token/风格/房间上限），
`handle` 管事件（挂嘴/信封解析入房）。测试用假事件直接驱动。
"""

import hmac
import re

from .command import CommandContext, permission_of
from .hub import Hub
from .mouth import BroadcastMouth
from .network.base import (
    Connected,
    Disconnected,
    Json,
    Message,
    Pending,
    ServerEvent,
    Text,
)
from .style import ReplyStyle, resolve_reply_style
from .utils.logger import LoggerManager
from .utils.text import strip_control_chars

_logger = LoggerManager.get_logger("GATEWAY")

_CHANNEL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_TEXT_LEN = 2000
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class Gateway:
    """房间与传输层之间的策略层。"""

    def __init__(
        self,
        hub: Hub,
        *,
        token: str | None = None,
        executor=None,
        max_rooms: int | None = None,
    ) -> None:
        """初始化。

        Args:
            hub: 房间注册表（房间的 mouth 需可挂连接才能广播）
            token: 接入令牌（None = 不校验）
            executor: 命令执行器（None = 斜杠消息按普通聊天处理）
            max_rooms: 同时存在的房间上限（None = 不限）
        """
        self._hub = hub
        self._token = token
        self._executor = executor
        self._max_rooms = max_rooms
        self._styles: dict[str, ReplyStyle | None] = {}
        """ 握手时解析出的风格（按连接暂存，Connected 时应用） """

    async def validate(self, pending: Pending) -> tuple[int, str] | None:
        """握手策略：频道名 / token / 房间上限 / 风格名。

        Args:
            pending: 待定握手

        Returns:
            tuple[int, str] | None: (状态码, 原因) 拒绝；None 放行
        """
        if not _CHANNEL_ID.fullmatch(pending.path):
            return 400, "非法频道名"
        if self._token is not None and not hmac.compare_digest(
            pending.query.get("token", ""), self._token
        ):
            return 403, "token 无效"
        if (
            self._max_rooms is not None
            and not self._hub.has(pending.path)
            and len(self._hub.room_ids()) >= self._max_rooms
        ):
            return 503, "房间数已达上限"
        style_name = pending.query.get("style")
        if style_name and pending.remote in _LOOPBACK:
            try:
                self._styles[pending.path] = resolve_reply_style(style_name)
            except KeyError as err:
                return 400, str(err)
        return None

    async def handle(self, event: ServerEvent) -> None:
        """消费一个连接事件。"""
        if isinstance(event, Connected):
            await self._on_connected(event.conn)
        elif isinstance(event, Message):
            await self._on_message(event)
        elif isinstance(event, Disconnected):
            self._on_disconnected(event.conn)

    async def run(self, server) -> None:
        """常驻消费事件流（被取消即退出）。"""
        async for event in server:
            await self.handle(event)

    async def _on_connected(self, conn) -> None:
        loop = self._hub.get(conn.path)
        mouth = loop.mouth
        style = self._styles.pop(conn.path, None)
        if isinstance(mouth, BroadcastMouth):
            if style is not None:
                mouth.set_style(style)  # 首连生效
            mouth.attach(conn)
        else:
            _logger.warning(f"房间 {conn.path} 的 mouth 不支持挂连接")

    async def _on_message(self, event: Message) -> None:
        loop = self._hub.get(event.conn.path)
        trusted = event.conn.remote in _LOOPBACK
        user, direct, text, role = _parse_frame(
            event.frame,
            strip_control_chars(event.conn.query.get("user", "访客"))[:32] or "访客",
            event.conn.query.get("type") == "private"
            or event.conn.query.get("direct") == "1",
            (event.conn.query.get("role", "") if trusted else ""),
        )
        if not text:
            return
        # 斜杠指令：本地执行回系统帧，不进房、不调模型；未知/权限不足静默吞掉
        if self._executor is not None and text.startswith("/"):
            reply = await self._executor.execute(
                text,
                CommandContext(
                    issuer=user,
                    permission=permission_of(role if trusted else ""),
                    room=event.conn.path,
                ),
            )
            if reply is not None:
                await event.conn.send(
                    {"type": "message", "speaker": "系统", "text": reply}
                )
            return
        loop.notify("message", user, text, user_id=user, mentioned=direct)

    def _on_disconnected(self, conn) -> None:
        """摘掉连接（只看不建）。"""
        loop = self._hub.peek(conn.path)
        if loop is None:
            return
        mouth = loop.mouth
        if isinstance(mouth, BroadcastMouth):
            mouth.detach(conn)


def _parse_frame(
    frame, user: str, conn_direct: bool, conn_role: str
) -> tuple[str, bool, str, str]:
    """解析一帧：Json 信封（{"text","user","direct","role"} 逐条覆盖连接级默认）或
    Text 纯文本；Binary 没有聊天语义，丢弃。

    Returns:
        tuple[str, bool, str, str]: (发送者, 是否 direct, 清洗后的文本, 角色)
    """
    msg_user, msg_direct, msg_role, text = user, conn_direct, conn_role, ""
    if isinstance(frame, Json) and isinstance(frame.data, dict):
        envelope = frame.data
        if isinstance(envelope.get("text"), str):
            text = envelope["text"]
            if isinstance(envelope.get("user"), str) and envelope["user"].strip():
                msg_user = strip_control_chars(envelope["user"].strip())[:32] or user
            msg_direct = msg_direct or envelope.get("direct") is True
            if isinstance(envelope.get("role"), str):
                msg_role = envelope["role"]
    elif isinstance(frame, Text):
        text = frame.data
    text = strip_control_chars(text).strip()[:_MAX_TEXT_LEN]
    return msg_user, msg_direct, text, msg_role
