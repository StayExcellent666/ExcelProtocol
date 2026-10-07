from types import SimpleNamespace

from twitch_bot import TwitchChatBot


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
