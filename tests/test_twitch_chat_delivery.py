from types import SimpleNamespace

import pytest

import twitch_bot as twitch_bot_module
from twitch_bot import TwitchChatBot


class FakeChannel:
    name = "streamer"

    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def make_chat_bot(api, channel):
    bot = object.__new__(TwitchChatBot)
    bot.bot_id = "bot-42"
    bot.twitch_api = api
    bot._user_access_token = "bot-user-token"
    bot._token_refresher = None
    bot._broadcaster_ids = {"streamer": "broadcaster-7"}
    bot._helix_send_failures = 0
    bot._helix_send_disabled_until = 0.0
    return bot


@pytest.mark.asyncio
async def test_badged_helix_send_is_preferred_over_irc():
    class Api:
        def __init__(self):
            self.calls = []

        async def send_chat_message(self, broadcaster_id, sender_id, message):
            self.calls.append((broadcaster_id, sender_id, message))
            return True, None

    channel = FakeChannel()
    api = Api()
    bot = make_chat_bot(api, channel)

    delivery = await TwitchChatBot.send_chat(bot, "Streamer", "hello", channel)

    assert delivery == "helix"
    assert api.calls == [("broadcaster-7", "bot-42", "hello")]
    assert channel.messages == []


@pytest.mark.asyncio
async def test_failed_helix_send_falls_back_and_opens_short_circuit():
    class Api:
        def __init__(self):
            self.calls = 0

        async def send_chat_message(self, *_args):
            self.calls += 1
            return False, "missing authorization"

    channel = FakeChannel()
    api = Api()
    bot = make_chat_bot(api, channel)

    for index in range(4):
        delivery = await TwitchChatBot.send_chat(
            bot, "streamer", f"message {index}", channel,
        )
        assert delivery == "irc"

    assert api.calls == 3
    assert channel.messages == [
        "message 0", "message 1", "message 2", "message 3",
    ]
    assert bot._helix_send_disabled_until > 0


@pytest.mark.asyncio
async def test_announcement_uses_bot_user_token_and_purple_banner():
    class Api:
        def __init__(self):
            self.calls = []

        async def send_chat_announcement(self, *args):
            self.calls.append(args)
            return True, None

    channel = FakeChannel()
    api = Api()
    bot = make_chat_bot(api, channel)

    sent = await TwitchChatBot._send_announcement(
        bot, channel, "Shoutout message", "purple",
    )

    assert sent is True
    assert api.calls == [(
        "broadcaster-7", "bot-42", "bot-user-token",
        "Shoutout message", "purple",
    )]


@pytest.mark.asyncio
async def test_announcement_refreshes_rejected_token_and_retries_once():
    class Api:
        def __init__(self):
            self.tokens = []

        async def send_chat_announcement(self, _broadcaster, _moderator,
                                         token, _message, _color):
            self.tokens.append(token)
            if len(self.tokens) == 1:
                return False, "HTTP 401: Invalid OAuth token"
            return True, None

    refreshed = []

    async def refresh_token():
        refreshed.append(True)
        return "fresh-user-token"

    channel = FakeChannel()
    api = Api()
    bot = make_chat_bot(api, channel)
    bot._token_refresher = refresh_token
    bot._connection = SimpleNamespace(_token="old-user-token")
    bot._http = SimpleNamespace(token="old-user-token")

    sent = await TwitchChatBot._send_announcement(
        bot, channel, "Shoutout message", "purple",
    )

    assert sent is True
    assert refreshed == [True]
    assert api.tokens == ["bot-user-token", "fresh-user-token"]
    assert bot._connection._token == "fresh-user-token"
    assert bot._http.token == "fresh-user-token"


@pytest.mark.asyncio
async def test_builtin_responses_use_bold_labels():
    sent = []

    class Db:
        @staticmethod
        def is_builtin_command_enabled(*_args):
            return True

    class Api:
        @staticmethod
        async def get_user(_channel):
            return {"id": "7"}

        @staticmethod
        async def get_channel_info(_user_id):
            return {"game_name": "Just Chatting", "title": "Community night"}

        @staticmethod
        async def get_stream_uptime(_channel):
            return "2h 41m"

        @staticmethod
        async def get_viewer_count(_channel):
            return 126

    class Bot:
        db = Db()
        twitch_api = Api()

        @staticmethod
        async def _check_cooldown(*_args):
            return True

        @staticmethod
        async def _send_chat(_channel, message):
            sent.append(message)

    message = SimpleNamespace(
        author=SimpleNamespace(is_mod=False, name="viewer"),
        channel=SimpleNamespace(name="streamer"),
    )
    bot = Bot()
    for command in ("!game", "!uptime", "!title", "!viewers"):
        assert await TwitchChatBot._handle_builtin(
            bot, message, command, "", "streamer",
        ) is True

    assert sent == [
        "🎮 𝗖𝗮𝘁𝗲𝗴𝗼𝗿𝘆 • Just Chatting",
        "⏱️ 𝗨𝗽𝘁𝗶𝗺𝗲 • 2h 41m",
        "📺 𝗧𝗶𝘁𝗹𝗲 • Community night",
        "👀 𝗩𝗶𝗲𝘄𝗲𝗿𝘀 • 126",
    ]


