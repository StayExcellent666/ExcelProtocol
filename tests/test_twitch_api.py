import pytest

from twitch_api import TwitchAPI


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload or {"data": []}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


@pytest.mark.asyncio
async def test_stream_batch_failure_is_not_reported_as_everyone_offline(monkeypatch):
    api = TwitchAPI()
    monkeypatch.setattr(api, "get_session", lambda: _async_value(FakeSession(FakeResponse(500))))
    monkeypatch.setattr(api, "_headers", lambda: _async_value({}))

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await api.get_streams_by_logins(["alice", "bob"])


@pytest.mark.asyncio
async def test_stream_batch_success_returns_live_logins(monkeypatch):
    payload = {"data": [{"user_id": "1", "user_login": "Alice"}]}
    api = TwitchAPI()
    monkeypatch.setattr(api, "get_session", lambda: _async_value(FakeSession(FakeResponse(200, payload))))
    monkeypatch.setattr(api, "_headers", lambda: _async_value({}))
    monkeypatch.setattr(api, "_get_profile_images", lambda user_ids: _async_value({"1": "image"}))

    result = await api.get_streams_by_logins(["alice", "bob"])
    assert set(result) == {"alice"}
    assert result["alice"]["profile_image_url"] == "image"


async def _async_value(value):
    return value
