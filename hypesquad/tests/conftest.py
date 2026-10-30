import pytest

from excelprotocol_hypesquad import Database, HypeSquadService


@pytest.fixture
def service(tmp_path):
    return HypeSquadService(
        Database(str(tmp_path / "hypesquad.db")), chooser=lambda rows: rows[0]
    )


@pytest.fixture
def configured(service):
    service.configure_guild(
        "guild-a",
        enabled=True,
        announcement_channel_id="announce-a",
        leaderboard_channel_id="leader-a",
    )
    service.configure_guild(
        "guild-b",
        enabled=True,
        announcement_channel_id="announce-b",
        leaderboard_channel_id="leader-b",
    )
    service.link_member("target", "tw-target", "featured")
    service.join_member("target", "guild-a")
    service.link_member("helper-a", "tw-helper-a", "helper_a")
    service.join_member("helper-a", "guild-a")
    service.link_member("helper-b", "tw-helper-b", "helper_b")
    service.join_member("helper-b", "guild-b")
    return service

