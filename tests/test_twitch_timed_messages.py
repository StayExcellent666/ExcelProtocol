from types import SimpleNamespace

from database import Database
from twitch_bot import TwitchChatBot


def run_immediate(coroutine):
    """Drive a coroutine whose test doubles complete without yielding."""
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("Coroutine unexpectedly yielded")


def test_timeline_database_lifecycle_and_loop(tmp_path):
    db = Database(str(tmp_path / "timeline.db"))
    saved = db.save_twitch_timed_message_timeline(
        "Streamer", 60, True, True, [
            {"slot_index": 0, "message": "Join the Discord!", "delivery_mode": "announcement"},
            {"slot_index": 2, "message": "Try !commands", "delivery_mode": "chat"},
        ],
    )

    assert saved["duration_minutes"] == 60
    assert saved["enabled"] is True
    assert saved["only_while_live"] is True
    assert [(slot["slot_index"], slot["offset_minutes"]) for slot in saved["slots"]] == [
        (0, 5), (2, 15),
    ]

    anchor = saved["anchor_tick"]
    first = db.get_twitch_timeline_messages_for_tick(anchor)
    assert len(first) == 1
    assert first[0]["message"] == "Join the Discord!"
    assert first[0]["delivery_mode"] == "announcement"
    assert db.get_twitch_timeline_messages_for_tick(anchor + 1) == []
    assert db.get_twitch_timeline_messages_for_tick(anchor + 2)[0]["message"] == "Try !commands"
    assert db.get_twitch_timeline_messages_for_tick(anchor + 12)[0]["message"] == "Join the Discord!"

    disabled = db.save_twitch_timed_message_timeline(
        "streamer", 60, False, False, saved["slots"],
    )
    assert disabled["enabled"] is False
    assert db.get_twitch_timeline_messages_for_tick(anchor + 12) == []


def test_channels_share_the_same_five_minute_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr("database.time.time", lambda: 3_000)
    db = Database(str(tmp_path / "shared-heartbeat.db"))
    for channel in ("alpha", "beta"):
        db.save_twitch_timed_message_timeline(
            channel, 60, True, False,
            [{"slot_index": 0, "message": channel, "delivery_mode": "chat"}],
        )

    alpha = db.get_twitch_timed_message_timeline("alpha")
    beta = db.get_twitch_timed_message_timeline("beta")
    assert alpha["anchor_tick"] == beta["anchor_tick"] == 11
    assert [item["twitch_channel"] for item in db.get_twitch_timeline_messages_for_tick(11)] == [
        "alpha", "beta",
    ]


def test_legacy_timers_migrate_into_consecutive_timeline_slots(tmp_path):
    path = tmp_path / "migration.db"
    db = Database(str(path))
    db.save_twitch_timed_message(
        "Streamer", "First message", 5, "chat", True, True,
    )
    db.save_twitch_timed_message(
        "Streamer", "Second message", 10, "announcement", True, True,
    )
    db.save_twitch_timed_message(
        "Streamer", "Disabled message", 15, "chat", False, True,
    )

    migrated = Database(str(path)).get_twitch_timed_message_timeline("streamer")
    assert [slot["message"] for slot in migrated["slots"]] == [
        "First message", "Second message",
    ]
    assert [slot["slot_index"] for slot in migrated["slots"]] == [0, 1]


def test_guild_cleanup_preserves_shared_timeline_until_last_link(tmp_path):
    db = Database(str(tmp_path / "cleanup.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO twitch_channels (guild_id, twitch_channel) VALUES (?, ?)",
        (101, "streamer"),
    )
    conn.execute(
        "INSERT INTO twitch_channels (guild_id, twitch_channel) VALUES (?, ?)",
        (202, "streamer"),
    )
    conn.commit()
    conn.close()
    db.save_twitch_timed_message_timeline(
        "streamer", 60, True, True,
        [{"slot_index": 0, "message": "Still shared", "delivery_mode": "chat"}],
    )

    db.cleanup_guild(101)
    assert len(db.get_twitch_timed_message_timeline("streamer")["slots"]) == 1

    db.cleanup_guild(202)
    assert db.get_twitch_timed_message_timeline("streamer")["slots"] == []


def test_timeline_announcement_falls_back_to_regular_chat():
    sent = []
    channel = SimpleNamespace(name="streamer")
    bot = object.__new__(TwitchChatBot)
    bot._channel_live_checker = lambda _channel: True
    bot._connected_channel_for = lambda _channel: channel

    async def announcement(*_args, **_kwargs):
        return False

    async def chat(_channel, message):
        sent.append(message)

    bot._send_announcement = announcement
    bot._send_chat = chat
    delivered = run_immediate(TwitchChatBot._deliver_timed_message(bot, {
        "twitch_channel": "streamer", "slot_index": 3, "message": "Community link",
        "delivery_mode": "announcement", "only_while_live": True,
    }))

    assert delivered is True
    assert sent == ["Community link"]


def test_live_only_timeline_slot_is_skipped_while_offline():
    channel = SimpleNamespace(name="streamer")
    bot = object.__new__(TwitchChatBot)
    bot._channel_live_checker = lambda _channel: False
    bot._connected_channel_for = lambda _channel: channel

    delivered = run_immediate(TwitchChatBot._deliver_timed_message(bot, {
        "twitch_channel": "streamer", "slot_index": 0, "message": "Community link",
        "delivery_mode": "chat", "only_while_live": True,
    }))

    assert delivered is False

