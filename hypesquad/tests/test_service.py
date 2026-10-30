from datetime import datetime, timedelta, timezone

import pytest

from excelprotocol_hypesquad.service import Conflict, Forbidden
from excelprotocol_hypesquad.twitch import TwitchClient


UTC = timezone.utc


def test_server_must_opt_in_before_members_can_join(service):
    service.configure_guild("off", enabled=False)
    service.link_member("member", "tw-member", "member")

    with pytest.raises(Forbidden):
        service.join_member("member", "off")


def test_twitch_identity_can_only_belong_to_one_discord_member(service):
    service.configure_guild(
        "guild",
        enabled=True,
        announcement_channel_id="ann",
        leaderboard_channel_id="board",
    )
    service.link_member("one", "tw-1", "one")

    with pytest.raises(Conflict):
        service.link_member("two", "tw-1", "stolen")


def test_oauth_link_state_is_one_time(configured):
    state = configured.create_link_state("new-member", "guild-a")
    pending = configured.consume_link_state(state)

    assert pending["discord_user_id"] == "new-member"
    with pytest.raises(Forbidden):
        configured.consume_link_state(state)


def test_manual_hyperaid_join_and_spam_resistant_scoring(configured):
    start = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    event = configured.select_manual("target", "guild-a", now=start)
    assert event["status"] == "waiting_live"
    assert {row["guild_id"] for row in event["guild_targets"]} == {
        "guild-a", "guild-b"
    }

    configured.mark_live(event["id"], now=start)
    participant = configured.join_event(
        event["id"], "helper-a", "guild-a", now=start
    )
    assert participant["score"] == 10

    first = configured.record_activity(
        event["id"], "tw-helper-a", "message-1", start
    )
    second = configured.record_activity(
        event["id"], "tw-helper-a", "message-2", start + timedelta(seconds=10)
    )
    duplicate = configured.record_activity(
        event["id"], "tw-helper-a", "message-2", start + timedelta(seconds=20)
    )
    third = configured.record_activity(
        event["id"], "tw-helper-a", "message-3", start + timedelta(minutes=1)
    )

    assert first["score"] == 13
    assert second["score"] == 13
    assert duplicate["message_count"] == 2
    assert third["message_count"] == 3
    assert third["scored_message_count"] == 2
    assert third["active_minutes"] == 2
    assert third["score"] == 16


def test_only_joined_linked_members_score_and_featured_streamer_cannot_join(configured):
    event = configured.select_manual("target", "guild-a")
    configured.mark_live(event["id"])

    with pytest.raises(Conflict):
        configured.join_event(event["id"], "target", "guild-a")
    assert configured.record_activity(
        event["id"], "unknown-twitch-user", "message"
    ) is None


def test_finish_builds_per_server_leaderboards_and_discards_activity_receipts(configured):
    start = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    event = configured.select_manual("target", "guild-a", now=start)
    configured.mark_live(event["id"], now=start)
    configured.join_event(event["id"], "helper-a", "guild-a", now=start)
    configured.join_event(event["id"], "helper-b", "guild-b", now=start)
    configured.record_activity(event["id"], "tw-helper-a", "a-1", start)
    configured.record_activity(event["id"], "tw-helper-b", "b-1", start)

    finished = configured.finish_event(event["id"], now=start + timedelta(hours=2))
    outbox = configured.pending_outbox()
    completion = next(row for row in outbox if row["event_type"] == "hyperaid.finished")

    assert finished["status"] == "finished"
    boards = {
        row["guild_id"]: row["rows"]
        for row in completion["payload"]["guild_leaderboards"]
    }
    assert boards["guild-a"][0]["twitch_login"] == "helper_a"
    assert boards["guild-b"][0]["twitch_login"] == "helper_b"
    assert configured.db.fetchone(
        "SELECT COUNT(*) AS count FROM activity_receipts WHERE event_id=?",
        (event["id"],),
    )["count"] == 0


def test_weekly_selection_is_live_only_fair_and_slot_idempotent(configured):
    now = datetime(2026, 10, 31, 19, 0, tzinfo=UTC)
    configured.configure_schedule(
        enabled=True, weekday=5, hour=19, timezone_name="UTC", cooldown_days=42
    )
    slot = configured.due_selection_slot(now)
    assert slot

    event = configured.select_weekly(
        {"tw-helper-b"}, selection_slot=slot, now=now
    )
    assert event["target_twitch_user_id"] == "tw-helper-b"
    assert configured.due_selection_slot(now) is None

    with pytest.raises(Conflict):
        configured.select_weekly(
            {"tw-target", "tw-helper-a"}, selection_slot=slot, now=now
        )


def test_servers_can_opt_out_of_automatic_weekly_events(configured):
    configured.configure_guild(
        "guild-b",
        enabled=True,
        announcement_channel_id="announce-b",
        leaderboard_channel_id="leader-b",
        auto_weekly=False,
    )
    now = datetime(2026, 10, 31, 19, 0, tzinfo=UTC)
    event = configured.select_weekly(
        {"tw-target", "tw-helper-b"}, selection_slot="weekly-slot", now=now
    )

    assert event["target_twitch_user_id"] == "tw-target"
    assert {target["guild_id"] for target in event["guild_targets"]} == {"guild-a"}


def test_only_one_network_hyperaid_can_be_open_and_cancel_releases_it(configured):
    first = configured.select_manual("target", "guild-a")

    with pytest.raises(Conflict):
        configured.select_manual("helper-a", "guild-a")

    assert configured.cancel_event(first["id"])["status"] == "cancelled"
    second = configured.select_manual("helper-a", "guild-a")
    assert second["status"] == "waiting_live"


def test_two_offline_checks_finish_active_event(configured):
    event = configured.select_manual("target", "guild-a")
    configured.mark_live(event["id"])

    first = configured.record_live_status(event["id"], False)
    second = configured.record_live_status(event["id"], False)

    assert first["status"] == "active"
    assert second["status"] == "finished"


def test_unlink_anonymizes_history(configured):
    event = configured.select_manual("target", "guild-a")
    configured.mark_live(event["id"])
    configured.join_event(event["id"], "helper-a", "guild-a")

    configured.unlink_member("helper-a")
    participant = configured.db.fetchone(
        "SELECT * FROM participants WHERE event_id=?", (event["id"],)
    )

    assert participant["twitch_login"] == "deleted-member"
    assert participant["discord_user_id"].startswith("deleted:")


def test_twitch_link_uses_identity_only_oidc_scope():
    client = TwitchClient("client", "secret", "https://example.test/callback")
    url = client.authorization_url("state")

    assert "scope=openid" in url
    assert "email" not in url
    assert "chat" not in url

