"""Tests for server_setup template builder.

Pure data/logic — no Discord API mocking needed. The orchestrator in bot.py
that actually talks to Discord isn't unit-testable without a heavy mock layer,
so it relies on manual verification in production. These tests cover the
template-data correctness which is the foundation everything else builds on.
"""
import server_setup


class TestResolveRoleNames:
    def test_returns_defaults_when_no_overrides(self):
        result = server_setup.resolve_role_names({})
        assert result["member"] == "MEMBER"
        assert result["vip"] == "VIP"
        assert result["moderator"] == "MODERATOR"
        assert result["admin"] == "ADMIN"
        assert result["bot"] == "BOT"

    def test_returns_defaults_for_none(self):
        # Caller may pass None instead of {}
        result = server_setup.resolve_role_names(None)
        assert result["member"] == "MEMBER"

    def test_applies_overrides(self):
        result = server_setup.resolve_role_names({"member": "Citizen", "vip": "Diamond"})
        assert result["member"] == "Citizen"
        assert result["vip"] == "Diamond"
        assert result["admin"] == "ADMIN"  # untouched

    def test_strips_whitespace(self):
        result = server_setup.resolve_role_names({"member": "  Citizen  "})
        assert result["member"] == "Citizen"

    def test_blank_string_falls_back_to_default(self):
        # The UI may submit empty strings for skipped fields
        result = server_setup.resolve_role_names({"member": "", "vip": "   "})
        assert result["member"] == "MEMBER"
        assert result["vip"] == "VIP"


class TestBuildPlan:
    def test_simplistic_default(self):
        plan = server_setup.build_plan({"template": "simplistic"})
        assert plan["template_id"] == "simplistic"
        assert plan["template_label"] == "Simplistic"
        cat_names = [c["name"] for c in plan["categories"]]
        assert "INFO" in cat_names
        assert "GENERAL" in cat_names
        assert "VOICE" in cat_names

    def test_aesthetic_uses_emoji_channels(self):
        plan = server_setup.build_plan({"template": "aesthetic"})
        # Find the welcome channel — should have emoji prefix
        welcome = None
        for cat in plan["categories"]:
            for ch_name, ch_type in cat["channels"]:
                if "welcome" in ch_name.lower():
                    welcome = ch_name
                    break
        assert welcome is not None
        # Aesthetic prefixes with emoji like 🌸-welcome
        assert "-welcome" in welcome
        # Has a non-ascii character (emoji)
        assert any(ord(c) > 127 for c in welcome)

    def test_cluttered_has_most_channels(self):
        s = server_setup.build_plan({"template": "simplistic"})
        a = server_setup.build_plan({"template": "aesthetic"})
        c = server_setup.build_plan({"template": "cluttered"})
        s_count = sum(len(cat["channels"]) for cat in s["categories"])
        a_count = sum(len(cat["channels"]) for cat in a["categories"])
        c_count = sum(len(cat["channels"]) for cat in c["categories"])
        assert c_count > a_count > s_count

    def test_invalid_template_falls_back_to_simplistic(self):
        plan = server_setup.build_plan({"template": "asdf"})
        assert plan["template_id"] == "simplistic"

    def test_vip_module_adds_category(self):
        # Without VIP
        plan1 = server_setup.build_plan({"template": "simplistic", "enable_vip": False})
        cat_names1 = [c["name"] for c in plan1["categories"]]
        assert not any("VIP" in n for n in cat_names1)

        # With VIP
        plan2 = server_setup.build_plan({"template": "simplistic", "enable_vip": True})
        cat_names2 = [c["name"] for c in plan2["categories"]]
        assert any("VIP" in n for n in cat_names2)

    def test_vip_aesthetic_uses_emoji(self):
        plan = server_setup.build_plan({"template": "aesthetic", "enable_vip": True})
        vip_cat = next(c for c in plan["categories"] if "VIP" in c["name"])
        # Aesthetic VIP has diamond emoji
        assert "💎" in vip_cat["name"]
        ch_names = [n for n, _ in vip_cat["channels"]]
        assert any("💎" in n for n in ch_names)

    def test_vip_non_aesthetic_no_emoji(self):
        plan = server_setup.build_plan({"template": "simplistic", "enable_vip": True})
        vip_cat = next(c for c in plan["categories"] if "VIP" in c["name"])
        # Simplistic VIP is plain "VIP"
        assert vip_cat["name"] == "VIP"

    def test_toggles_propagate(self):
        plan = server_setup.build_plan({
            "template": "simplistic",
            "enable_verification": True,
            "enable_vip": True,
            "auto_post_rules": True,
        })
        assert plan["verification_enabled"] is True
        assert plan["vip_enabled"] is True
        assert plan["auto_post_rules"] is True

    def test_role_overrides_propagate(self):
        plan = server_setup.build_plan({
            "template": "simplistic",
            "role_names": {"member": "Citizen"},
        })
        assert plan["role_names"]["member"] == "Citizen"


