"""房间测试 —— 事件编号 / 未读投递 / gap 折叠与展开 / 回退 / 落盘回放。"""

from gensokyoai.room import Event, Room


def test_append_assigns_ids_and_marks_unread():
    """message 才进未读；空文本不入房也没有编号"""
    room = Room("g1")
    first = room.append("message", "甲", "你好", user_id="u1")
    blank = room.append("message", "乙", "   ")
    reply = room.append("reply", "觉", "在")

    assert (first.msg_id, reply.msg_id) == (1, 2)
    assert blank.msg_id == 0 and blank.text == ""
    assert [event.text for event in room.events] == ["你好", "在"]
    assert room.unread == 1


def test_take_feed_folds_older_unread_into_gap():
    """超出窗口的未读折成 gap，报出条数与是否有人提到"""
    room = Room("g1")
    for i in range(10):
        room.append("message", "甲", f"第{i}条", mentioned=(i == 2))

    shown, gap = room.take_feed(window=4)

    assert [event.text for event in shown] == ["第6条", "第7条", "第8条", "第9条"]
    assert gap == {"gapId": 0, "skipped": 6, "mentioned": True}
    assert room.unread == 0


def test_take_feed_window_zero_shows_everything():
    """window=0 = 不折叠"""
    room = Room("g1")
    for i in range(5):
        room.append("message", "甲", f"第{i}条")

    shown, gap = room.take_feed(window=0)

    assert len(shown) == 5 and gap is None


def test_reveal_returns_rest_as_new_gap():
    """揭一段 gap：剩下的挂成新 gap 一并返回，旧 id 作废"""
    room = Room("g1")
    for i in range(10):
        room.append("message", "甲", f"第{i}条")
    _, gap = room.take_feed(window=2)

    taken, rest_gap = room.reveal(gap["gapId"], side="earliest", limit=3)

    assert [event.text for event in taken] == ["第0条", "第1条", "第2条"]
    assert rest_gap is not None and rest_gap["skipped"] == 5
    assert room.reveal(gap["gapId"]) == ([], None), "同一条 gap 不能开两次"

    rest, _ = room.reveal(rest_gap["gapId"])
    assert [event.text for event in rest] == [
        "第3条",
        "第4条",
        "第5条",
        "第6条",
        "第7条",
    ]


def test_push_back_returns_events_to_unread():
    """回合失败时把没消费的消息退回未读"""
    room = Room("g1")
    room.append("message", "甲", "第一条")
    shown, _ = room.take_feed()
    assert room.unread == 0

    room.push_back(shown)

    assert room.unread == 1
    again, _ = room.take_feed()
    assert [event.text for event in again] == ["第一条"]


def test_feed_wire_shape():
    """给模型的事件带 msgId / userId / mentioned / time"""
    room = Room("g1")
    room.append("message", "甲", "在吗", user_id="u9", mentioned=True)
    shown, _ = room.take_feed()

    wire = shown[0].wire()

    assert wire["msgId"] == 1 and wire["name"] == "甲"
    assert wire["userId"] == "u9" and wire["mentioned"] is True
    assert len(wire["time"]) == 5


def test_room_events_bounded():
    """事件流有上界：超了丢最老的"""
    room = Room("g1", max_events=3)
    for i in range(6):
        room.append("message", "甲", f"第{i}条")

    assert [event.text for event in room.events] == ["第3条", "第4条", "第5条"]


async def test_reload_continues_msg_ids(tmp_path):
    """重启后编号接着走，不会和落盘的历史撞"""
    from gensokyoai.bgtasks import BgTasks
    from gensokyoai.room import RoomLog

    tasks = BgTasks()
    path = tmp_path / "events.jsonl"
    room = Room("g1", log=RoomLog(path, tasks))
    room.append("message", "甲", "第一条")
    room.append("reply", "觉", "第二条")
    await tasks.idle()

    reloaded = Room("g1", log=RoomLog(path, tasks))

    assert [event.text for event in reloaded.events] == ["第一条", "第二条"]
    assert isinstance(reloaded.events[0], Event)
    assert reloaded.append("message", "甲", "第三条").msg_id == 3
