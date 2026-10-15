import asyncio
import contextlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

from bot import TwitchNotifierBot
from twitch_bot import TwitchChatBot
from utils import utcnow


def test_connected_channel_names_ignores_reconnect_placeholders():
    fake = SimpleNamespace(
        connected_channels=[None, SimpleNamespace(name="StreamerOne"), SimpleNamespace(name=None)]
    )

    assert TwitchChatBot._connected_channel_names(fake) == {"streamerone"}


def test_registered_channel_names_normalizes_and_deduplicates_rows():
    rows = [
        {"twitch_channel": "StreamerOne"},
        {"twitch_channel": "#streamerone"},
        {"twitch_channel": " streamerTwo "},
        {"twitch_channel": ""},
        {},
        None,
    ]

    assert TwitchChatBot._registered_channel_names(rows) == {
        "streamerone", "streamertwo",
    }


def test_chat_reconnect_backoff_is_progressive_and_capped():
    assert [
        TwitchNotifierBot._twitch_chat_backoff_seconds(attempt)
        for attempt in range(1, 8)
    ] == [5, 15, 30, 60, 120, 300, 300]


def test_chat_watchdog_detects_stale_irc_activity():
    now = utcnow()
    fake = SimpleNamespace(
        _twitch_chat_state="connected",
        _twitch_chat_last_activity_at=now - timedelta(minutes=11),
        _twitch_chat_stale_after_seconds=600,
    )

    assert TwitchNotifierBot._twitch_chat_is_stalled(fake, object(), now) is True
    fake._twitch_chat_last_activity_at = now - timedelta(minutes=2)
    assert TwitchNotifierBot._twitch_chat_is_stalled(fake, object(), now) is False


def test_owner_lifecycle_tracks_connection_without_clearing_last_recovery():
    fake = SimpleNamespace(
        _twitch_chat_state="starting",
        _twitch_chat_last_ready_at=None,
        _twitch_chat_last_activity_at=None,
        _twitch_chat_last_error="RuntimeError: old socket died",
        _twitch_chat_next_retry_at=utcnow(),
    )

    TwitchNotifierBot._on_twitch_chat_lifecycle(fake, "ready")
    assert fake._twitch_chat_state == "connected"
    assert fake._twitch_chat_last_ready_at is not None
    assert fake._twitch_chat_last_activity_at is not None
    assert fake._twitch_chat_last_error == "RuntimeError: old socket died"
    assert fake._twitch_chat_next_retry_at is None

    TwitchNotifierBot._on_twitch_chat_lifecycle(fake, "reconnect")
    assert fake._twitch_chat_state == "reconnecting"


def test_registered_owner_channels_are_normalized_and_deduplicated():
    fake = SimpleNamespace(db=SimpleNamespace(
        get_all_twitch_channels=lambda: [
            {"twitch_channel": "#Streamer"},
            {"twitch_channel": " streamer "},
            {"twitch_channel": "Another"},
            {"twitch_channel": ""},
        ]
    ))

    assert TwitchNotifierBot._registered_twitch_chat_channels(fake) == [
        "another", "streamer",
    ]


def test_chat_lifecycle_callback_reports_state_changes():
    events = []
    bot = object.__new__(TwitchChatBot)
    bot._lifecycle_callback = lambda event, detail=None: events.append((event, detail))

    TwitchChatBot._report_lifecycle(bot, "activity")
    TwitchChatBot._report_lifecycle(bot, "reconnect")
    TwitchChatBot._report_lifecycle(bot, "join_failure", "Unable to join @streamer")

    assert events == [
        ("activity", None),
        ("reconnect", None),
        ("join_failure", "Unable to join @streamer"),
    ]


@pytest.mark.asyncio
async def test_chat_supervisor_rebuilds_client_after_task_exit():
    owner = SimpleNamespace(
        _twitch_chat_shutdown=False,
        _twitch_chat_state="stopped",
        _twitch_chat_next_retry_at=None,
        _twitch_chat_task=None,
        _twitch_chat_last_ready_at=None,
        _twitch_chat_last_exit_at=None,
        _twitch_chat_last_error=None,
        _twitch_chat_restart_count=0,
        _twitch_bot_login="excelbot",
        twitch_chat_bot=None,
    )
    built = []
    closed = []

    class FakeChat:
        async def start(self):
            if len(built) == 1:
                raise RuntimeError("socket died")
            owner._twitch_chat_shutdown = True

    async def validate():
        return {"user_id": "1", "login": "excelbot", "scopes": set()}

    async def refresh():
        raise AssertionError("valid credentials must not be refreshed")

    def build():
        chat = FakeChat()
        built.append(chat)
        return chat

    async def watch(_chat, task):
        return await task

    async def close(chat):
        closed.append(chat)

    async def cancel(task):
        if task and not task.done():
            task.cancel()

    owner._validate_twitch_chat_identity = validate
    owner._refresh_twitch_chat_credentials = refresh
    owner._build_twitch_chat_client = build
    owner._watch_twitch_chat_task = watch
    owner._cancel_twitch_chat_task = cancel
    owner._close_twitch_chat_client = close
    owner._twitch_chat_backoff_seconds = lambda _failures: 0

    await TwitchNotifierBot._supervise_twitch_chat(owner)

    assert len(built) == 2
    assert closed == built
    assert owner._twitch_chat_restart_count == 1
    assert "socket died" in owner._twitch_chat_last_error
    assert owner._twitch_chat_state == "stopped"
    assert owner.twitch_chat_bot is None


@pytest.mark.asyncio
async def test_chat_watchdog_raises_for_alive_but_stale_task():
    async def never_finishes():
        await asyncio.Event().wait()

    task = asyncio.create_task(never_finishes())
    owner = SimpleNamespace(
        _twitch_chat_shutdown=False,
        _twitch_chat_watch_interval_seconds=0,
        _twitch_chat_stale_after_seconds=600,
        _twitch_chat_is_stalled=lambda _chat: True,
    )
    try:
        with pytest.raises(RuntimeError, match="no IRC activity"):
            await TwitchNotifierBot._watch_twitch_chat_task(owner, object(), task)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