class TestCountPlanItems:
    def test_simplistic_counts(self):
        plan = server_setup.build_plan({"template": "simplistic"})
        counts = server_setup.count_plan_items(plan)
        assert counts["roles"] == 5  # always: admin, mod, bot, vip, member
        assert counts["categories"] == 3  # INFO, GENERAL, VOICE
        assert counts["text_channels"] == 4  # welcome, rules, general, live-notifications
        assert counts["voice_channels"] == 1  # Lounge
        assert counts["total_channels"] == 5

    def test_vip_adds_to_counts(self):
        plain = server_setup.count_plan_items(server_setup.build_plan({"template": "simplistic"}))
        with_vip = server_setup.count_plan_items(server_setup.build_plan({
            "template": "simplistic", "enable_vip": True,
        }))
        assert with_vip["categories"] == plain["categories"] + 1
        assert with_vip["text_channels"] == plain["text_channels"] + 1  # vip-chat
        assert with_vip["voice_channels"] == plain["voice_channels"] + 1  # VIP VC


class TestRolePermissions:
    """The wizard creates roles with sensible default permissions per role."""

    def test_admin_has_full_management_perms_but_not_administrator(self):
        """Admin gets every non-admin management perm explicitly. The wizard
        avoids the `administrator` flag because the bot itself doesn't have
        it; users grant Administrator manually via Discord settings after
        the wizard runs (the post-run note prompts them to do so)."""
        flags = server_setup.ROLE_PERMISSIONS["admin"]
        assert "administrator" not in flags, \
            "Wizard must not grant `administrator` (bot lacks it)"
        # Every meaningful management perm
        for needed in ["manage_channels", "manage_roles", "manage_guild",
                       "kick_members", "ban_members", "view_audit_log"]:
            assert needed in flags, f"admin should have {needed}"

    def test_moderator_has_kick_ban_manage_messages(self):
        flags = server_setup.ROLE_PERMISSIONS["moderator"]
        assert "kick_members" in flags
        assert "ban_members" in flags
        assert "manage_messages" in flags
        # Mod should NOT have administrator
        assert "administrator" not in flags

    def test_member_has_view_channels_and_send_messages(self):
        flags = server_setup.ROLE_PERMISSIONS["member"]
        assert "view_channel" in flags
        assert "send_messages" in flags
        # Member should NOT have kick/ban/admin
        assert "kick_members" not in flags
        assert "ban_members" not in flags
        assert "administrator" not in flags

    def test_vip_has_extras_over_member(self):
        member = set(server_setup.ROLE_PERMISSIONS["member"])
        vip = set(server_setup.ROLE_PERMISSIONS["vip"])
        # VIP is a superset (or equal) of member basics
        for basic in ["view_channel", "send_messages", "read_message_history"]:
            assert basic in vip
        # VIP has at least one extra
        assert vip != member

    def test_bot_has_manage_roles_for_bot_management(self):
        flags = server_setup.ROLE_PERMISSIONS["bot"]
        # Community bots commonly need these
        assert "manage_roles" in flags
        assert "manage_channels" in flags
        # But not admin (defense in depth — most bots don't need it)
        assert "administrator" not in flags

    def test_build_permissions_returns_discord_permissions(self):
        import discord
        perms = server_setup.build_permissions("moderator")
        assert isinstance(perms, discord.Permissions)
        assert perms.kick_members is True
        assert perms.ban_members is True
        assert perms.administrator is False

    def test_build_permissions_admin_does_not_set_administrator(self):
        """Admin role uses enumerated perms, never `administrator`."""
        perms = server_setup.build_permissions("admin")
        assert perms.administrator is False
        # But should have the management bits
        assert perms.manage_channels is True
        assert perms.manage_roles is True
        assert perms.kick_members is True
        assert perms.ban_members is True

    def test_build_permissions_unknown_key_returns_empty(self):
        import discord
        perms = server_setup.build_permissions("unknown_role")
        # No flags set — equivalent to Permissions.none()
        assert isinstance(perms, discord.Permissions)
        assert perms.value == 0


class TestIsServerEstablished:
    """The heuristic that gates the confirmation prompt in the wizard."""

    def test_fresh_server_not_established(self):
        # New Discord server: typically 3-4 default channels, 0 custom roles, 1 member (bot owner)
        assert not server_setup.is_server_established(4, 0, 1)

    def test_small_test_server_not_established(self):
        # A test/playground server: a few channels, no custom roles, few members
        assert not server_setup.is_server_established(5, 3, 10)

    def test_real_community_established_by_channels(self):
        assert server_setup.is_server_established(20, 0, 1)

    def test_real_community_established_by_roles(self):
        assert server_setup.is_server_established(3, 5, 2)

    def test_real_community_established_by_members(self):
        assert server_setup.is_server_established(3, 0, 50)

    def test_one_threshold_trip_is_enough(self):
        # Established if ANY signal crosses, not all
        assert server_setup.is_server_established(100, 0, 0)
        assert server_setup.is_server_established(0, 100, 0)
        assert server_setup.is_server_established(0, 0, 100)