@pytest.mark.asyncio
async def test_shoutout_is_compact_and_promotional():
    announcements = []
    sent = []

    class Api:
        @staticmethod
        async def get_last_stream_info(_target):
            return {
                "user": {"display_name": "TTVTriggaHappy"},
                "game_name": "Fortnite",
                "last_streamed_at": "2026-10-13T18:00:00Z",
            }

    class Bot:
        twitch_api = Api()

        @staticmethod
        async def _send_announcement(_channel, message, color="purple"):
            announcements.append((message, color))
            return True

        @staticmethod
        async def _send_chat(_channel, message):
            sent.append(message)

    await TwitchChatBot._do_shoutout(
        Bot(), SimpleNamespace(name="streamer"), "ttvtriggahappy",
    )

    expected = (
        "📣 𝗦𝗛𝗢𝗨𝗧𝗢𝗨𝗧 • Go show @TTVTriggaHappy some love! "
        "🎮 Fortnite • Last live Oct 13 • twitch.tv/ttvtriggahappy"
    )
    assert announcements == [(expected, "purple")]
    assert sent == []


@pytest.mark.asyncio
async def test_shoutout_falls_back_to_normal_chat_when_announcement_fails():
    sent = []

    class Api:
        @staticmethod
        async def get_last_stream_info(_target):
            return {
                "user": {"display_name": "Creator"},
                "game_name": "Just Chatting",
                "last_streamed_at": None,
            }

    class Bot:
        twitch_api = Api()

        @staticmethod
        async def _send_announcement(*_args, **_kwargs):
            return False

        @staticmethod
        async def _send_chat(_channel, message):
            sent.append(message)

    await TwitchChatBot._do_shoutout(
        Bot(), SimpleNamespace(name="streamer"), "creator",
    )

    assert sent == [
        "📣 𝗦𝗛𝗢𝗨𝗧𝗢𝗨𝗧 • Go show @Creator some love! "
        "🎮 Just Chatting • twitch.tv/creator"
    ]


def test_mass_shoutout_target_parser_accepts_mentions_commas_and_deduplicates():
    assert TwitchChatBot._parse_mass_shoutout_targets(
        "@Alpha, beta @ALPHA invalid-name @third_user"
    ) == ["alpha", "beta", "third_user"]


@pytest.mark.asyncio
async def test_mass_shoutout_named_targets_are_queued_and_paced(monkeypatch):
    monkeypatch.setattr(twitch_bot_module, "MASS_SHOUTOUT_DELAY_SECONDS", 0)
    channel = FakeChannel()
    processed = []

    bot = object.__new__(TwitchChatBot)
    bot._mass_shoutout_tasks = {}

    async def send_chat(_channel, text):
        channel.messages.append(text)

    async def shoutout(_channel, target, report_errors=True):
        processed.append((target, report_errors))
        return target != "missing"

    bot._send_chat = send_chat
    bot._do_shoutout = shoutout

    await TwitchChatBot._start_mass_shoutout(
        bot, channel, "@Alpha, @missing beta", "streamer",
    )
    await bot._mass_shoutout_tasks["streamer"]

    assert processed == [
        ("alpha", False), ("missing", False), ("beta", False),
    ]
    assert channel.messages == [
        "📣 𝗠𝗔𝗦𝗦 𝗦𝗛𝗢𝗨𝗧𝗢𝗨𝗧 • Queued 3 creator(s)",
        "✅ 𝗠𝗔𝗦𝗦 𝗦𝗛𝗢𝗨𝗧𝗢𝗨𝗧 • 2 sent • 1 skipped",
    ]
    assert bot._mass_shoutout_tasks == {}


@pytest.mark.asyncio
async def test_mass_shoutout_all_excludes_broadcaster_bot_and_common_bots():
    class Api:
        @staticmethod
        async def get_chatters(*_args, **_kwargs):
            return [
                {"user_login": "streamer"},
                {"user_id": "bot-42", "user_login": "exceltwitch"},
                {"user_login": "nightbot"},
                {"user_login": "CreatorOne"},
                {"user_login": "creatorone"},
                {"user_login": "CreatorTwo"},
            ]

    bot = object.__new__(TwitchChatBot)
    bot.bot_id = "bot-42"
    bot._http = SimpleNamespace(nick="exceltwitch")
    bot._connection = SimpleNamespace(nick="exceltwitch")
    bot.twitch_api = Api()
    bot._user_access_token = "token"
    bot._token_refresher = None
    bot._broadcaster_ids = {"streamer": "broadcaster-7"}

    assert await TwitchChatBot._get_mass_shoutout_chatters(
        bot, "streamer",
    ) == ["creatorone", "creatortwo"]

