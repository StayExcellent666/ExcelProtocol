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
