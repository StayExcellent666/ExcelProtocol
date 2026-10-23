import asyncio
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


def test_timed_message_database_lifecycle(tmp_path):
    db = Database(str(tmp_path / "timers.db"))
    timer_id = db.save_twitch_timed_message(
        "Streamer", "Join the Discord!", 5, "announcement", True, True,
    )

    assert db.get_twitch_timed_messages("streamer") == [{
        "id": timer_id,
        "message": "Join the Discord!",
        "interval_minutes": 5,
        "delivery_mode": "announcement",
        "enabled": True,
        "only_while_live": True,
        "last_sent_at": db.get_twitch_timed_messages("streamer")[0]["last_sent_at"],
    }]

    conn = db.get_connection()
    conn.execute(
        "UPDATE twitch_timed_messages SET last_sent_at = datetime('now', '-6 minutes') WHERE id = ?",
        (timer_id,),
    )
    conn.commit()
    conn.close()
    assert [item["id"] for item in db.get_due_twitch_timed_messages()] == [timer_id]

    db.mark_twitch_timed_message_sent(timer_id)
    assert db.get_due_twitch_timed_messages() == []

    db.save_twitch_timed_message(
        "streamer", "Updated", 30, "chat", False, False, timer_id,
    )
    updated = db.get_twitch_timed_messages("streamer")[0]
    assert updated["message"] == "Updated"
    assert updated["enabled"] is False
    assert updated["only_while_live"] is False
    assert db.remove_twitch_timed_message("streamer", timer_id) is True
    assert db.get_twitch_timed_messages("streamer") == []


def test_guild_cleanup_preserves_shared_channel_timers_until_last_link(tmp_path):
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
    db.save_twitch_timed_message(
        "streamer", "Still shared", 15, "chat", True, True,
    )

    db.cleanup_guild(101)
    assert len(db.get_twitch_timed_messages("streamer")) == 1

    db.cleanup_guild(202)
    assert db.get_twitch_timed_messages("streamer") == []


def test_timed_announcement_falls_back_to_regular_chat(monkeypatch):
    async def immediate_to_thread(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    marked = []
    sent = []
    channel = SimpleNamespace(name="streamer")
    bot = object.__new__(TwitchChatBot)
    bot.db = SimpleNamespace(mark_twitch_timed_message_sent=marked.append)
    bot._channel_live_checker = lambda _channel: True
    bot._connected_channel_for = lambda _channel: channel

    async def announcement(*_args, **_kwargs):
        return False

    async def chat(_channel, message):
        sent.append(message)

    bot._send_announcement = announcement
    bot._send_chat = chat
    delivered = run_immediate(TwitchChatBot._deliver_timed_message(bot, {
        "id": 7, "twitch_channel": "streamer", "message": "Community link",
        "delivery_mode": "announcement", "only_while_live": True,
    }))

    assert delivered is True
    assert sent == ["Community link"]
    assert marked == [7]


def test_live_only_timer_waits_until_stream_is_live(monkeypatch):
    async def immediate_to_thread(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    channel = SimpleNamespace(name="streamer")
    bot = object.__new__(TwitchChatBot)
    bot.db = SimpleNamespace(mark_twitch_timed_message_sent=lambda _timer_id: None)
    bot._channel_live_checker = lambda _channel: False
    bot._connected_channel_for = lambda _channel: channel

    delivered = run_immediate(TwitchChatBot._deliver_timed_message(bot, {
        "id": 8, "twitch_channel": "streamer", "message": "Community link",
        "delivery_mode": "chat", "only_while_live": True,
    }))

    assert delivered is False

